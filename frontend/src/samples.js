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

// ---------------------------------------------------------------------------
// 策略工坊模板(手动模式起步代码):三套均可过 Layer 1 校验,并在真实 bundle
// 上有成交潜力。写法要点与 strategies/demo_ma_cross.py 同源:
// - 信号只用 history_bars 截至当前 bar 的数据(无未来函数);
// - 不访问外部数据源(统一走 bundle 口径);
// - 下单用 order_target_percent 目标仓位并留现金缓冲;
// - rqalpha 6.5 的 get_position(direction) 第二参必填,统一带 POSITION_DIRECTION.LONG。
// ---------------------------------------------------------------------------

// 模板一:空白骨架(init / handle_bar + 中文注释),示例信号可直接替换。
export const STUDIO_TEMPLATE_BLANK_NAME = '空白模板';
export const STUDIO_TEMPLATE_BLANK_CODE = `# 策略工坊 · 空白模板:在 init / handle_bar 中实现你自己的策略逻辑。
# 平台约定(与四层校验对齐):
# - 只用平台注入的 API(history_bars / order_target_percent / context.portfolio 等);
#   访问外部数据源(requests / 本地文件 / 网络接口)会被 Layer 1 拦截;
# - 信号只能用截至当前 bar 的数据,禁止未来函数(如 .shift(-1));
# - 平台强制保守成本:0.2% 滑点、万八佣金、千一印花、成交量占比限制。

from rqalpha.const import POSITION_DIRECTION


def init(context):
    # 在此初始化:标的、参数、状态变量
    context.s1 = "000001.XSHE"   # 标的(order_book_id)
    context.fast = 5             # 示例参数:快线窗口
    context.slow = 20            # 示例参数:慢线窗口


def handle_bar(context, bar_dict):
    # 每个交易日收盘 bar 调用一次;在此写你的信号与下单逻辑
    prices = history_bars(context.s1, context.slow + 2, "1d", "close")
    if prices is None or len(prices) < context.slow:
        return

    # 示例信号(请替换成你自己的逻辑):双均线金叉/死叉
    ma_fast = prices[-context.fast:].mean()
    ma_slow = prices[-context.slow:].mean()

    pos = context.portfolio.get_position(context.s1, POSITION_DIRECTION.LONG)
    if ma_fast > ma_slow and pos.quantity == 0:
        order_target_percent(context.s1, 0.95)   # 金叉:目标仓位 95%(留现金缓冲)
    elif ma_fast < ma_slow and pos.quantity > 0:
        order_target_percent(context.s1, 0.0)    # 死叉:清仓
`;

// 模板二:双均线择时(与 strategies/demo_ma_cross.py 同型,实测可通过并可成交)。
export const STUDIO_TEMPLATE_MA_NAME = '双均线择时';
export const STUDIO_TEMPLATE_MA_CODE = `# 策略工坊模板:双均线择时(单一标的,低换手)。
# 快线上穿慢线(金叉)满仓,下穿(死叉)清仓;信号只用截至当前 bar 的数据。

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

// 模板三:RSI 超卖回归。history_bars 不传 fields 返回 numpy 结构化数组,
// bars['close'] 直接得到 ndarray 参与整体运算(不要当 DataFrame / Series 用)。
export const STUDIO_TEMPLATE_RSI_NAME = 'RSI 超卖';
export const STUDIO_TEMPLATE_RSI_CODE = `# 策略工坊模板:RSI 超卖回归(单一标的,低换手)。
# - history_bars 不传 fields 时返回 numpy 结构化数组,bars['close'] 直接得到
#   ndarray,可整体参与算术运算(不要当 DataFrame / Series 用);
# - RSI < 30 超卖买入,RSI 回升越过 45 卖出(避免在超卖区反复进出);
# - 信号只用截至当前 bar 的数据,无未来函数。

from rqalpha.const import POSITION_DIRECTION


def calculate_rsi(closes, period):
    """Wilder 式 RSI:纯 ndarray 切片实现,不引入外部依赖。"""
    if closes is None or len(closes) < period + 1:
        return None
    diffs = closes[1:] - closes[:-1]        # 逐日涨跌(截至当前 bar)
    gains = diffs.copy()
    losses = (-diffs).copy()
    gains[gains < 0] = 0.0
    losses[losses < 0] = 0.0
    avg_gain = gains[-period:].mean()
    avg_loss = losses[-period:].mean()
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def init(context):
    context.s1 = "000001.XSHE"
    context.period = 14        # RSI 周期
    context.oversold = 30.0    # 超卖买入阈值
    context.recover = 45.0     # 回升卖出阈值


def handle_bar(context, bar_dict):
    bars = history_bars(context.s1, context.period + 1, "1d")
    if bars is None or len(bars) < context.period + 1:
        return
    closes = bars["close"]     # ndarray:直接参与运算
    rsi = calculate_rsi(closes, context.period)
    if rsi is None:
        return

    pos = context.portfolio.get_position(context.s1, POSITION_DIRECTION.LONG)
    if pos.quantity == 0 and rsi < context.oversold:
        # 超卖买入(目标仓位 90%,留现金缓冲)
        order_target_percent(context.s1, 0.9)
    elif pos.quantity > 0 and rsi > context.recover:
        # 回升卖出
        order_target_percent(context.s1, 0.0)
`;

// 工坊模板下拉的数据源(key/name/code);默认选中双均线(最能演示完整链路)。
export const STUDIO_TEMPLATES = [
  { key: 'ma', name: STUDIO_TEMPLATE_MA_NAME, code: STUDIO_TEMPLATE_MA_CODE },
  { key: 'rsi', name: STUDIO_TEMPLATE_RSI_NAME, code: STUDIO_TEMPLATE_RSI_CODE },
  { key: 'blank', name: STUDIO_TEMPLATE_BLANK_NAME, code: STUDIO_TEMPLATE_BLANK_CODE },
];
