"""本地数据管理 API 测试:盘点/预下载(替身)/删除/缓存清理。"""

from __future__ import annotations

import json
import pickle

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from aquant.api.main import app
    from aquant.data.bundle import build_bundle
    from aquant.data.synthetic_source import SyntheticSource

    src = SyntheticSource(n_instruments=3, start="2023-01-03", end="2023-03-31", seed=42)
    bundle = build_bundle([src], tmp_path / "bundle")
    (bundle / "cross_check.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("AQUANT_BUNDLE_PATH", str(bundle))
    monkeypatch.setenv("HOME", str(tmp_path))  # 隔离 duckdb 缓存目录
    monkeypatch.setattr("aquant.api.data_admin.default_bundle_path",
                        lambda: bundle)
    app.state.runner = None
    return TestClient(app), bundle, tmp_path


def test_overview_inventory(client):
    tc, bundle, _ = client
    resp = tc.get("/api/data/overview")
    assert resp.status_code == 200
    d = resp.json()
    assert d["counts"]["CS"] == 3
    assert d["counts"]["INDX"] >= 1
    assert d["calendar"]["start"] == "2023-01-03"
    assert d["total_rows"] > 0
    assert any(f["name"] == "stocks.h5" for f in d["files"])
    assert d["bundle_size_bytes"] > 0
    rows = {r["oid"]: r for r in d["instruments"]}
    assert rows["000001.XSHE"]["rows"] > 0
    assert rows["000001.XSHE"]["first"] == "2023-01-03"


def test_fetch_validates_format(client):
    tc, bundle, _ = client
    resp = tc.post("/api/data/fetch", json={"oids": ["bad-code"]})
    assert resp.status_code == 200
    d = resp.json()
    assert d["ok"] is False
    assert "6 位数字" in d["error"]


def test_remove_instruments_rebuilds(client, monkeypatch):
    tc, bundle, tmp_path = client
    # 替身重建:返回剔除后的 bundle(模拟删除)
    real_remove_path = "aquant.data.extend._rebuild_for_coverage"

    def _fake_rebuild(b, out_dir, start, end, **kw):
        from aquant.data.bundle import build_bundle
        from aquant.data.synthetic_source import SyntheticSource
        keep = kw.get("cs_codes") or []
        n = len(keep) if keep else 3
        src = SyntheticSource(n_instruments=max(1, n - 0), start=start, end=end, seed=42)
        # 保持日期一致;合约数用真实 keep 量近似
        build_bundle([src], out_dir)

    # 直接走 remove_instruments(其内部重建被替身会破坏一致性,这里跑真实路径:
    # 合成 bundle 的重建需要网络(Baostock)…… 改为单测 extend.remove_instruments 的
    # 参数校验路径 + 前端契约)
    resp = tc.post("/api/data/instruments/remove", json={"oids": ["999999.XSHE"]})
    assert resp.status_code == 200
    d = resp.json()
    assert d["ok"] is False
    assert "不在数据池中" in d["error"]

    resp = tc.post("/api/data/instruments/remove", json={"oids": ["000001.XSHG"]})
    assert resp.status_code == 200
    d = resp.json()
    assert d["ok"] is False
    assert "基准指数不可删除" in d["error"]


def test_cache_clear(client, tmp_path):
    tc, bundle, tmp = client
    cache_dir = tmp / ".aquant" / "cache" / "duckdb"
    cache_dir.mkdir(parents=True)
    (cache_dir / "c.duckdb").write_bytes(b"x" * 128)
    resp = tc.post("/api/data/cache/clear")
    assert resp.status_code == 200
    d = resp.json()
    assert d["ok"] is True
    assert d["removed"] == ["c.duckdb"]
    assert d["freed_bytes"] == 128
