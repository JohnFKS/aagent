"""K 线行情读取:GET /api/kline 的数据层(docs/design.md §6"K 线图"路线项)。

直接读 RQAlpha bundle 的 stocks.h5(h5py 结构化数组,字段 datetime(int64
YYYYMMDD000000)/open/close/high/low/volume/total_turnover/limit_up/limit_down),
输出 A 股习惯的**真实价**(不复权)日线 K 线——与回测撮合口径一致(ADR #1),
展示层不做前/后复权换算(策略信号与撮合的复权语义由 runner 负责)。

约束与容错(全部映射为 HTTP 400,worker 不因行情查询挂掉):
- bundle / stocks.h5 缺失 → FileNotFoundError(API 层转 BUNDLE_GUIDANCE 构建指引,
  与 /api/chat 同型);
- oid 不在 bundle → KlineError,文案附带可用 oid 提示(前几个);
- h5 打开/读取异常(文件损坏、被占用等 OSError 系)→ KlineError;
- start/end 只接受 'YYYY-MM-DD';非法格式 → KlineError;
- 单次响应最多 MAX_POINTS=500 根:超出按等距抽样(保留首末两根)。

datetime 为 14 位整数(YYYYMMDD000000),取前 8 位转 'YYYY-MM-DD' 纯日期字符串,
与前端 lightweight-charts 的 UTC 口径一致(日期不做本地时区换算,无偏移)。
"""

from __future__ import annotations

import re
from typing import Any

import h5py
import numpy as np

# 经 schema 模块属性调用(而非 from-import),便于测试 monkeypatch
# aquant.config.schema.default_bundle_path 指向不存在目录做容错用例
from aquant.config import schema

MAX_POINTS = 500

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class KlineError(Exception):
    """K 线查询失败(参数/数据问题,用户可修复)→ API 层统一转 400。"""


def _to_int8(day: str) -> int:
    """'YYYY-MM-DD' → int YYYYMMDD(与 datetime 字段前 8 位比较)。"""
    return int(day.replace("-", ""))


def load_kline(oid: str, start: str | None, end: str | None) -> list[dict[str, Any]]:
    """从 bundle stocks.h5 读取单个合约的日线 K 线(真实价)。

    :param oid: 合约 order_book_id,如 000001.XSHE
    :param start/end: 可选过滤区间(含端点,'YYYY-MM-DD')
    :returns: candles 列表 [{date, open, close, high, low, volume}],日期升序;
              区间内无行情(停牌/越界)时返回空列表
    :raises FileNotFoundError: bundle / stocks.h5 缺失(API 层转 BUNDLE_GUIDANCE)
    :raises KlineError: oid 不在 bundle / h5 读取失败 / 日期格式非法
    """
    oid = (oid or "").strip()
    if not oid:
        raise KlineError("oid 不能为空:请提供合约 order_book_id(如 000001.XSHE)。")
    for name, value in (("start", start), ("end", end)):
        if value and not _DATE_RE.match(value.strip()):
            raise KlineError(f"{name} 参数必须是 YYYY-MM-DD 格式(收到:{value!r})。")

    stocks_h5 = schema.default_bundle_path() / "stocks.h5"
    if not stocks_h5.exists():
        # 与 /api/chat 同型:缺失属于用户可修复的环境问题,给 400 + 构建指引
        raise FileNotFoundError(f"数据 bundle 不存在:{stocks_h5}")

    try:
        with h5py.File(stocks_h5, "r") as h5:
            if oid not in h5:
                available = sorted(h5.keys())[:5]
                hint = "、".join(available) if available else "(bundle 内无股票数据)"
                raise KlineError(f"合约不在数据 bundle 中(可用:{hint})。")
            data = h5[oid][:]
    except KlineError:
        raise
    except Exception as exc:  # h5 损坏/占用等 → 容错 400 而不是 500
        raise KlineError(f"K 线数据读取失败({stocks_h5}):{exc}") from exc

    if data.size == 0:
        return []

    # datetime 前 8 位 = YYYYMMDD;按含端点闭区间过滤(停牌日 bundle 本就无行)
    dates8 = (data["datetime"] // 1_000_000).astype(np.int64)
    lo = _to_int8(start.strip()) if start else 0
    hi = _to_int8(end.strip()) if end else 99999999
    rows = data[(dates8 >= lo) & (dates8 <= hi)]

    if rows.size == 0:
        return []

    # 超 500 根等距抽样:linspace 自带首末端点,round→unique 后仍严格递增,
    # 显式回填首末下标兜底(浮点舍入极端情况下也不丢首尾)
    if rows.size > MAX_POINTS:
        idx = np.unique(np.linspace(0, rows.size - 1, MAX_POINTS).round().astype(np.int64))
        idx[0] = 0
        idx[-1] = rows.size - 1
        rows = rows[idx]

    candles: list[dict[str, Any]] = []
    for row in rows:
        yyyymmdd = str(int(row["datetime"]) // 1_000_000).zfill(8)
        candles.append(
            {
                "date": f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}",
                "open": float(row["open"]),
                "close": float(row["close"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "volume": float(row["volume"]),
            }
        )
    return candles
