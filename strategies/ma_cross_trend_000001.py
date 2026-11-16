"""编辑版策略:双均线 + 60 日趋势过滤(单一标的,低换手)。

在 demo_ma_cross 基础上编辑:
- 新增 60 日趋势线过滤,只在收盘价站上长期均线时开仓;
- 目标仓位由 95% 降至 90%,留更多现金缓冲;
- 信号仍只用 history_bars 截至当前 bar 的 ndarray,无未来函数、无外部数据。
"""

from rqalpha.const import POSITION_DIRECTION


def init(context):
    context.s1 = "000001.XSHE"
    context.fast = 5
    context.slow = 20
    context.trend_window = 60


def handle_bar(context, bar_dict):
    prices = history_bars(context.s1, context.trend_window + 2, "1d", "close")
    if prices is None or len(prices) < context.trend_window:
        return
    ma_fast = prices[-context.fast:].mean()
    ma_slow = prices[-context.slow:].mean()
    ma_trend = prices[-context.trend_window:].mean()
    last_price = prices[-1]

    pos = context.portfolio.get_position(context.s1, POSITION_DIRECTION.LONG)
    if ma_fast > ma_slow and last_price > ma_trend and pos.quantity == 0:
        order_target_percent(context.s1, 0.90)
    elif ma_fast < ma_slow and pos.quantity > 0:
        order_target_percent(context.s1, 0.0)
