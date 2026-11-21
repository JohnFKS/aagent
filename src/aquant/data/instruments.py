"""bundle 合约查询:基准 label 解析与基准 oid 预校验。

背景:平台允许任意 bundle 内合约(个股/ETF/指数)作为回测基准。指数 oid 的
中文名来自内置映射(上证指数/沪深300…);个股/ETF 的名称从 bundle 的
instruments.pk(pickle list[dict])读取。两个用途:
1. API 响应的 ``benchmark_label``(图例展示中文名而不是裸 oid);
2. 回测前的基准 oid 预校验:不在 bundle 的 oid 直接给可读中文 400,
   而不是让 rqalpha 子进程报"unknown order_book_id"(对 LLM/用户不友好)。

读文件很小(几千合约约几十 KB),按 (路径, mtime) 做 lru 缓存;bundle 重建后
mtime 变化自动失效。任何读取异常都降级为空表(名称回退 oid、校验放行交给
runner 的兜底报错),不阻塞主流程。
"""

from __future__ import annotations

import pickle
from pathlib import Path

from aquant.data.benchmark import DEFAULT_BENCHMARK_LABELS

__all__ = ["load_instruments", "instrument_symbol", "benchmark_label", "benchmark_exists"]


def load_instruments(bundle_path: str | Path) -> dict[str, dict]:
    """读 bundle instruments.pk → {order_book_id: {symbol, type, ...}}(mtime 缓存)。"""
    path = Path(bundle_path) / "instruments.pk"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    cached = _CACHE.get(path)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    try:
        with open(path, "rb") as f:
            raw = pickle.load(f)
        table = {i["order_book_id"]: i for i in raw if isinstance(i, dict) and i.get("order_book_id")}
    except Exception:
        table = {}
    _CACHE[path] = (mtime, table)
    return table


_CACHE: dict[Path, tuple[float, dict[str, dict]]] = {}


def instrument_symbol(oid: str, bundle_path: str | Path) -> str | None:
    """bundle 内合约的中文名(无则 None)。"""
    inst = load_instruments(bundle_path).get(oid)
    return str(inst["symbol"]) if inst else None


def benchmark_label(oid: str | None, bundle_path: str | Path) -> str:
    """基准图例名:指数内置映射 → bundle 合约名 → 回退裸 oid。"""
    if not oid:
        return ""
    known = DEFAULT_BENCHMARK_LABELS.get(oid)
    if known:
        return known
    return instrument_symbol(oid, bundle_path) or oid


def benchmark_exists(oid: str, bundle_path: str | Path) -> bool:
    """基准 oid 是否为 bundle 已注册合约(指数内置 oid 恒存在)。"""
    if oid in DEFAULT_BENCHMARK_LABELS:
        return True
    return oid in load_instruments(bundle_path)
