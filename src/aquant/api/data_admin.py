"""本地数据管理 API(数据池/缓存盘点、预下载、删除)。

数据管理界面(前端「数据管理」Tab)与 MCP/CLI 的后端:
- GET  /api/data/overview — bundle 数据池盘点(标的明细/文件明细/容量/缓存);
- POST /api/data/fetch    — 预下载:把请求标的在线拉取并加入数据池
                            (ensure_coverage 标自适应,已存在的只补区间);
- POST /api/data/instruments/remove — 从数据池删除标的(重建保持覆盖区间,
                            台账/缓存同步清理;基准指数不可删);
- POST /api/data/cache/clear — 清空 K 线 DuckDB 缓存(不影响 bundle)。

所有端点 readwrite(写操作影响本地数据),token 闸门同既有语义。
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from aquant.compat import version_report
from aquant.config.schema import default_bundle_path
from aquant.data.extend import (
    InstrumentUnavailableError,
    bundle_calendar,
    ensure_coverage,
    remove_instruments,
)
from aquant.data.instruments import load_instruments

router = APIRouter(prefix="/api/data", tags=["data-management"])

_OID_FORMAT_HINT = "标的代码格式应为 6 位数字 + 交易所后缀(如 600519.XSHG),多个用逗号分隔"


class FetchRequest(BaseModel):
    """预下载请求:把标的在线拉取并加入本地数据池。"""

    oids: list[str] = Field(min_length=1, description="要预下载的标的代码(可多个)")
    start: str | None = Field(default=None, description="起始日 YYYY-MM-DD(缺省沿用数据池起点)")
    end: str | None = Field(default=None, description="结束日 YYYY-MM-DD(缺省取今天,拉取最新)")


class RemoveRequest(BaseModel):
    """删除请求:从数据池移除标的。"""

    oids: list[str] = Field(min_length=1, description="要删除的标的代码(可多个)")


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _instrument_data_days(bundle: Path, oid: str, inst_type: str) -> dict[str, Any]:
    """单个标的的行情行数与首末日期(直读对应 h5)。"""
    import h5py

    file_map = {"CS": "stocks.h5", "ETF": "funds.h5", "INDX": "indexes.h5"}
    filename = file_map.get(inst_type, "stocks.h5")
    path = bundle / filename
    if not path.exists():
        return {"rows": 0, "first": None, "last": None}
    try:
        with h5py.File(path, "r") as h5:
            if oid not in h5:
                return {"rows": 0, "first": None, "last": None}
            data = h5[oid][:]
            if len(data) == 0:
                return {"rows": 0, "first": None, "last": None}
            ymd = lambda dt: f"{int(dt) // 1_000_000:08d}"[:4] + "-" + \
                f"{int(dt) // 1_000_000:08d}"[4:6] + "-" + f"{int(dt) // 1_000_000:08d}"[6:]
            return {
                "rows": int(len(data)),
                "first": ymd(data["datetime"][0]),
                "last": ymd(data["datetime"][-1]),
            }
    except Exception:
        return {"rows": 0, "first": None, "last": None}


def _bundle_inventory() -> dict[str, Any]:
    bundle = default_bundle_path()
    insts = load_instruments(bundle)
    cal = bundle_calendar(bundle)

    rows: list[dict[str, Any]] = []
    total_rows = 0
    for oid, inst in sorted(insts.items()):
        info = _instrument_data_days(bundle, oid, str(inst.get("type", "")))
        total_rows += info["rows"]
        rows.append({
            "oid": oid,
            "symbol": inst.get("symbol", ""),
            "type": inst.get("type", ""),
            "listed_date": inst.get("listed_date", ""),
            "rows": info["rows"],
            "first": info["first"],
            "last": info["last"],
        })

    files = []
    bundle_size = 0
    if bundle.exists():
        for f in sorted(bundle.iterdir()):
            if f.is_file():
                size = _file_size(f)
                bundle_size += size
                files.append({"name": f.name, "size_bytes": size})
    cache_dir = Path.home() / ".aquant" / "cache" / "duckdb"
    cache_files = []
    cache_size = 0
    if cache_dir.exists():
        for f in sorted(cache_dir.glob("*.duckdb")):
            size = _file_size(f)
            cache_size += size
            cache_files.append({"name": f.name, "size_bytes": size})

    return {
        "bundle_path": str(bundle),
        "calendar": {"start": cal[0] if cal else None, "end": cal[1] if cal else None,
                     "days": len(cal) if cal else 0},
        "instruments": rows,
        "counts": {
            "total": len(rows),
            "CS": sum(1 for r in rows if r["type"] == "CS"),
            "ETF": sum(1 for r in rows if r["type"] == "ETF"),
            "INDX": sum(1 for r in rows if r["type"] == "INDX"),
        },
        "total_rows": total_rows,
        "files": files,
        "bundle_size_bytes": bundle_size,
        "cache_files": cache_files,
        "cache_size_bytes": cache_size,
        "rqalpha": version_report(),
    }


@router.get("/overview")
def data_overview() -> dict[str, Any]:
    """数据池/文件/缓存盘点(数据管理界面主数据源)。"""
    return _bundle_inventory()


@router.post("/fetch")
def data_fetch(req: FetchRequest, request: Request) -> dict[str, Any]:
    """预下载:在线拉取标的行情并加入数据池(标自适应,幂等)。"""
    import re

    oids = []
    for raw in req.oids:
        oid = str(raw).strip().upper()
        if not oid:
            continue
        if not re.fullmatch(r"\d{6}\.(XSHE|XSHG|BJ)", oid):
            return {"ok": False, "error": f"{_OID_FORMAT_HINT}(收到:{raw})"}
        oids.append(oid)
    if not oids:
        return {"ok": False, "error": _OID_FORMAT_HINT}

    bundle = default_bundle_path()
    cal = bundle_calendar(bundle)
    start = req.start or (cal[0] if cal else "2022-01-04")
    end = req.end or datetime.now().strftime("%Y-%m-%d")
    try:
        info = ensure_coverage(bundle, start, end, extra_oids=set(oids))
    except InstrumentUnavailableError as exc:
        return {"ok": False, "error": str(exc)}
    except TimeoutError as exc:
        return {"ok": False, "error": f"并发锁等待超时,请稍后重试:{exc}"}

    if info is None:
        added = []
        evicted = []
    else:
        added = info.get("added", [])
        evicted = info.get("evicted", [])
    insts = load_instruments(bundle)
    fetched_rows = []
    for oid in oids:
        inst = insts.get(oid)
        if inst is None:
            continue
        info_rows = _instrument_data_days(bundle, oid, str(inst.get("type", "")))
        fetched_rows.append({"oid": oid, "symbol": inst.get("symbol", ""), **info_rows})

    return {
        "ok": True,
        "requested": oids,
        "added": added,
        "evicted": evicted,
        "already_in_pool": [o for o in oids if o in insts and o not in added],
        "fetched": fetched_rows,
        "calendar": {"start": (cal2[0] if (cal2 := bundle_calendar(bundle)) else None),
                     "end": (cal2[1] if cal2 else None)},
        "elapsed_seconds": None,
    }


@router.post("/instruments/remove")
def data_instruments_remove(req: RemoveRequest) -> dict[str, Any]:
    """从数据池删除标的(重建保持覆盖区间;基准指数不可删)。"""
    try:
        result = remove_instruments(default_bundle_path(), set(req.oids))
        return {"ok": True, **result}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}


@router.post("/cache/clear")
def data_cache_clear() -> dict[str, Any]:
    """清空 K 线 DuckDB 缓存(不影响 bundle 本体;下次回测自动重建缓存)。"""
    import shutil as _shutil

    cache_dir = Path.home() / ".aquant" / "cache" / "duckdb"
    removed = []
    size = 0
    if cache_dir.exists():
        for f in sorted(cache_dir.glob("*.duckdb")):
            size += _file_size(f)
            try:
                f.unlink()
                removed.append(f.name)
            except OSError:
                pass
        # 清理残留 wal 文件
        for f in sorted(cache_dir.glob("*.wal")):
            try:
                f.unlink()
            except OSError:
                pass
    return {"ok": True, "removed": removed, "freed_bytes": size}
