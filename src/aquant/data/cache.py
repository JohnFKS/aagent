"""Layer 2:Parquet 本地缓存代理(透明加速,不改口径)。

``wrap(source, cache_dir)`` 返回实现同一 BarSource 协议的缓存代理:
- ``daily_raw`` / ``adjust_factor``:每 order_book_id 一个 parquet 文件
  (DatetimeIndex/Series 语义由 pandas parquet 读写原样保留,float 精确往返);
- ``instruments`` / ``dividends`` / ``splits``:parquet(表格型,列结构固定);
- ``suspended_days`` / ``st_days``:JSON(int 列表,天然适合);
- 未命中 → 回源取数后写盘;命中 → 直接读盘,**不再触碰底层 source**。

缓存语义是"按 (source 实例提供的口径) 一次性落盘":同一 cache_dir 复用时,
即使换一个同 seed 的新 source 实例,命中即读盘、不回源。因此 cache_dir 必须
与数据口径一一对应(建议路径中包含 source 标识与参数,或每次重建)。
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from aquant.data.provider import BarSource

__all__ = ["ParquetCache", "wrap"]

_INSTRUMENTS_FILE = "instruments.json"

# 子目录:每日行情 / 复权因子 / 分红 / 拆股 用 parquet;停牌/ST 用 json
_SUBDIRS = {
    "daily_raw": "daily_raw",
    "adjust_factor": "adjust_factor",
    "dividends": "dividends",
    "splits": "splits",
}
_SET_KINDS = ("suspended_days", "st_days")


class ParquetCache:
    """BarSource 的 Parquet 缓存代理(命中读盘,未命中回源后写盘)。"""

    def __init__(self, source: BarSource, cache_dir: str | Path) -> None:
        self._source = source
        self._cache_dir = Path(cache_dir)
        for d in (*_SUBDIRS.values(), "sets"):
            (self._cache_dir / d).mkdir(parents=True, exist_ok=True)
        self._instruments: list[dict] | None = None

    # ------------------------------------------------------------------
    # 内部:缓存路径
    # ------------------------------------------------------------------
    def _table_path(self, kind: str, oid: str) -> Path:
        return self._cache_dir / _SUBDIRS[kind] / f"{oid}.parquet"

    def _set_path(self, kind: str, oid: str) -> Path:
        return self._cache_dir / "sets" / f"{kind}.{oid}.json"

    @property
    def source(self) -> BarSource:
        """被代理的底层 source(测试/调试用)。"""
        return self._source

    @property
    def cache_dir(self) -> Path:
        return self._cache_dir

    # ------------------------------------------------------------------
    # BarSource 协议
    # ------------------------------------------------------------------
    def instruments(self) -> list[dict]:
        if self._instruments is None:
            path = self._cache_dir / _INSTRUMENTS_FILE
            if path.exists():
                with open(path, "r", encoding="utf-8") as f:
                    self._instruments = json.load(f)
            else:
                self._instruments = self._source.instruments()
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(self._instruments, f, ensure_ascii=False)
        return [dict(item) for item in self._instruments]

    def daily_raw(self, oid: str) -> pd.DataFrame:
        path = self._table_path("daily_raw", oid)
        if path.exists():
            return pd.read_parquet(path)
        df = self._source.daily_raw(oid)
        df.to_parquet(path)
        return df.copy()

    def adjust_factor(self, oid: str) -> pd.Series:
        path = self._table_path("adjust_factor", oid)
        if path.exists():
            return pd.read_parquet(path).iloc[:, 0]
        s = self._source.adjust_factor(oid)
        s.to_frame().to_parquet(path)
        return s.copy()

    def dividends(self, oid: str) -> pd.DataFrame:
        path = self._table_path("dividends", oid)
        if path.exists():
            return pd.read_parquet(path)
        df = self._source.dividends(oid)
        df.to_parquet(path)
        return df.copy()

    def splits(self, oid: str) -> pd.DataFrame:
        path = self._table_path("splits", oid)
        if path.exists():
            return pd.read_parquet(path)
        df = self._source.splits(oid)
        df.to_parquet(path)
        return df.copy()

    def suspended_days(self, oid: str) -> list[int]:
        return self._read_or_write_set("suspended_days", oid)

    def st_days(self, oid: str) -> list[int]:
        return self._read_or_write_set("st_days", oid)

    # ------------------------------------------------------------------
    # 内部:日期集合(JSON int 列表)
    # ------------------------------------------------------------------
    def _read_or_write_set(self, kind: str, oid: str) -> list[int]:
        path = self._set_path(kind, oid)
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                return [int(d) for d in json.load(f)]
        days = list(self._source_days(kind, oid))
        with open(path, "w", encoding="utf-8") as f:
            json.dump([int(d) for d in days], f)
        return days

    def _source_days(self, kind: str, oid: str) -> list[int]:
        if kind == "suspended_days":
            return list(self._source.suspended_days(oid))
        return list(self._source.st_days(oid))


def wrap(source: BarSource, cache_dir: str | Path) -> ParquetCache:
    """用 ParquetCache 代理 source,返回实现同一 BarSource 协议的对象。"""
    return ParquetCache(source, cache_dir)
