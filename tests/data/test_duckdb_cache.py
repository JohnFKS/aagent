"""DuckDB 缓存:命中不回源、保留期裁剪、超容量淘汰。"""
import pandas as pd
from aquant.data.duckdb_cache import DuckDBCache

IDX = pd.date_range("2024-01-02", periods=10, freq="B")


class FakeSource:
    def __init__(self, days=10):
        self.calls = 0
        i = IDX[:days]
        self.df = pd.DataFrame({
            "open": [10.0] * len(i), "high": [11.0] * len(i), "low": [9.0] * len(i),
            "close": [10.5] * len(i), "volume": [1e6] * len(i),
            "total_turnover": [1.05e7] * len(i),
        }, index=i)

    def daily_raw(self, oid):
        self.calls += 1
        return self.df.copy()

    def adjust_factor(self, oid):
        return pd.Series(1.0, index=self.df.index)

    def instruments(self):
        return []

    def dividends(self, oid):
        return pd.DataFrame()

    def splits(self, oid):
        return pd.DataFrame()

    def suspended_days(self, oid):
        return []

    def st_days(self, oid):
        return []


def test_cache_hit_avoids_source(tmp_path):
    src = FakeSource()
    cache = DuckDBCache(src, tmp_path, "hit", max_size_mb=10)
    first = cache.daily_raw("000001.XSHE")
    second = cache.daily_raw("000001.XSHE")
    assert src.calls == 1
    pd.testing.assert_frame_equal(first, second, check_freq=False, check_names=False)
    assert list(second.columns) == ["open", "high", "low", "close", "volume", "total_turnover"]
    assert cache.stats()["daily_raw"] == 10
    cache.close()


def test_retention_prunes_old_days(tmp_path):
    cache = DuckDBCache(FakeSource(), tmp_path, "retention",
                        max_size_mb=10, retention_days=5)
    df = cache.daily_raw("000001.XSHE")
    assert len(df) <= 6
    assert cache.stats()["daily_raw"] == len(df)
    cache.close()


def test_size_limit_evicts_lru(tmp_path):
    src = FakeSource()
    cache = DuckDBCache(src, tmp_path, "tiny", max_size_mb=0.001)
    cache.daily_raw("000001.XSHE")
    cache.daily_raw("000001.XSHE")
    assert src.calls == 2
    cache.close()
