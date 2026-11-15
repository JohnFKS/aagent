"""坏示例 1:未来函数(.shift(-1))。

用"明天的收盘价"来决定今天的交易 —— 回测神策略,实盘必死。
平台 Layer 1 的 ast.shift_negative 规则会以 ERROR 拦截本策略。
"""


def init(context):
    context.s1 = "000001.XSHE"


def handle_bar(context, bar_dict):
    import pandas as pd

    prices = history_bars(context.s1, 30, "1d", "close")
    s = pd.Series(prices)
    # 明天的收盘价(未来数据!)
    tomorrow = s.shift(-1).iloc[-1]
    if not pd.isna(tomorrow) and tomorrow > prices[-1]:
        order_target_percent(context.s1, 0.95)
