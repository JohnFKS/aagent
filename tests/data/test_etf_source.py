"""ETF 数据源测试:代码规范化、双源降级、bundle 的 ETF→funds.h5 分流(零网络)。"""

import pickle

import h5py
import numpy as np
import pandas as pd
import pytest

from aquant.data.bundle import build_bundle
from aquant.data.etf_source import EtfSource


def _fake_hist_em(n=6, factor=1.0):
    idx = pd.date_range("2024-01-02", periods=n, freq="B")
    return pd.DataFrame({
        "日期": idx.strftime("%Y-%m-%d"),
        "开盘": [10.0 * factor] * n, "收盘": [10.5 * factor] * n,
        "最高": [11.0 * factor] * n, "最低": [9.0 * factor] * n,
        "成交量": [10_000.0] * n, "成交额": [10_500_000.0 * factor] * n,
    })


def _fake_hist_sina(n=6, factor=1.0):
    idx = pd.date_range("2024-01-02", periods=n, freq="B")
    return pd.DataFrame({
        "date": idx, "open": [10.0 * factor] * n, "high": [11.0 * factor] * n,
        "low": [9.0 * factor] * n, "close": [10.5 * factor] * n,
        "volume": [1_000_000.0] * n, "amount": [10_500_000.0 * factor] * n,
    })


class _FakeAK:
    def __init__(self, em_fail=True):
        self.em_fail = em_fail
        self.em_calls = 0
        self.sina_calls = 0

    def fund_etf_hist_em(self, **kw):
        self.em_calls += 1
        if self.em_fail:
            raise RuntimeError("Push2RateLimitError 限流")
        return _fake_hist_em(factor=1.26 if kw.get("adjust") == "hfq" else 1.0)

    def fund_etf_hist_sina(self, **kw):
        self.sina_calls += 1
        return _fake_hist_sina()

    def tool_trade_date_hist_sina(self):
        return pd.DataFrame({"trade_date": pd.date_range("2024-01-01", "2024-12-31")})


def _src(monkeypatch, fake, codes=None):
    src = EtfSource(codes=codes or ["510300"], start="2024-01-01", end="2024-03-01")
    monkeypatch.setattr(src, "_ak", fake)
    return src


class TestNormalizeCode:
    @pytest.mark.parametrize("raw,expected", [
        ("510300", "510300.XSHG"),
        ("588080", "588080.XSHG"),
        ("159915", "159915.XSHE"),
        ("510300.XSHG", "510300.XSHG"),
        ("sh510300", "510300.XSHG"),
        ("sz159915", "159915.XSHE"),
    ])
    def test_normalize(self, raw, expected):
        assert EtfSource._normalize_code(raw) == expected


class TestInstrumentMeta:
    def test_etf_type_and_fund_type(self, monkeypatch):
        src = _src(monkeypatch, _FakeAK(em_fail=False))
        inst = src.instruments()[0]
        assert inst["type"] == "ETF"
        assert inst["fund_type"] == "ETF"
        assert inst["round_lot"] == 100
        assert inst["listed_date"] == "2024-01-01"


class TestFallback:
    def test_sina_fallback_on_em_ban(self, monkeypatch):
        fake = _FakeAK(em_fail=True)
        src = _src(monkeypatch, fake)
        df = src.daily_raw("510300.XSHG")
        assert fake.em_calls >= 1 and fake.sina_calls >= 1
        assert list(df.columns) == ["open", "high", "low", "close", "volume", "total_turnover"]
        assert df["volume"].iloc[0] == 1_000_000.0  # 新浪成交量已是股
        assert src.source_used["510300.XSHG"] == "sina"
        # 降级源复权因子恒为 1
        assert (src.adjust_factor("510300.XSHG") == 1.0).all()

    def test_eastmoney_no_fallback(self, monkeypatch):
        fake = _FakeAK(em_fail=False)
        src = _src(monkeypatch, fake)
        df = src.daily_raw("510300.XSHG")
        assert fake.sina_calls == 0
        assert df["volume"].iloc[0] == 1_000_000.0  # 东财 10000 手 ×100
        assert src.source_used["510300.XSHG"] == "eastmoney"
        factor = src.adjust_factor("510300.XSHG")
        assert factor.iloc[-1] == pytest.approx(1.26, rel=1e-6)


class TestBundleFundsRouting:
    def test_etf_written_to_funds_h5(self, monkeypatch, tmp_path):
        fake = _FakeAK(em_fail=False)
        src = _src(monkeypatch, fake, codes=["510300", "159915"])
        out = build_bundle([src], tmp_path / "bundle")
        with h5py.File(out / "funds.h5", "r") as f:
            assert "510300.XSHG" in f and "159915.XSHE" in f
        with h5py.File(out / "stocks.h5", "r") as f:
            assert "510300.XSHG" not in f
        with open(out / "instruments.pk", "rb") as f:
            insts = pickle.load(f)
        types = {i["order_book_id"]: i["type"] for i in insts}
        assert types["510300.XSHG"] == "ETF"
        assert types["159915.XSHE"] == "ETF"
