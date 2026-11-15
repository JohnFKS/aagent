"""ParquetCache 测试:roundtrip 值一致 + 计数 spy 验证命中不再回源。"""

from __future__ import annotations

import pandas as pd
import pytest

from aquant.data.cache import ParquetCache, wrap
from aquant.data.provider import BarSource
from aquant.data.synthetic_source import SyntheticSource

PARAMS = dict(n_instruments=3, start="2023-06-01", end="2024-06-30", seed=42)


class CountingSource:
    """计数 spy:代理一个真实 source,统计每个协议方法被调用的次数。"""

    def __init__(self, inner: SyntheticSource) -> None:
        self._inner = inner
        self.calls: dict[str, int] = {
            "instruments": 0, "daily_raw": 0, "adjust_factor": 0,
            "dividends": 0, "splits": 0, "suspended_days": 0, "st_days": 0,
        }

    def instruments(self) -> list[dict]:
        self.calls["instruments"] += 1
        return self._inner.instruments()

    def daily_raw(self, oid: str) -> pd.DataFrame:
        self.calls["daily_raw"] += 1
        return self._inner.daily_raw(oid)

    def adjust_factor(self, oid: str) -> pd.Series:
        self.calls["adjust_factor"] += 1
        return self._inner.adjust_factor(oid)

    def dividends(self, oid: str) -> pd.DataFrame:
        self.calls["dividends"] += 1
        return self._inner.dividends(oid)

    def splits(self, oid: str) -> pd.DataFrame:
        self.calls["splits"] += 1
        return self._inner.splits(oid)

    def suspended_days(self, oid: str) -> list[int]:
        self.calls["suspended_days"] += 1
        return self._inner.suspended_days(oid)

    def st_days(self, oid: str) -> list[int]:
        self.calls["st_days"] += 1
        return self._inner.st_days(oid)


@pytest.fixture()
def spy(tmp_path) -> tuple[ParquetCache, CountingSource, SyntheticSource]:
    inner = SyntheticSource(**PARAMS)
    spy_source = CountingSource(inner)
    return wrap(spy_source, tmp_path / "cache"), spy_source, inner


def _touch_everything(cache: ParquetCache, oids: list[str]) -> None:
    cache.instruments()
    for oid in oids:
        cache.daily_raw(oid)
        cache.adjust_factor(oid)
        cache.dividends(oid)
        cache.splits(oid)
        cache.suspended_days(oid)
        cache.st_days(oid)


class TestParquetCache:
    def test_wrap_implements_bar_source_protocol(self, spy):
        cache, _, _ = spy
        assert isinstance(cache, ParquetCache)
        assert isinstance(cache, BarSource)  # runtime_checkable 协议

    def test_roundtrip_values_equal(self, spy):
        """缓存命中与直连 source 的值逐项一致(DataFrame/Series/list/dict)。"""
        cache, _, inner = spy
        oids = [i["order_book_id"] for i in cache.instruments()]
        assert cache.instruments() == inner.instruments()
        for oid in oids:
            pd.testing.assert_frame_equal(cache.daily_raw(oid), inner.daily_raw(oid))
            pd.testing.assert_series_equal(cache.adjust_factor(oid), inner.adjust_factor(oid))
            pd.testing.assert_frame_equal(cache.dividends(oid), inner.dividends(oid))
            pd.testing.assert_frame_equal(cache.splits(oid), inner.splits(oid))
            assert cache.suspended_days(oid) == inner.suspended_days(oid)
            assert cache.st_days(oid) == inner.st_days(oid)

    def test_roundtrip_preserves_datetime_index(self, spy):
        cache, _, _ = spy
        oid = cache.instruments()[0]["order_book_id"]
        df = cache.daily_raw(oid)
        assert isinstance(df.index, pd.DatetimeIndex)
        assert df.index.is_monotonic_increasing
        s = cache.adjust_factor(oid)
        assert isinstance(s.index, pd.DatetimeIndex)
        assert s.index.equals(df.index)

    def test_second_call_never_hits_source(self, spy):
        """计数 spy:首次调用回源一次;二次调用全部命中缓存,不再回源。"""
        cache, spy_source, _ = spy
        oids = [i["order_book_id"] for i in cache.instruments()]
        _touch_everything(cache, oids)
        first = dict(spy_source.calls)
        # 每个方法对每个 oid 恰好回源一次(instruments 一次)
        assert first["daily_raw"] == len(oids)
        assert first["adjust_factor"] == len(oids)
        assert first["instruments"] == 1

        _touch_everything(cache, oids)
        _touch_everything(cache, oids)
        assert spy_source.calls == first, "二次调用不应再回源"

    def test_cache_dir_reuse_never_hits_fresh_source(self, spy, tmp_path):
        """同一 cache_dir 换全新 source 实例(同 seed 数据)→ 全部命中,0 次回源。"""
        cache, _, inner = spy
        oids = [i["order_book_id"] for i in cache.instruments()]
        _touch_everything(cache, oids)  # 预热缓存

        fresh_inner = SyntheticSource(**PARAMS)  # 同 seed ⇒ 数据一致的新实例
        fresh_spy = CountingSource(fresh_inner)
        cache2 = wrap(fresh_spy, cache.cache_dir)
        _touch_everything(cache2, oids)
        assert set(fresh_spy.calls.values()) == {0}, "缓存复用不应回源"

    def test_cache_files_layout(self, spy):
        cache, _, _ = spy
        oid = cache.instruments()[0]["order_book_id"]
        cache.daily_raw(oid)
        cache.adjust_factor(oid)
        cache.dividends(oid)
        cache.splits(oid)
        cache.suspended_days(oid)
        cache.st_days(oid)
        cache.instruments()
        root = cache.cache_dir
        assert (root / "daily_raw" / f"{oid}.parquet").exists()
        assert (root / "adjust_factor" / f"{oid}.parquet").exists()
        assert (root / "dividends" / f"{oid}.parquet").exists()
        assert (root / "splits" / f"{oid}.parquet").exists()
        assert (root / "sets" / f"suspended_days.{oid}.json").exists()
        assert (root / "sets" / f"st_days.{oid}.json").exists()
        assert (root / "instruments.json").exists()

    def test_dividend_split_data_roundtrip_with_events(self, spy):
        """合约 0 注入了分红+拆股,parquet 往返后事件明细与因子不变。"""
        cache, _, inner = spy
        oid = inner.instruments()[0]["order_book_id"]
        assert len(inner.dividends(oid)) == 1
        assert len(inner.splits(oid)) == 1
        pd.testing.assert_frame_equal(cache.dividends(oid), inner.dividends(oid))
        pd.testing.assert_frame_equal(cache.splits(oid), inner.splits(oid))
        # 因子单调不减且事件跳变保留
        cached_factor = cache.adjust_factor(oid).to_numpy()
        assert cached_factor[-1] == inner.adjust_factor(oid).iloc[-1]
        assert cached_factor[-1] > 1.0
