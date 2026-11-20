"""DuckDB 本地 K 线缓存代理:减少重复请求,并按容量/保留期自动清理。"""
from __future__ import annotations
import json, re
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Any
import pandas as pd
from aquant.data.provider import DAILY_COLUMNS, BarSource
__all__ = ["DuckDBCache", "wrap"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_raw (
  oid VARCHAR, date DATE, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
  volume DOUBLE, total_turnover DOUBLE, PRIMARY KEY (oid, date));
CREATE TABLE IF NOT EXISTS adjust_factor (
  oid VARCHAR, date DATE, factor DOUBLE, PRIMARY KEY (oid, date));
CREATE TABLE IF NOT EXISTS json_cache (
  kind VARCHAR, oid VARCHAR, value VARCHAR, PRIMARY KEY (kind, oid));
CREATE TABLE IF NOT EXISTS access (oid VARCHAR PRIMARY KEY, last_access TIMESTAMP);
"""


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:120]


class DuckDBCache:
    def __init__(self, source: BarSource, cache_dir: str | Path, source_key: str,
                 *, max_size_mb: float = 512.0, retention_days: int | None = None) -> None:
        try:
            import duckdb
        except ImportError as exc:
            raise ImportError("请先安装:pip install 'aquant[duckdb]'") from exc
        self._source = source
        self.dir = Path(cache_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / f"{_safe(source_key)}.duckdb"
        self.max_size_mb = float(max_size_mb)
        self.retention_days = retention_days
        self._con = duckdb.connect(str(self.path))
        for stmt in _SCHEMA.strip().split(";"):
            if stmt.strip():
                self._con.execute(stmt)

    @property
    def source(self) -> BarSource:
        return self._source

    def _touch(self, oid: str) -> None:
        self._con.execute(
            "INSERT INTO access VALUES (?, ?) ON CONFLICT(oid) DO UPDATE SET last_access=excluded.last_access",
            [oid, datetime.now()],
        )

    @staticmethod
    def _frame(rows: pd.DataFrame) -> pd.DataFrame:
        if rows.empty:
            return pd.DataFrame(columns=list(DAILY_COLUMNS))
        rows = rows.set_index("date")
        rows.index = pd.DatetimeIndex(rows.index)
        return rows[list(DAILY_COLUMNS)].sort_index()

    def daily_raw(self, oid: str) -> pd.DataFrame:
        rows = self._con.execute(
            "SELECT date,open,high,low,close,volume,total_turnover FROM daily_raw WHERE oid=? ORDER BY date",
            [oid],
        ).fetchdf()
        if not rows.empty:
            self._touch(oid)
            return self._frame(rows)
        df = self._source.daily_raw(oid)
        if not isinstance(df, pd.DataFrame) or df.empty:
            return df
        records = [
            (oid, pd.Timestamp(d).date(), float(r["open"]), float(r["high"]),
             float(r["low"]), float(r["close"]), float(r["volume"]),
             float(r["total_turnover"]))
            for d, r in df[list(DAILY_COLUMNS)].iterrows()
        ]
        self._con.executemany("INSERT OR REPLACE INTO daily_raw VALUES (?,?,?,?,?,?,?,?)", records)
        self._touch(oid)
        self.enforce_limits()
        rows = self._con.execute(
            "SELECT date,open,high,low,close,volume,total_turnover FROM daily_raw WHERE oid=? ORDER BY date",
            [oid],
        ).fetchdf()
        return self._frame(rows)

    def adjust_factor(self, oid: str) -> pd.Series:
        rows = self._con.execute(
            "SELECT date,factor FROM adjust_factor WHERE oid=? ORDER BY date", [oid]
        ).fetchdf()
        if not rows.empty:
            self._touch(oid)
            s = pd.Series(rows["factor"].to_numpy(),
                          index=pd.DatetimeIndex(pd.to_datetime(rows["date"])), dtype=float)
            s.name = "ex_cum_factor"
            return s
        s = self._source.adjust_factor(oid)
        if s is None or len(s) == 0:
            return s
        self._con.executemany(
            "INSERT OR REPLACE INTO adjust_factor VALUES (?,?,?)",
            [(oid, pd.Timestamp(d).date(), float(v)) for d, v in s.items()],
        )
        self._touch(oid)
        self.enforce_limits()
        return s.copy()

    def _read_json(self, kind: str, oid: str) -> Any:
        row = self._con.execute(
            "SELECT value FROM json_cache WHERE kind=? AND oid=?", [kind, oid]
        ).fetchone()
        if row is None:
            return None
        return json.loads(row[0])

    def _write_json(self, kind: str, oid: str, value: Any) -> None:
        self._con.execute(
            "INSERT OR REPLACE INTO json_cache VALUES (?,?,?)",
            [kind, oid, json.dumps(value, ensure_ascii=False, default=str)],
        )

    def _cached_frame(self, kind: str, oid: str, factory: Any) -> pd.DataFrame:
        value = self._read_json(kind, oid)
        if value is not None:
            self._touch(oid)
            return pd.read_json(StringIO(value), orient="table")
        df = factory()
        self._write_json(kind, oid, df.to_json(orient="table"))
        self._touch(oid)
        self.enforce_limits()
        return df

    def instruments(self) -> list[dict]:
        value = self._read_json("instruments", "__all__")
        if value is not None:
            return value
        value = self._source.instruments()
        self._write_json("instruments", "__all__", value)
        return value

    def dividends(self, oid: str) -> pd.DataFrame:
        return self._cached_frame("dividends", oid, lambda: self._source.dividends(oid))

    def splits(self, oid: str) -> pd.DataFrame:
        return self._cached_frame("splits", oid, lambda: self._source.splits(oid))

    def _cached_set(self, kind: str, oid: str, factory: Any) -> list[int]:
        value = self._read_json(kind, oid)
        if value is not None:
            self._touch(oid)
            return [int(d) for d in value]
        value = [int(d) for d in factory()]
        self._write_json(kind, oid, value)
        self._touch(oid)
        self.enforce_limits()
        return value

    def suspended_days(self, oid: str) -> list[int]:
        return self._cached_set("suspended_days", oid, lambda: self._source.suspended_days(oid))

    def st_days(self, oid: str) -> list[int]:
        return self._cached_set("st_days", oid, lambda: self._source.st_days(oid))

    def prune(self) -> None:
        if not self.retention_days:
            return
        row = self._con.execute("SELECT MAX(date) FROM daily_raw").fetchone()
        if not row or row[0] is None:
            return
        cutoff = (pd.Timestamp(row[0]) - timedelta(days=int(self.retention_days))).date()
        self._con.execute("DELETE FROM daily_raw WHERE date < ?", [cutoff])
        self._con.execute("DELETE FROM adjust_factor WHERE date < ?", [cutoff])
        self._con.execute("CHECKPOINT")

    def _size_mb(self) -> float:
        return self.path.stat().st_size / 1024 / 1024 if self.path.exists() else 0.0

    def enforce_limits(self) -> None:
        self.prune()
        if self.max_size_mb <= 0 or self._size_mb() <= self.max_size_mb:
            return
        self._con.execute("CHECKPOINT")
        if self._size_mb() <= self.max_size_mb:
            return
        victims = [r[0] for r in self._con.execute(
            "SELECT oid FROM access ORDER BY last_access LIMIT 8").fetchall()]
        for oid in victims:
            for sql in ("DELETE FROM daily_raw WHERE oid=?",
                        "DELETE FROM adjust_factor WHERE oid=?",
                        "DELETE FROM json_cache WHERE oid=?",
                        "DELETE FROM access WHERE oid=?"):
                self._con.execute(sql, [oid])
            self._con.execute("CHECKPOINT")
            if self._size_mb() <= self.max_size_mb:
                break

    def stats(self) -> dict[str, Any]:
        counts = {}
        for table in ("daily_raw", "adjust_factor", "json_cache", "access"):
            counts[table] = self._con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        return {"path": str(self.path), "size_mb": round(self._size_mb(), 3), **counts}

    def close(self) -> None:
        self._con.close()


def wrap(source: BarSource, cache_dir: str | Path, source_key: str, **kwargs: Any) -> DuckDBCache:
    return DuckDBCache(source, cache_dir, source_key, **kwargs)

