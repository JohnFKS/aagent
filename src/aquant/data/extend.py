"""Layer 2:bundle 区间自动扩展(回测区间超出数据范围时自动拉取最新数据)。

背景:bundle 是静态产物(构建时固定日期区间)。回测请求超出区间(典型:数据
截止 2024-12-31,用户回测 2026 年)时会以 "There is no data" 崩溃。本模块让
平台**自动拉取最新行情**:按请求区间重建 bundle(复用 build_bundle 的整套
正确性管线:真实价 + 板块涨跌停 + 复权因子 + 跨源比价),重建后重注入真实
基准指数(benchmark.inject_benchmark),然后原子换目录。

数据保留策略(对应"之前存的数据长时间不用就删除"):
- 旧 bundle 目录在成功换新后**直接删除**(数据可由管线再生,不占磁盘);
- DuckDB K 线缓存按 access 表的最近访问时间清理:`prune_stale_cache`
  (默认 90 天未访问的 oid 连同其行情行一起删除;existing LRU 按容量淘汰不变)。

安全阀:
- 环境变量 ``AQUANT_BUNDLE_AUTO_EXTEND=0`` 关闭(默认开);
- 仅对"真实构建"的 bundle 生效(须含 cross_check.json / benchmark_meta.json /
  bundle_meta.json 之一,合成测试 bundle 不触发);
- 合约数上限(默认 60,超出跳过 —— 大池子重建耗时不适合同步等待);
- 单文件锁防并发重建,锁超时(10 分钟)自动失效;
- 重建失败(网络/限流)→ 返回 None 并告警,回测按原语义给出可读错误。
"""

from __future__ import annotations

import json
import os
import shutil
import time
import warnings
from datetime import datetime
from pathlib import Path

import numpy as np

from aquant.data.instruments import load_instruments

__all__ = [
    "bundle_calendar",
    "ensure_coverage",
    "prune_stale_cache",
    "MAX_AUTO_INSTRUMENTS",
]

# 自动扩展安全阀
MAX_AUTO_INSTRUMENTS = 60
DEFAULT_RETENTION_DAYS = 90
LOCK_STALE_SECONDS = 600  # 锁文件超过 10 分钟视为残留,自动接管

_ENV_FLAG = "AQUANT_BUNDLE_AUTO_EXTEND"


def auto_extend_enabled() -> bool:
    """AQUANT_BUNDLE_AUTO_EXTEND=0 时关闭自动扩展(默认开)。"""
    return os.environ.get(_ENV_FLAG, "1") != "0"


def _is_real_bundle(bundle: Path) -> bool:
    """真实构建标记:三种元文件任一存在(合成测试 bundle 均没有)。"""
    return any(
        (bundle / name).exists()
        for name in ("cross_check.json", "benchmark_meta.json", "bundle_meta.json")
    )


def bundle_calendar(bundle_path: str | Path) -> tuple[str, str] | None:
    """bundle 交易日历范围('YYYY-MM-DD', 'YYYY-MM-DD');不完整/缺失 → None。"""
    bundle = Path(bundle_path)
    cal_file = bundle / "trading_dates.npy"
    if not cal_file.exists() or not (bundle / "instruments.pk").exists():
        return None
    try:
        cal = np.load(cal_file)
        if len(cal) == 0:
            return None
        ymd = lambda v: f"{int(v):08d}"[:4] + "-" + f"{int(v):08d}"[4:6] + "-" + f"{int(v):08d}"[6:]
        return ymd(cal.min()), ymd(cal.max())
    except Exception:
        return None


class _FileLock:
    """单文件互斥锁(O_CREAT|O_EXCL),上下文管理;等待期间轮询,残留锁自动接管。

    等待上限须大于一次重建的耗时(并发请求等待锁释放后经"双检"直接复用新区间)。
    """

    WAIT_TIMEOUT_SECONDS = 240

    def __init__(self, path: Path, stale_seconds: int = LOCK_STALE_SECONDS) -> None:
        self.path = path
        self.stale_seconds = stale_seconds
        self._fd: int | None = None

    def __enter__(self) -> "_FileLock":
        deadline = time.time() + self.WAIT_TIMEOUT_SECONDS
        while True:
            try:
                self._fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self._fd, str(time.time()).encode())
                return self
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > self.stale_seconds:
                        self.path.unlink()  # 残留锁:接管
                        continue
                except FileNotFoundError:
                    continue
                if time.time() > deadline:
                    raise TimeoutError(f"bundle 扩展锁等待超时(>240s):{self.path}")
                time.sleep(0.5)

    def __exit__(self, *exc) -> None:
        if self._fd is not None:
            os.close(self._fd)
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass


def ensure_coverage(
    bundle_path: str | Path,
    start: str,
    end: str,
    *,
    max_span_days: int = 3660,
    cache_retention_days: int = DEFAULT_RETENTION_DAYS,
) -> dict | None:
    """确保 bundle 日历覆盖 [start, end];不覆盖时自动重建扩展。

    :returns: 扩展信息 dict(extended/prior/now/instruments/elapsed_seconds),
              已覆盖或不需要扩展时返回 None
    :raises: 重建失败不抛(告警并返回 None),由回测按原语义给出可读错误;
             但锁等待超时等"并发异常"向上抛(调用方可决定是否重试)。
    """
    bundle = Path(bundle_path)
    cal = bundle_calendar(bundle)
    if cal is None:
        return None  # bundle 缺失/不完整:交给 run_backtest 的既有错误语义
    bundle_start, bundle_end = cal
    if start >= bundle_start and end <= bundle_end:
        return None  # 已覆盖

    if not auto_extend_enabled():
        return None
    if not _is_real_bundle(bundle):
        return None  # 合成/测试 bundle 不自动扩展

    instruments = load_instruments(bundle)
    if len(instruments) == 0 or len(instruments) > MAX_AUTO_INSTRUMENTS:
        return None  # 空表或超上限(大池子重建耗时长,提示手动构建)

    # 扩展而非收缩:目标区间 = 请求区间 ∪ 现有区间
    new_start, new_end = min(start, bundle_start), max(end, bundle_end)
    try:
        span = (datetime.strptime(new_end, "%Y-%m-%d") - datetime.strptime(new_start, "%Y-%m-%d")).days
    except ValueError:
        return None
    if span > max_span_days:
        return None  # 区间跨度超限,提示手动构建

    t0 = time.time()
    with _FileLock(bundle.with_name(bundle.name + ".extend.lock")):
        # 双检:并发场景下别的请求可能已完成扩展
        cal2 = bundle_calendar(bundle)
        if cal2 and start >= cal2[0] and end <= cal2[1]:
            return None

        new_dir = bundle.with_name(bundle.name + f".ext-{int(time.time())}")
        old_dir = bundle.with_name(bundle.name + ".old")
        try:
            _rebuild_for_coverage(bundle, new_dir, new_start, new_end)
            if not bundle_calendar(new_dir):
                raise RuntimeError("重建产物校验失败(缺 trading_dates/instruments)")
            # 原子换目录:旧目录改名备份 → 新目录就位 → 失败则回滚
            if old_dir.exists():
                shutil.rmtree(old_dir, ignore_errors=True)
            os.rename(bundle, old_dir)
            try:
                os.rename(new_dir, bundle)
            except OSError:
                os.rename(old_dir, bundle)  # 回滚:保证 bundle 始终在位
                raise
        except Exception as exc:  # noqa: BLE001 重建失败降级:告警后按原语义报错
            warnings.warn(f"bundle 自动扩展失败(回测将按原区间语义报错):{exc}", stacklevel=2)
            return None
        finally:
            if new_dir.exists():
                shutil.rmtree(new_dir, ignore_errors=True)
        if old_dir.exists():
            shutil.rmtree(old_dir, ignore_errors=True)  # 旧数据删除(可再生成)

    # 缓存保留策略:长期未访问的 K 线缓存一并清理
    pruned = prune_stale_cache(
        Path.home() / ".aquant" / "cache" / "duckdb", cache_retention_days
    )
    return {
        "extended": True,
        "prior": {"start": bundle_start, "end": bundle_end},
        "now": {"start": new_start, "end": new_end},
        "instruments": len(instruments),
        "cache_oids_removed": pruned,
        "elapsed_seconds": round(time.time() - t0, 1),
    }


def _rebuild_for_coverage(
    bundle: Path, out_dir: Path, start: str, end: str
) -> None:
    """按既有合约清单重建 bundle 到 out_dir(测试可 monkeypatch 替身)。

    CS 用 BaostockSource(主)+ AKShare(跨源比价);ETF 用 EtfSource;
    重建完成后重注入 bundle 内登记的 INDX 真实指数行情。
    """
    from aquant.data.bundle import build_bundle

    instruments = load_instruments(bundle)
    cs_codes = [oid for oid, i in instruments.items() if i.get("type") == "CS"]
    etf_codes = [oid for oid, i in instruments.items() if i.get("type") == "ETF"]

    sources = []
    if cs_codes:
        from aquant.data.baostock_source import BaostockSource

        # 自动扩展只走 Baostock 主力源:同步等待场景下 AKShare 跨源比价会把
        # 重建拖到分钟级(限流+重试);原构建的跨源结论见 cross_check.json
        # (Baostock 为既有胜出源,直接沿用)。
        sources.append(BaostockSource(cs_codes, start, end))
    if etf_codes:
        from aquant.data.etf_source import EtfSource

        sources.append(EtfSource(codes=etf_codes, start=start, end=end, names={}))
    if not sources:
        raise RuntimeError("bundle 内无可重建合约(CS/ETF 均为空)")

    selected = sources[0]
    cache_key = f"autoextend_{selected.oids if hasattr(selected, 'oids') else len(cs_codes)}_{start}_{end}"
    try:
        from aquant.data.duckdb_cache import DuckDBCache

        selected = DuckDBCache(
            selected,
            Path.home() / ".aquant" / "cache" / "duckdb",
            cache_key,
            max_size_mb=512.0,
        )
    except Exception:
        pass  # 缓存不可用时直连数据源

    build_bundle([selected], out_dir, fill_missing_factors=True)
    if hasattr(selected, "close"):
        try:
            selected.close()
        except Exception:
            pass

    # 重注入真实基准指数(重建后 indexes.h5 只有合成回退)
    index_oids = [oid for oid, i in instruments.items() if i.get("type") == "INDX"]
    if index_oids:
        from aquant.data.benchmark import fetch_baostock_index, inject_benchmark

        frames = {}
        for oid in index_oids:
            try:
                frames[oid] = fetch_baostock_index(oid, start, end)
            except Exception:
                continue  # 单只失败不阻塞;缺的指数回退合成形态
        if frames:
            inject_benchmark(out_dir, frames)

    (out_dir / "bundle_meta.json").write_text(
        json.dumps(
            {
                "rebuilt_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "reason": "auto_extend",
                "start": start,
                "end": end,
                "instruments": len(instruments),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def prune_stale_cache(cache_dir: str | Path, retention_days: int = DEFAULT_RETENTION_DAYS) -> int:
    """按最近访问时间清理 DuckDB K 线缓存:超过 retention_days 未访问的 oid 全量删除。

    :returns: 清理掉的 oid 数(缓存目录不存在/无过期 → 0)
    """
    import duckdb
    import pandas as pd

    cache_dir = Path(cache_dir)
    if not cache_dir.exists() or retention_days <= 0:
        return 0
    removed = 0
    for db_file in sorted(cache_dir.glob("*.duckdb")):
        try:
            con = duckdb.connect(str(db_file))
            try:
                rows = con.execute(
                    "SELECT oid, MAX(last_access) AS la FROM access GROUP BY oid"
                ).fetchall()
                if not rows:
                    continue
                newest = max(la for _, la in rows if la is not None) if any(la for _, la in rows) else None
                if newest is None:
                    continue
                cutoff = (pd.Timestamp(newest) - pd.Timedelta(days=retention_days)).to_pydatetime()
                stale = [oid for oid, la in rows if la is None or pd.Timestamp(la).to_pydatetime() < cutoff]
                if not stale:
                    continue
                # 只删除实际存在的表(缓存文件可能只建了部分表)
                tables = {
                    r[0] for r in con.execute(
                        "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
                    ).fetchall()
                }
                for oid in stale:
                    for table in ("daily_raw", "adjust_factor", "json_cache", "access"):
                        if table in tables:
                            con.execute(f"DELETE FROM {table} WHERE oid=?", [oid])
                removed += len(stale)
                con.execute("CHECKPOINT")
            finally:
                con.close()
        except Exception:
            continue  # 单文件损坏/占用不阻塞整体清理
    return removed
