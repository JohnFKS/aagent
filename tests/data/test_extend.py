"""bundle 区间自动扩展(data.extend)单元测试:不打网络,重建函数用替身。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from aquant.data.extend import ensure_coverage
from aquant.data.instruments import load_instruments

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


def _load_instruments_of(bundle_dir):
    import pickle as _p

    with open(Path(bundle_dir) / "instruments.pk", "rb") as f:
        return _p.load(f)


def _wider_rebuild(bundle, out_dir, start, end, **kw):
    """替身重建:用 SyntheticSource 造一个更宽区间的 bundle(模拟扩展成功)。

    请求包含的新标的(codes 中 bundle 原本没有的 oid)会被登记为额外合约,
    模拟"在线获取并加入数据池"。
    """
    from aquant.data.bundle import build_bundle
    from aquant.data.synthetic_source import SyntheticSource

    codes = kw.get("cs_codes") or []
    known = {i.get("order_book_id") for i in _load_instruments_of(bundle)}
    extra = [c for c in codes if c not in known]
    src = SyntheticSource(n_instruments=3, start=start, end=end, seed=11)
    build_bundle([src], out_dir)
    import pickle as _p
    insts = _load_instruments_of(out_dir)
    for oid in extra or []:
        insts.append({
            "order_book_id": oid, "symbol": "新标的", "type": "CS",
            "listed_date": start, "de_listed_date": "0000-00-00",
            "round_lot": 100, "exchange": oid.split(".")[-1],
        })
    with open(out_dir / "instruments.pk", "wb") as f:
        _p.dump(insts, f)
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
    def _boom(bundle, out_dir, start, end, **kw):
        raise RuntimeError("网络不可用")
    monkeypatch.setattr("aquant.data.extend._rebuild_for_coverage", _boom)
    assert ensure_coverage(real_bundle, "2023-03-01", "2023-06-30") is None
    assert bundle_calendar(real_bundle) == ("2023-01-03", "2023-03-31")


def test_ensure_coverage_rebuild_failure_keeps_bundle_usable(real_bundle, monkeypatch):
    def _half_broken(bundle, out_dir, start, end, **kw):
        _wider_rebuild(bundle, out_dir, start, end, **kw)
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


# ---------------------------------------------------------------------------
# 标的自适应:任意正常上市股票自动获取 + 长期未用清理
# ---------------------------------------------------------------------------

def test_ensure_coverage_adds_missing_instrument(real_bundle, monkeypatch):
    """bundle 外的新标的:自动获取并加入数据池(重建合约含新标的)。"""
    captured = {}

    def _fake_rebuild(bundle, out_dir, start, end, **kw):
        captured["codes"] = kw.get("cs_codes")
        _wider_rebuild(bundle, out_dir, start, end, **kw)
        # 替身同样登记新标的(模拟在线获取成功)
        import pickle as _p
        with open(out_dir / "instruments.pk", "rb") as f:
            insts = _p.load(f)
        for oid in kw.get("cs_codes") or []:
            if not any(i.get("order_book_id") == oid for i in insts):
                insts.append({
                    "order_book_id": oid, "symbol": "新标的", "type": "CS",
                    "listed_date": start, "de_listed_date": "0000-00-00",
                    "round_lot": 100, "exchange": oid.split(".")[-1],
                })
        with open(out_dir / "instruments.pk", "wb") as f:
            _p.dump(insts, f)

    monkeypatch.setattr("aquant.data.extend._rebuild_for_coverage", _fake_rebuild)

    def _fake_load(self, oid):
        self._basics[oid] = {"code_name": "新标的", "type": "1", "status": "1"}

    monkeypatch.setattr("aquant.data.baostock_source.BaostockSource._load", _fake_load)

    new_oid = "601398.XSHG"  # 不在合成 bundle 的合约里
    info = ensure_coverage(real_bundle, "2023-01-10", "2023-02-20", extra_oids={new_oid})
    assert info and info["extended"]
    assert new_oid in info["added"]
    # 换新后新标的已注册
    from aquant.data.instruments import load_instruments
    assert new_oid in load_instruments(real_bundle)
    # 使用台账已登记
    ledger = json.loads((real_bundle / "instrument_usage.json").read_text(encoding="utf-8"))
    assert new_oid in ledger


def test_ensure_coverage_rejects_unavailable_instrument(real_bundle, monkeypatch):
    """退市/无行情的标的 → InstrumentUnavailableError(可读原因),bundle 原样在位。"""
    from aquant.data.extend import InstrumentUnavailableError

    def _no_data(self, oid):
        raise ValueError(f"{oid}:Baostock 区间无日线数据")

    monkeypatch.setattr(
        "aquant.data.baostock_source.BaostockSource._load", _no_data
    )
    with pytest.raises(InstrumentUnavailableError) as exc:
        ensure_coverage(real_bundle, "2023-01-10", "2023-02-20", extra_oids={"999999.XSHE"})
    assert "无法提供行情数据" in str(exc.value)
    # bundle 未被破坏
    assert bundle_calendar(real_bundle) == ("2023-01-03", "2023-03-31")


def test_ensure_coverage_evicts_stale_instruments(real_bundle, monkeypatch):
    """90 天未使用的标的在重建时清理;本次请求的标的与指数保留。"""
    captured = {}

    def _fake_rebuild(bundle, out_dir, start, end, **kw):
        captured["codes"] = list(kw.get("cs_codes") or [])
        _wider_rebuild(bundle, out_dir, start, end, **kw)
        import pickle as _p
        with open(out_dir / "instruments.pk", "rb") as f:
            insts = _p.load(f)
        for oid in kw.get("cs_codes") or []:
            if not any(i.get("order_book_id") == oid for i in insts):
                insts.append({
                    "order_book_id": oid, "symbol": "存量标的", "type": "CS",
                    "listed_date": start, "de_listed_date": "0000-00-00",
                    "round_lot": 100, "exchange": oid.split(".")[-1],
                })
        with open(out_dir / "instruments.pk", "wb") as f:
            _p.dump(insts, f)

    monkeypatch.setattr("aquant.data.extend._rebuild_for_coverage", _fake_rebuild)

    # 预置台账:600519.XSHG 200 天未用(应清理),300750.XSHE 近期使用(保留),
    # 000001.XSHE 不在请求中且 200 天未用 → 也应清理(与真实策略不符可再取回)
    stale_iso = (datetime.now() - timedelta(days=200)).strftime("%Y-%m-%d %H:%M:%S")
    ledger = {}
    all_insts = load_instruments(real_bundle)
    for oid in all_insts:
        ledger[oid] = stale_iso
    ledger["300750.XSHE"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")  # 近期用过
    (real_bundle / "instrument_usage.json").write_text(
        json.dumps(ledger), encoding="utf-8")

    # 清理在"下一次触发重建"时惰性执行:用超出末日的区间触发
    info = ensure_coverage(real_bundle, "2023-01-10", "2023-04-30",
                           extra_oids={"300750.XSHE"})
    assert info and info["extended"]
    assert "600519.XSHG" in info["evicted"]
    assert "300750.XSHE" not in info["evicted"]      # 本次请求的标的不清理
    assert "000001.XSHG" not in info["evicted"]      # 基准指数永不清理
    assert "600519.XSHG" not in captured["codes"]    # 重建清单不含被清理标的
    assert "300750.XSHE" in captured["codes"]
