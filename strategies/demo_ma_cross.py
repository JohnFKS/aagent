"""合规示例策略:双均线择时(单一标的,低换手)。

符合平台四层校验的写法要点:
- 信号只用 history_bars 截至当前 bar 的数据(无未来函数);
- 不直接访问外部数据源(统一走 bundle 口径);
- 不假设零成本:平台会注入 0.2% 滑点、万八佣金、千一印花、成交量限制;
- 不做当日买卖回转(T+1 下本来也无法成交)。
"""

from rqalpha.const import POSITION_DIRECTION


def init(context):
    context.s1 = "000001.XSHE"
    context.fast = 5
    context.slow = 20


def handle_bar(context, bar_dict):
    prices = history_bars(context.s1, context.slow + 2, "1d", "close")
    if prices is None or len(prices) < context.slow:
        return
    ma_fast = prices[-context.fast:].mean()
    ma_slow = prices[-context.slow:].mean()

    pos = context.portfolio.get_position(context.s1, POSITION_DIRECTION.LONG)
    if ma_fast > ma_slow and pos.quantity == 0:
        # 目标仓位 95%(留现金缓冲),下单买入
        order_target_percent(context.s1, 0.95)
    elif ma_fast < ma_slow and pos.quantity > 0:
        order_target_percent(context.s1, 0.0)
