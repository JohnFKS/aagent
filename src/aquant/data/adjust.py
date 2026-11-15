"""Layer 2:统一复权 —— hfq/raw 因子序列 → RQAlpha ex_cum_factor 结构化数组。

口径约定(见 docs/design.md ADR-1):
- bundle 存真实价(不复权),复权信息只存 `ex_cum_factor.h5`;
- hfq_price = raw_price × ex_cum_factor;factor 单调不减,在除权除息日(因子变化点)跳变;
- RQAlpha 查询历史 bar 时按 `start_date` 分段应用因子,实现前复权/后复权。

`ex_cum_factor_from` 把逐日因子序列压缩为"因子变化点分段表":
- 首条记录:start_date = 数据首日(14 位 YYYYMMDD000000,与 rqalpha bars['datetime'] 同格式),
  ex_cum_factor = 因子首值;
- 之后每遇到因子变化(除权除息日),生成一条 (该日, 新因子) 记录。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = ["EX_CUM_FACTOR_DTYPE", "date_to_int14", "ex_cum_factor_from"]

# 与 rqalpha SimpleFactorStore(ex_cum_factor.h5)使用的字段一致
EX_CUM_FACTOR_DTYPE = np.dtype([("start_date", np.int64), ("ex_cum_factor", np.float64)])


def date_to_int14(d) -> np.int64:
    """pandas.Timestamp / datetime.date → 14 位 int(YYYYMMDD000000)。

    与 rqalpha.utils.datetime_func.convert_dt_to_int 对零点时刻的结果一致,
    可直接与 bars['datetime'] 比较。
    """
    ts = pd.Timestamp(d)
    return np.int64(
        ts.year * 10_000_000_000 + ts.month * 100_000_000 + ts.day * 1_000_000
    )


def _factor_change_points(factor: pd.Series, min_jump: float = 0.002) -> list[int]:
    """因子变化点(除权除息日)位置。

    真实数据的因子由 hfq/raw 两列舍入价相除得到,无事件期间存在 ~1e-4 级噪声;
    因此"事件"定义为因子相对当前段基准**上升 ≥ min_jump**(默认 0.2%,
    A股分红/送股事件的因子跳变通常 ≥0.5%),事件之间取段首因子(运行最大值),
    保证结果单调不减;段内 ±0.02% 的口径抖动被吸收。
    """
    values = factor.to_numpy(dtype=np.float64)
    if len(values) == 0:
        return []
    events = [0]
    for i in range(1, len(values)):
        if values[i] >= values[events[-1]] * (1 + min_jump):
            events.append(i)
    return events[1:]


def ex_cum_factor_from(raw_df: pd.DataFrame, factor_series: pd.Series) -> np.ndarray:
    """由真实价日线与逐日累计复权因子,生成 rqalpha ex_cum_factor 分段表。

    :param raw_df: 该合约真实价日线(升序,DatetimeIndex);仅用于确定日期范围,
        日期与 factor_series 对齐(以 raw_df 索引为准)。
    :param factor_series: 累计 hfq 因子(hfq = raw × factor),DatetimeIndex 升序、float64。
    :returns: 结构化数组,字段 start_date(int64,14 位 YYYYMMDD000000)+ ex_cum_factor(float64)。
        首条记录 = (数据首日, 因子首值);其后每条 = (因子事件日, 事件后因子)。

    异常:若因子相对运行最大值**下降超过 1%**(真实口径错误,如混用前/后复权),
    直接拒绝——这是 Layer 2 的数据质量闸门。
    """
    if raw_df.empty:
        return np.empty(0, dtype=EX_CUM_FACTOR_DTYPE)

    # 因子序列按 raw_df 的索引对齐(容许 factor 索引为 raw 索引的子集/超集)
    factor = factor_series.reindex(raw_df.index)
    if factor.isna().any():
        missing = factor[factor.isna()].index[:3].tolist()
        raise ValueError(
            f"factor_series 与 raw_df 日期未对齐,缺失 {len(factor[factor.isna()])} 天(如 {missing})"
        )

    values = factor.to_numpy(dtype=np.float64)
    if len(values) > 0 and values[0] <= 0:
        raise ValueError(f"复权因子必须为正数,首值 {values[0]!r}")

    # 数据质量闸门:真实方向错误(下降 >1%)不允许进入 bundle
    running_max = np.maximum.accumulate(values)
    if len(values) > 1 and np.any(values / running_max < 0.99):
        bad = int(np.argmin(values / running_max))
        raise ValueError(
            f"复权因子出现显著下降(第 {bad} 日 {values[bad]:.6f} vs 峰值 {running_max[bad]:.6f}):"
            "疑似前/后复权混用或数据方向错误,请检查数据源"
        )

    positions = [0] + _factor_change_points(factor)
    out = np.empty(len(positions), dtype=EX_CUM_FACTOR_DTYPE)
    out["start_date"] = [date_to_int14(raw_df.index[p]) for p in positions]
    out["ex_cum_factor"] = values[positions]
    return out
