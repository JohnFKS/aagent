"""bundle 区间自动扩展(data.extend)单元测试:不打网络,重建函数用替身。"""

from __future__ import annotations

import json

import pytest

from aquant.data.extend import (
    bundle_calendar,
    ensure_coverage,
    prune_stale_cache,
)


@pytest.fixture()
def real_bundle(tmp_path, monkeypatch):
    """带"真实构建"标记的 bundle(SyntheticSource 产物 + cross_check.json 哨兵)。"""
    from aquant.data.bundle import build_bundle
    from aquant.data.synthetic_source import SyntheticSource

    src = SyntheticSource(n_instruments=3, start="2023-01-03", end="2023-03-31", seed=11)
    out = build_bundle([src], tmp_path / "bundle")
    (out / "cross_check.json").write_text("{}", encoding="utf-8")  # 真实构建哨兵
    return out


def _wider_rebuild(bundle, out_dir, start, end):
    """替身重建:用 SyntheticSource 造一个更宽区间的 bundle(模拟扩展成功)。"""
    from aquant.data.bundle import build_bundle
    from aquant.data.synthetic_source import SyntheticSource

    src = SyntheticSource(n_instruments=3, start=start, end=end, seed=11)
    build_bundle([src], out_dir)
    (out_dir / "cross_check.json").write_text("{}", encoding="utf-8")


def test_bundle_calendar(real_bundle):
    assert bundle_calendar(real_bundle) == ("2023-01-03", "2023-03-31")
    assert bundle_calendar(real_bundle / "nope") is None


def test_ensure_coverage_skips_when_covered(real_bundle):
    assert ensure_coverage(real_bundle, "2023-01-10", "2023-02-20") is None


def test_ensure_coverage_extends_and_swaps(real_bundle, monkeypatch):
    monkeypatch.setattr(
        "aquant.data.extend._rebuild_for_coverage", _wider_rebuild
    )
    info = ensure_coverage(real_bundle, "2023-03-01", "2023-06-30")
    assert info and info["extended"]
    assert info["prior"]["end"] == "2023-03-31"
    assert info["now"]["end"] == "2023-06-30"
    assert info["instruments"] >= 3  # SyntheticSource 实际合约数可能含附加项
    # 原子换目录:新日历生效,旧目录与临时目录均已删除
    assert bundle_calendar(real_bundle) == ("2023-01-03", "2023-06-30")
    leftovers = [p.name for p in real_bundle.parent.iterdir()
                 if p.name.startswith(real_bundle.name) and p.name != real_bundle.name]
    assert leftovers == [], f"旧目录/临时目录未清理: {leftovers}"


def test_ensure_coverage_guards(real_bundle, monkeypatch):
    # 合成 bundle 哨兵移除 → 不扩展
    (real_bundle / "cross_check.json").unlink()
    monkeypatch.setattr(
        "aquant.data.extend._rebuild_for_coverage", _wider_rebuild
    )
    assert ensure_coverage(real_bundle, "2023-03-01", "2023-06-30") is None

    # 环境开关关闭 → 不扩展
    (real_bundle / "cross_check.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("AQUANT_BUNDLE_AUTO_EXTEND", "0")
    assert ensure_coverage(real_bundle, "2023-03-01", "2023-06-30") is None
    monkeypatch.delenv("AQUANT_BUNDLE_AUTO_EXTEND")

    # 跨度超限 → 不扩展
    assert ensure_coverage(real_bundle, "2010-01-01", "2030-01-01", max_span_days=100) is None

    # 重建失败 → 降级返回 None,bundle 原样在位
    def _boom(bundle, out_dir, start, end):
        raise RuntimeError("网络不可用")
    monkeypatch.setattr("aquant.data.extend._rebuild_for_coverage", _boom)
    assert ensure_coverage(real_bundle, "2023-03-01", "2023-06-30") is None
    assert bundle_calendar(real_bundle) == ("2023-01-03", "2023-03-31")


def test_ensure_coverage_rebuild_failure_keeps_bundle_usable(real_bundle, monkeypatch):
    def _half_broken(bundle, out_dir, start, end):
        _wider_rebuild(bundle, out_dir, start, end)
        (out_dir / "trading_dates.npy").unlink()  # 产物校验失败路径
    monkeypatch.setattr("aquant.data.extend._rebuild_for_coverage", _half_broken)
    assert ensure_coverage(real_bundle, "2023-03-01", "2023-06-30") is None
    assert bundle_calendar(real_bundle) == ("2023-01-03", "2023-03-31")


def test_prune_stale_cache(tmp_path):
    import duckdb
    import pandas as pd

    db = tmp_path / "cache.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE access (oid VARCHAR PRIMARY KEY, last_access TIMESTAMP)")
    con.execute("CREATE TABLE daily_raw (oid VARCHAR, date BIGINT)")
    old = (pd.Timestamp.now() - pd.Timedelta(days=200)).to_pydatetime()
    fresh = pd.Timestamp.now().to_pydatetime()
    con.execute("INSERT INTO access VALUES ('OLD', ?)", [old])
    con.execute("INSERT INTO access VALUES ('NEW', ?)", [fresh])
    con.execute("INSERT INTO daily_raw VALUES ('OLD', 20230101), ('NEW', 20230102)")
    con.close()

    removed = prune_stale_cache(tmp_path, retention_days=90)
    assert removed == 1  # 只清 200 天未访问的 OLD

    con = duckdb.connect(str(db))
    oids = [r[0] for r in con.execute("SELECT oid FROM access").fetchall()]
    raw = [r[0] for r in con.execute("SELECT DISTINCT oid FROM daily_raw").fetchall()]
    con.close()
    assert oids == ["NEW"] and raw == ["NEW"]
