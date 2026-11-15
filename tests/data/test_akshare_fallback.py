"""AKShare 双源降级链测试(monkeypatch 桩,零网络)。"""

import pandas as pd
import pytest

from aquant.data.akshare_source import AkshareSource


def _fake_daily(n=5, factor=1.0):
    idx = pd.date_range("2024-01-02", periods=n, freq="B")
    return pd.DataFrame(
        {
            "date": idx,
            "open": [10.0 * factor] * n,
            "high": [11.0 * factor] * n,
            "low": [9.0 * factor] * n,
            "close": [10.5 * factor] * n,
            "volume": [1_000_000.0] * n,       # 新浪:单位已是股
            "amount": [10_500_000.0 * factor] * n,
        }
    )


def _fake_hist_em(n=5):
    """东财形态:中文列,成交量单位为手。"""
    idx = pd.date_range("2024-01-02", periods=n, freq="B")
    return pd.DataFrame(
        {
            "日期": idx.strftime("%Y-%m-%d"),
            "开盘": [10.0] * n,
            "收盘": [10.5] * n,
            "最高": [11.0] * n,
            "最低": [9.0] * n,
            "成交量": [10_000.0] * n,          # 手
            "成交额": [10_500_000.0] * n,
        }
    )


class _FakeAK:
    """可编程桩:东财接口抛限流错误,新浪接口返回数据。"""

    class Push2RateLimitError(Exception):
        pass

    def __init__(self, em_fail: bool = True):
        self.em_fail = em_fail
        self.em_calls = 0
        self.sina_calls = 0

    def stock_zh_a_hist(self, **kw):
        self.em_calls += 1
        if self.em_fail:
            raise self.Push2RateLimitError("限流")
        return _fake_hist_em()

    def stock_zh_a_daily(self, **kw):
        self.sina_calls += 1
        # 按请求参数返回:hfq 是 126 倍形态,真实价为 1 倍
        return _fake_daily(factor=126.0 if kw.get("adjust") == "hfq" else 1.0)

    def tool_trade_date_hist_sina(self):
        return pd.DataFrame({"trade_date": pd.date_range("2024-01-01", "2024-12-31")})

    def stock_fh_dividend_163(self, **kw):  # 分红接口:空表
        return pd.DataFrame()


def _make_source(monkeypatch, fake):
    src = AkshareSource(codes=["000001"], start="2024-01-01", end="2024-03-01")
    monkeypatch.setattr(src, "_ak", fake)
    return src


def test_sina_fallback_on_eastmoney_ban(monkeypatch):
    """东财限流 → 自动降级新浪,成交量保持股单位,因子可计算。"""
    fake = _FakeAK(em_fail=True)
    src = _make_source(monkeypatch, fake)
    df = src.daily_raw("000001.XSHE")
    assert fake.em_calls >= 1 and fake.sina_calls >= 1
    assert list(df.columns) == ["open", "high", "low", "close", "volume", "total_turnover"]
    assert df["volume"].iloc[0] == 1_000_000.0  # 未做 手→股 换算
    factor = src.adjust_factor("000001.XSHE")
    assert (factor > 1).all()  # hfq/raw = 126 形态


def test_eastmoney_primary_when_available(monkeypatch):
    """东财可用时不触发新浪(主链路优先)。"""
    fake = _FakeAK(em_fail=False)
    src = _make_source(monkeypatch, fake)
    df = src.daily_raw("000001.XSHE")
    assert fake.sina_calls == 0
    assert df["volume"].iloc[0] == 1_000_000.0  # 手→股(10_000 手 × 100)


def test_sina_symbol_mapping():
    assert AkshareSource._sina_symbol("000001.XSHE") == "sz000001"
    assert AkshareSource._sina_symbol("600519.XSHG") == "sh600519"
    assert AkshareSource._sina_symbol("300750.XSHE") == "sz300750"
