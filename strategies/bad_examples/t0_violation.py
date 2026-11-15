"""坏示例 2:T+0 回转 + 零滑点配置。

策略在同一根 bar 内先卖后买同一标的(依赖可回转的撮合假设),
并试图通过配置覆盖关闭滑点 —— 两处都会被 Layer 1 拦截:
- cfg.zero_slippage(ERROR):用户 mod 覆盖 slippage=0
- 本策略再叠加 cfg.stock_t1_disabled 展示 T+1 被关闭时的拦截
"""

MOD_OVERRIDES = {
    "sys_simulation": {"slippage": 0.0},
    "sys_accounts": {"stock_t1": False},
}


def init(context):
    context.s1 = "000001.XSHE"


def handle_bar(context, bar_dict):
    from rqalpha.const import POSITION_DIRECTION

    pos = context.portfolio.get_position(context.s1, POSITION_DIRECTION.LONG)
    if pos.quantity > 0:
        # T+1 规则下当日买入的持仓不可卖出;先卖后买同 bar 回转是 T+0 思维
        order_shares(context.s1, -pos.quantity)
        order_shares(context.s1, pos.quantity)
