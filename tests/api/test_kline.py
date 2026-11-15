"""GET /api/kline(K 线行情端点)测试。

- 合成 bundle(fixture 用 aquant.data.bundle.build_bundle + SyntheticSource 造,
  与 tests/data/test_bundle.py 同法)→ 200:candles 数量 / 字段 / 日期范围与
  合成源逐值一致(真实价);
- 超 500 根等距抽样:数量 ≤ MAX_POINTS、严格递增、保留首末;
- 容错映射:bundle 缺失(monkeypatch default_bundle_path)→ 400 + BUNDLE_GUIDANCE;
  oid 不在 bundle → 400 + 可用提示;stocks.h5 损坏 → 400;日期格式非法 → 400;
- scope 门禁:/api/kline 为 readonly 新登记——readonly token 可读,无 token 401,
  readwrite token 亦可读;闸门关闭时零改动放行(现状回归)。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aquant.api import token_gate
from aquant.api.kline import MAX_POINTS, load_kline
from aquant.api.main import BUNDLE_GUIDANCE, app
from aquant.config import schema
from aquant.data.bundle import build_bundle
from aquant.data.synthetic_source import SyntheticSource

START_FULL, END_FULL = "2023-01-01", "2024-12-31"

CANDLE_FIELDS = {"date", "open", "close", "high", "low", "volume"}


@pytest.fixture(scope="module")
def source() -> SyntheticSource:
    return SyntheticSource(n_instruments=2, start=START_FULL, end=END_FULL, seed=42)


@pytest.fixture(scope="module")
def bundle(source: SyntheticSource, tmp_path_factory) -> Path:
    return build_bundle([source], tmp_path_factory.mktemp("kline-bundle"))


@pytest.fixture()
def bundle_env(bundle: Path, monkeypatch) -> Path:
    """把 AQUANT_BUNDLE_PATH 指到合成 bundle(default_bundle_path 每请求读环境变量)。"""
    monkeypatch.setenv("AQUANT_BUNDLE_PATH", str(bundle))
    return bundle


@pytest.fixture()
def client():
    """kline 不依赖 llm/runner,直接用 app 默认依赖(闸门关闭 = 开放模式)。"""
    with TestClient(app) as c:
        yield c


def _expected_dates(source: SyntheticSource, oid: str) -> list:
    """合成源推导 bundle 里该 oid 的行日期序列(停牌日不写行,与 bundle.py 一致)。"""
    raw = source.daily_raw(oid)
    susp = set(source.suspended_days(oid))
    return [d for d in raw.index if int(d.strftime("%Y%m%d")) not in susp]


class TestKlineOk:
    def test_month_range_fields_dates_values(self, client, bundle_env, source):
        """月区间:200 + candles 数量/字段/日期范围正确,OHLCV 与合成源逐值一致。"""
        oid = source.instruments()[0]["order_book_id"]
        resp = client.get(
            "/api/kline", params={"oid": oid, "start": "2023-01-01", "end": "2023-01-31"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["oid"] == oid
        assert body["start"] == "2023-01-01"
        assert body["end"] == "2023-01-31"

        expected = [d for d in _expected_dates(source, oid)
                    if "20230101" <= d.strftime("%Y%m%d") <= "20230131"]
        candles = body["candles"]
        assert len(candles) == len(expected) > 0

        dates = [c["date"] for c in candles]
        assert dates == [d.strftime("%Y-%m-%d") for d in expected]  # 范围 + 升序
        assert dates[0] >= "2023-01-01" and dates[-1] <= "2023-01-31"

        raw = source.daily_raw(oid)
        for candle, day in zip(candles, expected):
            assert set(candle) == CANDLE_FIELDS
            row = raw.loc[day]
            assert candle["open"] == pytest.approx(float(row["open"]))
            assert candle["close"] == pytest.approx(float(row["close"]))
            assert candle["high"] == pytest.approx(float(row["high"]))
            assert candle["low"] == pytest.approx(float(row["low"]))
            assert candle["volume"] == pytest.approx(float(row["volume"]))

    def test_candle_shape_fits_candlestick(self, client, bundle_env, source):
        """OHLC 形态合理:low ≤ open/close ≤ high(蜡烛图不出现负值/倒挂)。"""
        oid = source.instruments()[0]["order_book_id"]
        resp = client.get(
            "/api/kline", params={"oid": oid, "start": "2023-01-01", "end": "2023-03-31"}
        )
        for c in resp.json()["candles"]:
            assert 0 < c["low"] <= min(c["open"], c["close"])
            assert max(c["open"], c["close"]) <= c["high"]

    def test_no_range_returns_all_rows(self, client, bundle_env, source):
        """不带 start/end:返回全部行(≤500 根上限内 = 全量)。"""
        oid = source.instruments()[0]["order_book_id"]
        resp = client.get("/api/kline", params={"oid": oid})
        assert resp.status_code == 200
        candles = resp.json()["candles"]
        expected = _expected_dates(source, oid)
        if len(expected) <= MAX_POINTS:
            assert len(candles) == len(expected)
        else:
            assert len(candles) <= MAX_POINTS

    def test_out_of_range_returns_empty_list(self, client, bundle_env):
        """区间完全越界:200 + candles == [](前端不渲染,而非报错)。"""
        resp = client.get(
            "/api/kline",
            params={"oid": "000001.XSHE", "start": "2030-01-01", "end": "2030-12-31"},
        )
        assert resp.status_code == 200
        assert resp.json()["candles"] == []

    def test_load_kline_direct_call(self, bundle_env, source):
        """数据层直测:load_kline 返回值与 API 层契约一致。"""
        oid = source.instruments()[0]["order_book_id"]
        candles = load_kline(oid, "2023-01-01", "2023-01-10")
        assert candles and set(candles[0]) == CANDLE_FIELDS
        assert candles[0]["date"] >= "2023-01-01"


class TestKlineSampling:
    def test_over_500_points_evenly_sampled_keeps_first_last(self, client, bundle_env, source):
        """全区间(>500 根)→ 等距抽样 ≤500 根、严格递增、保留首末两根。"""
        oid = source.instruments()[0]["order_book_id"]
        resp = client.get(
            "/api/kline", params={"oid": oid, "start": START_FULL, "end": END_FULL}
        )
        assert resp.status_code == 200
        candles = resp.json()["candles"]

        all_dates = [d.strftime("%Y-%m-%d") for d in _expected_dates(source, oid)]
        assert len(all_dates) > MAX_POINTS  # 前提:合成区间确实超过 500 根
        assert len(candles) <= MAX_POINTS
        dates = [c["date"] for c in candles]
        assert dates == sorted(set(dates)), "抽样后必须严格递增、无重复"
        assert dates[0] == all_dates[0], "首根必须保留"
        assert dates[-1] == all_dates[-1], "末根必须保留"
        # 抽样点必须都是原始序列的子集
        assert set(dates) <= set(all_dates)


class TestKlineErrors:
    def test_unknown_oid_400_with_available_hint(self, client, bundle_env):
        """oid 不在 bundle → 400 + 可用 oid 提示(列出前几个)。"""
        resp = client.get("/api/kline", params={"oid": "999999.XSHE"})
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert detail.startswith("合约不在数据 bundle 中")
        assert "000001.XSHE" in detail and "600519.XSHG" in detail

    def test_missing_bundle_400_guidance(self, client, monkeypatch):
        """bundle 缺失(monkeypatch default_bundle_path)→ 400 + BUNDLE_GUIDANCE 文案。"""
        monkeypatch.setattr(
            schema, "default_bundle_path", lambda: Path("/nonexistent/kline-bundle")
        )
        resp = client.get("/api/kline", params={"oid": "000001.XSHE"})
        assert resp.status_code == 400
        body = resp.json()
        assert body["detail"] == BUNDLE_GUIDANCE
        assert "bundle" in body["reason"]

    def test_corrupt_stocks_h5_400(self, client, tmp_path, monkeypatch):
        """worker 容错:stocks.h5 损坏(非 h5 文件)→ 400 读取失败提示,而非 500。"""
        bad = tmp_path / "bad-bundle"
        bad.mkdir()
        (bad / "stocks.h5").write_text("this is not hdf5", encoding="utf-8")
        monkeypatch.setattr(schema, "default_bundle_path", lambda: bad)
        resp = client.get("/api/kline", params={"oid": "000001.XSHE"})
        assert resp.status_code == 400
        assert "K 线数据读取失败" in resp.json()["detail"]

    def test_bad_date_format_400(self, client, bundle_env):
        """start 非 YYYY-MM-DD → 400(可修复的参数错误)。"""
        resp = client.get(
            "/api/kline", params={"oid": "000001.XSHE", "start": "2023/01/01"}
        )
        assert resp.status_code == 400
        assert "YYYY-MM-DD" in resp.json()["detail"]

    def test_blank_oid_400(self, client, bundle_env):
        """oid 全空白 → 400 提示提供合约代码。"""
        resp = client.get("/api/kline", params={"oid": "   "})
        assert resp.status_code == 400
        assert "oid" in resp.json()["detail"]

    def test_load_kline_missing_bundle_raises_file_not_found(self, monkeypatch):
        """数据层直测:bundle 缺失抛 FileNotFoundError(API 层转构建指引)。"""
        monkeypatch.setattr(
            schema, "default_bundle_path", lambda: Path("/nonexistent/kline-bundle")
        )
        with pytest.raises(FileNotFoundError):
            load_kline("000001.XSHE", None, None)


class TestKlineScopeGate:
    """/api/kline 在 token_gate.SCOPE_ROUTES readonly 档登记后的门禁行为。"""

    @pytest.fixture()
    def gated(self, bundle_env, client, monkeypatch):
        monkeypatch.setenv(
            "AQUANT_MCP_TOKENS",
            json.dumps(
                {
                    "tkn_ro_kline": {"scope": "readonly", "name": "只读访客", "rpm": 60},
                    "tkn_rw_kline": {"scope": "readwrite", "name": "读写", "rpm": 60},
                },
                ensure_ascii=False,
            ),
        )
        token_gate._reset_rate_limits()
        yield client
        token_gate._reset_rate_limits()

    def test_readonly_token_can_read_kline(self, gated, source):
        """闸门开启 + readonly token:GET /api/kline 放行(只读端点)。"""
        oid = source.instruments()[0]["order_book_id"]
        resp = gated.get("/api/kline", params={"oid": oid}, headers={"X-MCP-Token": "tkn_ro_kline"})
        assert resp.status_code == 200
        assert len(resp.json()["candles"]) > 0

    def test_readwrite_token_can_read_kline(self, gated, source):
        oid = source.instruments()[0]["order_book_id"]
        resp = gated.get("/api/kline", params={"oid": oid}, headers={"X-MCP-Token": "tkn_rw_kline"})
        assert resp.status_code == 200

    def test_missing_token_gets_401_when_gate_on(self, gated, source):
        oid = source.instruments()[0]["order_book_id"]
        resp = gated.get("/api/kline", params={"oid": oid})
        assert resp.status_code == 401
        assert resp.json()["detail"] == "无效或缺失的 API Token"

    def test_gate_closed_allows_anonymous(self, bundle_env, client, source):
        """闸门关闭(未配置 token):现状行为,匿名放行。"""
        oid = source.instruments()[0]["order_book_id"]
        assert client.get("/api/kline", params={"oid": oid}).status_code == 200
