"""坏示例 3:绕过统一数据口径(策略内直接拉外部数据 + 墙钟时间)。

在策略里 requests 拉行情、用 datetime.now() 判断时点:
- 数据口径失控(无法保证无未来数据、不可复现)
- 墙钟时间让回测结果依赖运行时刻
平台 Layer 1 的 ast.external_data / ast.wall_clock 规则以 ERROR 拦截。
"""


def init(context):
    context.s1 = "000001.XSHE"


def handle_bar(context, bar_dict):
    import datetime

    import requests

    now = datetime.datetime.now()  # 墙钟时间:不可复现
    resp = requests.get("http://example.com/quote", timeout=3)  # 外部数据:绕过平台口径
    price = resp.json().get("price", 0)

    if now.weekday() < 5 and price > 0:
        order_target_percent(context.s1, 0.5)
