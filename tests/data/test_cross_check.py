"""跨源交叉校验:一致用主力,超差按覆盖度切换到最全源。"""
import pandas as pd
from aquant.data.cross_check import choose_source

IDX = pd.date_range("2024-01-02", periods=5, freq="B")


class FakeSource:
    def __init__(self, scale: float, days: int = 5):
        i = IDX[:days]
        self.df = pd.DataFrame({
            "open": [10.0 * scale] * len(i), "high": [11.0 * scale] * len(i),
            "low": [9.0 * scale] * len(i), "close": [10.5 * scale] * len(i),
            "volume": [1_000_000.0] * len(i),
            "total_turnover": [1.05e7 * scale] * len(i),
        }, index=i)

    def daily_raw(self, oid):
        return self.df.copy()

    def adjust_factor(self, oid):
        return pd.Series(1.0, index=self.df.index)


def test_consistent_prices_use_primary():
    sources = {"baostock": FakeSource(1.0), "akshare": FakeSource(1.0)}
    chosen, report = choose_source(sources, ["000001.XSHE"], primary="baostock")
    assert chosen == "baostock"
    assert report.used_fallback is False
    assert report.conflicts == []


def test_super_diff_falls_back_to_most_complete_source():
    sources = {"baostock": FakeSource(1.0, days=3), "akshare": FakeSource(1.2, days=5)}
    chosen, report = choose_source(sources, ["000001.XSHE"], primary="baostock")
    assert chosen == "akshare"
    assert report.used_fallback is True
    assert report.conflicts and report.conflicts[0]["max_raw_diff"] > 0.16
    assert "不混源" in report.reason


def test_no_conflict_keeps_primary_even_if_less_complete():
    sources = {"baostock": FakeSource(1.0, days=3), "akshare": FakeSource(1.0, days=5)}
    chosen, report = choose_source(sources, ["000001.XSHE"], primary="baostock")
    assert chosen == "baostock"
    assert report.used_fallback is False
