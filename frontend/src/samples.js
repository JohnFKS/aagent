// 预置示例与默认值(方便测试演示)

// 回测区间默认值(需求指定)
export const DEFAULT_START = '2023-01-03';
export const DEFAULT_END = '2024-06-28';

// 左栏"填入需求示例":一段典型的自然语言策略需求
export const SAMPLE_REQUIREMENT =
  '写一个000001.XSHE的双均线择时策略,金叉满仓死叉清仓';

// 右栏"填入坏策略示例":真实含未来函数模式(.shift(-1))的 RQAlpha 策略,
// 平台 Layer 1 的 ast.shift_negative 规则会以 ERROR 拦截(与 strategies/bad_examples 同型)。
export const SAMPLE_BAD_CODE = `# 坏策略示例:含未来函数(.shift(-1)),用于演示 Layer 1 校验拦截。
# 用"明天的收盘价"决定今天的交易 —— 回测虚高,实盘根本无法实现。


def init(context):
    context.s1 = "000001.XSHE"
    context.fast = 5
    context.slow = 20


def handle_bar(context, bar_dict):
    import pandas as pd

    prices = history_bars(context.s1, context.slow + 1, "1d", "close")
    s = pd.Series(prices)
    # 未来函数:shift(-1) 向未来方向取数,拿"明天"的收盘价
    tomorrow = s.shift(-1).iloc[-1]
    ma_fast = prices[-context.fast:].mean()
    ma_slow = prices[-context.slow:].mean()

    if tomorrow > ma_slow or ma_fast > ma_slow:
        order_target_percent(context.s1, 0.95)
    else:
        order_target_percent(context.s1, 0.0)
`;

// ---------------------------------------------------------------------------
// 竞技场示例(strategies/demo_ma_cross.py 的合规双均线 + 上面含未来函数的坏策略),
// "载入示例"一键填入 2 个候选,演示"同场竞技 + 校验拦截"的完整效果。
// ---------------------------------------------------------------------------
export const ARENA_SAMPLE_GOOD_NAME = '双均线金叉合规版';
export const ARENA_SAMPLE_GOOD_CODE = `# 竞技场候选一:双均线择时(合规,与 strategies/demo_ma_cross.py 同型)。
# 信号只用 history_bars 截至当前 bar 的数据,无未来函数,可实盘复现。

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
        # 金叉满仓(目标仓位 95%,留现金缓冲)
        order_target_percent(context.s1, 0.95)
    elif ma_fast < ma_slow and pos.quantity > 0:
        # 死叉清仓
        order_target_percent(context.s1, 0.0)
`;

export const ARENA_SAMPLE_BAD_NAME = '未来函数作弊者';

// 竞技场候选二:含 .shift(-1) 未来函数的坏策略(直接复用右栏示例),
// Layer 1 的 ast.shift_negative 规则会以 ERROR 拦截,status="rejected"。
export const ARENA_SAMPLES = [
  { name: ARENA_SAMPLE_GOOD_NAME, code: ARENA_SAMPLE_GOOD_CODE },
  { name: ARENA_SAMPLE_BAD_NAME, code: SAMPLE_BAD_CODE },
];
