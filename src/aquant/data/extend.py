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
import re
import shutil
import time
import warnings
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from aquant.data.instruments import load_instruments

__all__ = [
    "bundle_calendar",
    "ensure_coverage",
    "prune_stale_cache",
    "extract_strategy_oids",
    "InstrumentUnavailableError",
    "mark_instruments_used",
    "remove_instruments",
    "MAX_AUTO_INSTRUMENTS",
]

# 策略代码中的 order_book_id(6 位数字 + 交易所后缀)
_OID_RE = re.compile(r"\b\d{6}\.(?:XSHE|XSHG|BJ)\b")


class InstrumentUnavailableError(RuntimeError):
    """策略请求的标的无法提供(不存在/已退市/区间无行情)→ API 层转可读 422。"""


def extract_strategy_oids(code: str) -> set[str]:
    """从策略源码提取 order_book_id 集合(契约要求 oid 以字面量出现在代码中)。"""
    return set(_OID_RE.findall(code or ""))


def mark_instruments_used(bundle_path: str | Path, oids: set[str]) -> None:
    """登记标的使用时间(数据保留策略:长期未用的标的在下次重建时清理)。"""
    bundle = Path(bundle_path)
    ledger_file = bundle / "instrument_usage.json"
    try:
        try:
            ledger: dict[str, str] = json.loads(ledger_file.read_text(encoding="utf-8"))
        except Exception:
            ledger = {}
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for oid in oids or []:
            ledger[oid] = now
        ledger_file.write_text(json.dumps(ledger, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass  # 台账写入失败不阻塞主流程

# 自动扩展安全阀
MAX_AUTO_INSTRUMENTS = 60
DEFAULT_INSTRUMENT_RETENTION_DAYS = 90  # 标的保留:90 天未用自动清理
DEFAULT_RETENTION_DAYS = 90
LOCK_STALE_SECONDS = 600  # 锁文件超过 10 分钟视为残留,自动接管

_ENV_FLAG = "AQUANT_BUNDLE_AUTO_EXTEND"


def auto_extend_enabled() -> bool:
    """AQUANT_BUNDLE_AUTO_EXTEND=0 时关闭自动扩展(默认开)。"""
    return os.environ.get(_ENV_FLAG, "1") != "0"


def _looks_like_index(oid: str) -> bool:
    """指数代码形态:沪 000/880 开头、深 395/399 开头(与个股代码空间区分)。"""
    code = oid.split(".")[0] if "." in oid else oid
    suffix = oid.split(".")[-1].upper() if "." in oid else "XSHG"
    if suffix == "XSHG" and code.startswith(("000", "880")):
        return True
    if suffix == "XSHE" and code.startswith(("395", "399")):
        return True
    return False


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
    extra_oids: set[str] | None = None,
    max_span_days: int = 3660,
    cache_retention_days: int = DEFAULT_RETENTION_DAYS,
    instrument_retention_days: int = DEFAULT_INSTRUMENT_RETENTION_DAYS,
) -> dict | None:
    """确保 bundle 覆盖 [start, end] 与请求标的;缺失时自动在线获取并重建。

    标的自适应(对应"可回测任何正常状态股票"):
    - ``extra_oids`` 中不在 bundle 的标的 → Baostock 在线获取(日线+复权因子+
      停牌/ST+名称),指数型代码(如 000905.XSHG)走指数注入;
      已退市/无行情的标的抛 :class:`InstrumentUnavailableError`(API 层转可读 422);
    - 使用台账(instrument_usage.json):每次请求登记标的最后使用时间;
    - 数据保留:重建时清理**长期未使用**的标的(默认 90 天未用且不在本次请求中;
      基准指数 INDX 永不清理),其行情与缓存随后由 DuckDB 保留策略淘汰。

    :returns: 扩展信息 dict(extended/prior/now/added/evicted/...),
              已覆盖且无缺失时返回 None
    :raises InstrumentUnavailableError: 请求标的不可提供(用户可更换标的);
             锁等待超时等并发异常向上抛。
    """
    bundle = Path(bundle_path)
    cal = bundle_calendar(bundle)
    if cal is None:
        return None  # bundle 缺失/不完整:交给 run_backtest 的既有错误语义
    bundle_start, bundle_end = cal
    instruments = load_instruments(bundle)
    needed = {str(o) for o in (extra_oids or set()) if str(o)}
    missing = {o for o in needed if o not in instruments}
    if (
        not missing
        and start >= bundle_start
        and end <= bundle_end
    ):
        # 完全覆盖:只登记使用台账,无需重建
        mark_instruments_used(bundle, needed)
        return None

    if not auto_extend_enabled():
        return None
    if not _is_real_bundle(bundle):
        return None  # 合成/测试 bundle 不自动扩展
    if instruments and len(instruments) + len(missing) > MAX_AUTO_INSTRUMENTS:
        return None  # 超上限(大池子重建耗时长,提示手动构建)

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
        # 双检:并发场景下别的请求可能已完成扩展(含标的)
        cal2 = bundle_calendar(bundle)
        existing2 = load_instruments(bundle)
        if (cal2 and start >= cal2[0] and end <= cal2[1]
                and needed <= set(existing2)):
            mark_instruments_used(bundle, needed)
            return None

        # 使用台账:迁移缺失条目为当前时间(存量标的宽限期内不受清理影响)
        ledger_file = bundle / "instrument_usage.json"
        try:
            ledger: dict[str, str] = json.loads(ledger_file.read_text(encoding="utf-8"))
        except Exception:
            ledger = {}
        now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for oid in instruments:
            ledger.setdefault(oid, now_iso)
        for oid in needed:
            ledger[oid] = now_iso

        # 数据保留:长期未使用(且不在本次请求、非基准指数)的标的清理
        cutoff = datetime.now() - timedelta(days=instrument_retention_days)
        evicted: list[str] = []
        for oid, inst in instruments.items():
            if inst.get("type") == "INDX" or oid in needed:
                continue
            try:
                last_used = datetime.strptime(ledger.get(oid, now_iso), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            if last_used < cutoff:
                evicted.append(oid)

        # 新标的在线获取与校验:个股/ETF 走 Baostock 逐只预载(退市/无行情 → 可读异常),
        # 指数形态代码走指数注入(Baostock 指数日线)
        new_cs_codes: list[str] = []
        index_frames: dict[str, "pd.DataFrame"] = {}
        if missing:
            from aquant.data.baostock_source import BaostockSource
            from aquant.data.benchmark import fetch_baostock_index

            probe = BaostockSource(sorted(missing), new_start, new_end)
            for oid in sorted(missing):
                if _looks_like_index(oid):
                    try:
                        index_frames[oid] = fetch_baostock_index(oid, new_start, new_end)
                    except Exception as exc:  # noqa: BLE001
                        raise InstrumentUnavailableError(
                            f"基准指数 {oid} 无法提供行情数据:{exc}。请更换基准指数或标的。"
                        ) from exc
                    continue
                try:
                    probe._load(oid)
                except ValueError as exc:
                    raise InstrumentUnavailableError(
                        f"标的 {oid} 无法提供行情数据,无法回测:{exc}。"
                        "请确认代码正确且当前正常上市(已退市/长期停牌的标的无法回测)。"
                    ) from exc
                basic = probe._basics.get(oid) or {}
                out_date = str(basic.get("outDate") or "")
                status = str(basic.get("status") or "1")
                if (out_date and out_date != "0000-00-00") or status == "0":
                    raise InstrumentUnavailableError(
                        f"标的 {oid} 已退市(退出日期 {out_date or '未知'}),无法回测;"
                        "请更换为当前正常上市的标的。"
                    )
                new_cs_codes.append(oid)

        # 重建合约清单 = 存量(剔除闲置清理) ∪ 新增;INDX 指数由注入路径处理
        keep_oids = [
            oid for oid, inst in instruments.items()
            if inst.get("type") != "INDX" and oid not in evicted
        ]
        build_cs = sorted(set(keep_oids) | set(new_cs_codes))

        new_dir = bundle.with_name(bundle.name + f".ext-{int(time.time())}")
        old_dir = bundle.with_name(bundle.name + ".old")
        try:
            # 重建重试一次:Baostock 偶发限流/瞬时故障不至于让首次请求失败
            last_rebuild_error: Exception | None = None
            for attempt in range(2):
                try:
                    _rebuild_for_coverage(
                        bundle, new_dir, new_start, new_end,
                        cs_codes=build_cs, etf_codes=[], index_frames=index_frames,
                    )
                    if not bundle_calendar(new_dir):
                        raise RuntimeError("重建产物校验失败(缺 trading_dates/instruments)")
                    last_rebuild_error = None
                    break
                except InstrumentUnavailableError:
                    raise  # 标的不可提供是确定性问题,重试无意义
                except Exception as exc:  # noqa: BLE001
                    last_rebuild_error = exc
                    if new_dir.exists():
                        shutil.rmtree(new_dir, ignore_errors=True)
                    if attempt == 0:
                        warnings.warn(
                            f"bundle 重建第 1 次失败(将重试):{exc}", stacklevel=2
                        )
            if last_rebuild_error is not None:
                raise last_rebuild_error
            # 原子换目录:旧目录改名备份 → 新目录就位 → 失败则回滚
            if old_dir.exists():
                shutil.rmtree(old_dir, ignore_errors=True)
            os.rename(bundle, old_dir)
            try:
                os.rename(new_dir, bundle)
            except OSError:
                os.rename(old_dir, bundle)  # 回滚:保证 bundle 始终在位
                raise
        except InstrumentUnavailableError:
            raise  # 用户可修复(换标的),原样向上
        except Exception as exc:  # noqa: BLE001 - 重建失败降级:告警后按原语义报错
            warnings.warn(f"bundle 自动扩展失败(回测将按原区间语义报错):{exc}", stacklevel=2)
            return None
        finally:
            if new_dir.exists():
                shutil.rmtree(new_dir, ignore_errors=True)
        if old_dir.exists():
            shutil.rmtree(old_dir, ignore_errors=True)  # 旧数据删除(可再生成)
        mark_instruments_used(bundle, needed | set(keep_oids) | set(new_cs_codes) | set(index_frames))

    # 缓存保留策略:长期未访问的 K 线缓存一并清理
    pruned = prune_stale_cache(
        Path.home() / ".aquant" / "cache" / "duckdb", cache_retention_days
    )
    return {
        "extended": True,
        "prior": {"start": bundle_start, "end": bundle_end},
        "now": {"start": new_start, "end": new_end},
        "instruments": len(instruments),
        "added": sorted(new_cs_codes) + sorted(index_frames),
        "evicted": evicted,
        "cache_oids_removed": pruned,
        "elapsed_seconds": round(time.time() - t0, 1),
    }


def _normalize_listed_dates(bundle_dir: str | Path) -> None:
    """把 bundle 内所有 CS/ETF 合约的 listed_date 统一设为日历首日。

    rqalpha 在 order_target_percent 等下单 API 中检查 listed_at(trading_dt),
    若真实上市日 > 回测起始日,则视为"该日未上市"而拒单(即使 bundle 已有行情)。
    统一设为日历首日消除此限制;实际数据可用性由 bars 行本身控制。
    """
    import pickle as _p

    bundle = Path(bundle_dir)
    cal = bundle_calendar(bundle)
    if cal is None:
        return
    first_day = cal[0]
    with open(bundle / "instruments.pk", "rb") as f:
        instruments = _p.load(f)
    changed = False
    for inst in instruments:
        if inst.get("type") in ("CS", "ETF") and inst.get("listed_date", "") != first_day:
            if inst.get("listed_date", "") and inst["listed_date"] > first_day:
                inst["listed_date"] = first_day
                changed = True
    if changed:
        with open(bundle / "instruments.pk", "wb") as f:
            _p.dump(instruments, f)


def _rebuild_for_coverage(
    bundle: Path,
    out_dir: Path,
    start: str,
    end: str,
    *,
    cs_codes: list[str] | None = None,
    etf_codes: list[str] | None = None,
    index_frames: dict[str, "pd.DataFrame"] | None = None,
) -> None:
    """按给定合约清单重建 bundle 到 out_dir(测试可 monkeypatch 替身)。

    CS/ETF 均走 Baostock 主力源(同步等待场景下 AKShare 跨源比价会把重建拖到
    分钟级);指数行情经 index_frames 注入(benchmark.inject_benchmark)。
    """
    from aquant.data.bundle import build_bundle

    if cs_codes is None:
        instruments = load_instruments(bundle)
        cs_codes = [oid for oid, i in instruments.items() if i.get("type") in ("CS", "ETF")]

    sources = []
    if cs_codes:
        from aquant.data.baostock_source import BaostockSource

        sources.append(BaostockSource(cs_codes, start, end))
    if etf_codes:
        from aquant.data.etf_source import EtfSource

        sources.append(EtfSource(codes=etf_codes, start=start, end=end, names={}))
    if not sources and not index_frames:
        raise RuntimeError("无可重建合约(CS/ETF/指数均为空)")

    selected = sources[0] if sources else None
    if selected is not None:
        # 缓存 key 与代码清单无关(稳定 key):新增标的只拉新 oid,
        # 存量 oid 经 daily_raw 的区间覆盖检查直接命中(零重拉)。
        cache_key = "autoextend"
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
    else:
        # 只有指数注入:以现有合约清单 + 指数重建(避免空 bundle)
        instruments = load_instruments(bundle)
        cs_all = [oid for oid, i in instruments.items() if i.get("type") in ("CS", "ETF")]
        if not cs_all:
            raise RuntimeError("bundle 无 CS/ETF 合约,无法仅凭指数重建")
        from aquant.data.baostock_source import BaostockSource

        build_bundle([BaostockSource(cs_all, start, end)], out_dir, fill_missing_factors=True)

    # 注入真实基准/指数行情(重建后 indexes.h5 只有合成回退;含新增指数)
    if index_frames:
        from aquant.data.benchmark import inject_benchmark

        inject_benchmark(out_dir, index_frames)

    # listed_date 统一设为日历首日:
    # rqalpha 的 order_target_percent 在下单时检查 listed_at(trading_dt),
    # 若 listed_date > 回测起始日(如 2024 年新上市的 ETF),rqalpha 视为"该日未上市"
    # 而拒单。bundle 内行情本身已控制可用性(未上市的日期无 bar 数据),无需在
    # instrument 层面二次过滤 —— 统一设为日历首日使任意标的在整个回测期间均可下单。
    _normalize_listed_dates(out_dir)

    (out_dir / "bundle_meta.json").write_text(
        json.dumps(
            {
                "rebuilt_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "reason": "auto_extend",
                "start": start,
                "end": end,
                "instruments": len(load_instruments(out_dir)),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )



def remove_instruments(
    bundle_path: str | Path,
    oids: set[str] | list[str],
    *,
    instrument_retention_days: int | None = None,
) -> dict:
    """从数据池删除指定标的(重建 bundle 保持既有覆盖区间不变)。

    - 基准指数(INDX)不可删除;
    - 台账与 DuckDB 行情缓存中对应条目一并清理;
    - :raises ValueError: oid 不存在/类型不可删/清理后池为空。
    """
    bundle = Path(bundle_path)
    remove_set = {str(o) for o in oids}
    instruments = load_instruments(bundle)
    unknown = sorted(remove_set - set(instruments))
    if unknown:
        raise ValueError(f"标的不在数据池中:{', '.join(unknown)}")
    index_oids = {o for o in remove_set if instruments[o].get("type") == "INDX"}
    if index_oids:
        raise ValueError(f"基准指数不可删除:{', '.join(sorted(index_oids))}")

    keep = [oid for oid in instruments if oid not in remove_set]
    if not any(load_instruments(bundle)[o].get("type") in ("CS", "ETF") for o in keep):
        raise ValueError("删除后数据池将不含任何可交易标的,拒绝执行。")

    cal = bundle_calendar(bundle)
    new_start, new_end = cal

    t0 = time.time()
    with _FileLock(bundle.with_name(bundle.name + ".extend.lock")):
        new_dir = bundle.with_name(bundle.name + f".ext-{int(time.time())}")
        old_dir = bundle.with_name(bundle.name + ".old")
        try:
            _rebuild_for_coverage(
                bundle, new_dir, new_start, new_end,
                cs_codes=sorted(keep), etf_codes=[], index_frames={},
            )
            if not bundle_calendar(new_dir):
                raise RuntimeError("重建产物校验失败(缺 trading_dates/instruments)")
            if old_dir.exists():
                shutil.rmtree(old_dir, ignore_errors=True)
            os.rename(bundle, old_dir)
            try:
                os.rename(new_dir, bundle)
            except OSError:
                os.rename(old_dir, bundle)
                raise
        except Exception as exc:  # noqa: BLE001
            warnings.warn(f"数据池删除失败(已回滚):{exc}", stacklevel=2)
            raise
        finally:
            if new_dir.exists():
                shutil.rmtree(new_dir, ignore_errors=True)
        if old_dir.exists():
            shutil.rmtree(old_dir, ignore_errors=True)

        # 台账同步清理
        ledger_file = bundle / "instrument_usage.json"
        try:
            ledger = json.loads(ledger_file.read_text(encoding="utf-8"))
            for oid in remove_set:
                ledger.pop(oid, None)
            ledger_file.write_text(json.dumps(ledger, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    # DuckDB 缓存同步清理对应 oid
    removed_cache = 0
    cache_dir = Path.home() / ".aquant" / "cache" / "duckdb"
    import duckdb as _duckdb

    for db_file in sorted(cache_dir.glob("*.duckdb")):
        try:
            con = _duckdb.connect(str(db_file))
            try:
                tables = {r[0] for r in con.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
                ).fetchall()}
                for oid in remove_set:
                    for table in ("daily_raw", "adjust_factor", "json_cache", "access"):
                        if table in tables:
                            con.execute(f"DELETE FROM {table} WHERE oid=?", [oid])
                removed_cache += len(remove_set)
                con.execute("CHECKPOINT")
            finally:
                con.close()
        except Exception:
            continue

    return {
        "removed": sorted(remove_set),
        "pool_size": len(load_instruments(bundle)),
        "cache_oids_removed": removed_cache,
        "elapsed_seconds": round(time.time() - t0, 1),
    }


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
