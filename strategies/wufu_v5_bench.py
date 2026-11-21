# -*- coding: utf-8 -*-
"""
【五福改进版 v5 · RQAlpha 日线平台移植版】多维超参共识 ETF 轮动
============================================================
原策略核心逻辑完整保留:
  1. 44 组多维超参(动量回看期 x R2阈值 x 均线周期)共识选 ETF
  2. 加权对数线性回归动量得分 = 年化斜率 x R2
  3. 同频次分组四因子风险排名(波动/最差单日/R2均值/区间回撤代理)
  4. 大A走弱状态机 + 牛市三级状态机 + 复合收益强制防御状态机
  5. 分模式风控(硬止损/成本保护/趋势均线/吊灯/YOYO/移动止损/RSI止盈/翻倍止盈)
  6. 持仓粘性 + 单一持仓 + 货币ETF兜底 + 空仓兜底 + 增强日报

平台适配要点:
  - 行情仅通过 history_bars(..., "1d", "close+volume") 获取, 全部判空+长度校验;
    结构化数组按字段名取列, 单字段回退路径返回一维数组整体切片
  - 指数信号改用宽基ETF代理: 沪深300/中证1000/创业板/科创50
  - ATR 用收盘价一阶差分近似(平台行情口径仅有收盘价)
  - 全市场动态池改为精选固定池(平台无全市场证券列表接口),
    流动性过滤 = 近3日平均成交额(收盘x成交量) > 2000万
  - 原"收盘分析->次日开盘执行"合并为单根日线: 用已完成历史K线决策, 当根K线执行
  - 交易日计数用 handle_bar 调用次数; 当前日期只取 context.now
"""
import numpy as np
from rqalpha.const import POSITION_DIRECTION

# ============================ 标的池(均为正常上市交易的ETF) ============================
GLOBAL_ETF_POOL = [
    "518880.XSHG",  # 黄金ETF
    "159985.XSHE",  # 豆粕ETF
    "159980.XSHE",  # 有色ETF(商品期货)
    "159981.XSHE",  # 能源化工ETF
    "513100.XSHG",  # 纳指ETF
    "159509.XSHE",  # 纳指科技ETF
    "513500.XSHG",  # 标普500ETF
    "513520.XSHG",  # 日经225ETF
    "513030.XSHG",  # 德国DAX ETF
]
CHINA_ETF_POOL = [
    "510300.XSHG", "510500.XSHG", "512100.XSHG", "159915.XSHE", "588000.XSHG",
    "159949.XSHE", "510880.XSHG", "512890.XSHG",
    "510900.XSHG", "159920.XSHE", "513180.XSHG", "513330.XSHG", "513050.XSHG", "513120.XSHG",
    "512880.XSHG", "512800.XSHG",
    "512010.XSHG", "512170.XSHG",
    "512690.XSHG", "159928.XSHE", "515170.XSHG",
    "512480.XSHG", "159995.XSHE", "515880.XSHG", "515050.XSHG", "512720.XSHG",
    "515790.XSHG", "515030.XSHG", "516160.XSHG",
    "512660.XSHG", "512400.XSHG", "515220.XSHG", "516150.XSHG",
    "512200.XSHG", "512980.XSHG",
]
DEFENSIVE_ETF = "511880.XSHG"  # 银华日利货币ETF(防御兜底)
MARKET_PROXY_ETFS = ["510300.XSHG", "512100.XSHG", "159915.XSHE", "588000.XSHG"]
BENCH_ETF = "510300.XSHG"

ETF_NAMES = {
    "518880.XSHG": "黄金ETF", "159985.XSHE": "豆粕ETF", "159980.XSHE": "有色ETF",
    "159981.XSHE": "能源化工ETF", "513100.XSHG": "纳指ETF", "159509.XSHE": "纳指科技ETF",
    "513500.XSHG": "标普500ETF", "513520.XSHG": "日经225ETF", "513030.XSHG": "德国ETF",
    "510300.XSHG": "沪深300ETF", "510500.XSHG": "中证500ETF", "512100.XSHG": "中证1000ETF",
    "159915.XSHE": "创业板ETF", "588000.XSHG": "科创50ETF", "159949.XSHE": "创业板50ETF",
    "510880.XSHG": "红利ETF", "512890.XSHG": "红利低波ETF",
    "510900.XSHG": "H股ETF", "159920.XSHE": "恒生ETF", "513180.XSHG": "恒生科技ETF",
    "513330.XSHG": "恒生互联网ETF", "513050.XSHG": "中概互联网ETF", "513120.XSHG": "港股创新药ETF",
    "512880.XSHG": "证券ETF", "512800.XSHG": "银行ETF",
    "512010.XSHG": "医药ETF", "512170.XSHG": "医疗ETF",
    "512690.XSHG": "酒ETF", "159928.XSHE": "消费ETF", "515170.XSHG": "食品饮料ETF",
    "512480.XSHG": "半导体ETF", "159995.XSHE": "芯片ETF", "515880.XSHG": "通信ETF",
    "515050.XSHG": "5G通信ETF", "512720.XSHG": "计算机ETF",
    "515790.XSHG": "光伏ETF", "515030.XSHG": "新能源车ETF", "516160.XSHG": "新能源ETF",
    "512660.XSHG": "军工ETF", "512400.XSHG": "有色金属ETF", "515220.XSHG": "煤炭ETF",
    "516150.XSHG": "稀土ETF", "512200.XSHG": "房地产ETF", "512980.XSHG": "传媒ETF",
    "511880.XSHG": "银华日利",
}

# 44 组多维超参: (回看期, 名称前缀, [(R2阈值, 均线周期, 牛市权重), ...])
_HP_SPEC = [
    (15, "S15", [(0.30, 8, 1.4), (0.35, 15, 1.3), (0.40, 20, 1.2), (0.45, 25, 1.0)]),
    (18, "S18", [(0.30, 8, 1.4), (0.35, 15, 1.3), (0.40, 20, 1.2), (0.45, 25, 1.0)]),
    (20, "M20", [(0.30, 8, 1.2), (0.35, 15, 1.1), (0.40, 20, 1.0), (0.45, 25, 0.9)]),
    (22, "M22", [(0.30, 8, 1.1), (0.35, 15, 1.0), (0.40, 20, 1.0), (0.45, 25, 0.9)]),
    (24, "M24", [(0.30, 8, 1.0), (0.35, 15, 1.0), (0.40, 20, 0.95), (0.45, 25, 0.9)]),
    (25, "M25", [(0.30, 8, 1.0), (0.35, 15, 1.0), (0.40, 20, 0.95), (0.45, 25, 0.9)]),
    (26, "M26", [(0.30, 8, 0.95), (0.35, 15, 0.95), (0.40, 20, 0.9), (0.45, 25, 0.85)]),
    (28, "M28", [(0.30, 8, 0.9), (0.35, 15, 0.9), (0.40, 20, 0.85), (0.45, 25, 0.8)]),
    (30, "L30", [(0.30, 8, 0.85), (0.35, 15, 0.85), (0.40, 20, 0.8), (0.45, 25, 0.75)]),
    (35, "L35", [(0.30, 8, 0.8), (0.35, 15, 0.8), (0.40, 20, 0.75), (0.45, 25, 0.7)]),
    (40, "L40", [(0.30, 8, 0.7), (0.35, 15, 0.7), (0.40, 20, 0.65), (0.45, 25, 0.6)]),
]

STOPLOSS_PARAMS = {
    "attack": {
        "atr_periods": 22, "chandelier_multiplier": 3.0, "yoyo_multiplier": 1.5,
        "trailing_stop_ratio": 0.15, "cost_stop_ratio": 0.09,
        "rsi_overbought": 85, "profit_threshold": 0.15, "max_single_loss_ratio": None,
    },
    "bull_attack": {
        "atr_periods": 28, "chandelier_multiplier": 4.0, "yoyo_multiplier": 2.0,
        "trailing_stop_ratio": 0.18, "cost_stop_ratio": 0.09,
        "rsi_overbought": 92, "profit_threshold": 0.30, "max_single_loss_ratio": None,
        "disable_ma_exit": True, "disable_double_takeprofit": True,
    },
    "defense": {
        "atr_periods": 14, "chandelier_multiplier": 2.0, "yoyo_multiplier": 1.2,
        "trailing_stop_ratio": 0.10, "cost_stop_ratio": 0.10,
        "rsi_overbought": 80, "profit_threshold": 0.10, "max_single_loss_ratio": 0.10,
    },
    "cash_like": {
        "atr_periods": 10, "chandelier_multiplier": 1.5, "yoyo_multiplier": 1.0,
        "trailing_stop_ratio": 0.02, "cost_stop_ratio": 0.015,
        "rsi_overbought": 90, "profit_threshold": 0.03, "max_single_loss_ratio": 0.02,
        "hard_stoploss": 0.015, "day_drop_3d_thr": -0.006, "day_drop_vs_ma5_thr": -0.006,
    },
}

FETCH_COUNT = 120


# ============================================================
# 初始化(只赋参数, 不下单)
# ============================================================
def init(context):
    context.global_pool = list(GLOBAL_ETF_POOL)
    context.fixed_pool = list(GLOBAL_ETF_POOL) + list(CHINA_ETF_POOL)
    context.global_set = set(GLOBAL_ETF_POOL)
    context.defensive_etf = DEFENSIVE_ETF
    context.proxy_codes = list(MARKET_PROXY_ETFS)
    context.bench_code = BENCH_ETF
    context.stock_universe = sorted(set(context.fixed_pool) | {DEFENSIVE_ETF} | set(MARKET_PROXY_ETFS))
    context.fetch_count = FETCH_COUNT

    context.hyperparams = _build_hyperparams()
    context.max_lookback = max(h["lookback"] for h in context.hyperparams)
    context.lb_set = sorted(set(h["lookback"] for h in context.hyperparams))
    context.ma_set = sorted(set(h["ma_lb"] for h in context.hyperparams))
    context.top_n_per_param = 50
    context.recommend_count = 5
    context.min_freq_absolute = 3.0
    context.bull_min_freq_absolute = 2.0
    context.min_score_threshold = 0.0
    context.max_score_threshold = 8.0
    context.bull_max_score_threshold = 12.0
    context.bull_r2_relax = 0.05
    context.ma_threshold = 1.0
    context.loss = 0.97
    context.bull_loss = 0.95
    context.money_threshold = 2e7
    context.enable_stickiness = True
    context.stickiness_freq_gap = 2.0
    context.stickiness_score_ratio = 1.15
    context.bull_stickiness_score_ratio = 1.25

    context.is_weak = False
    context.weak_days = 0
    context.max_weak_days = 10
    context.weak_ma = 10
    context.is_bull = False
    context.bull_level = 0
    context.bull_ma = 20
    context.bull_breadth_enter = 3
    context.bull_breadth_exit = 2
    context.bull_mom_lb = 10
    context.bull_mom_thr = 0.03
    context.bull_strong_thr = 0.08
    context.attack_boost = False

    context.force_defense = False
    context.force_days_left = 0
    context.force_defense_span = 5
    context.bull_force_defense_span = 3
    context.strategy_ret_hist = []
    context.bench_ret_hist = []
    context.max_10d_loss = -0.105
    context.bull_max_10d_loss = -0.14
    context.max_ret_diff = -0.20
    context.bull_max_ret_diff = -0.28

    context.stoploss_params = STOPLOSS_PARAMS
    context.stoploss_limit = 0.08
    context.trend_ma = 20
    context.crash_single = -0.05
    context.crash_composite = -0.05
    context.highest_close = {}
    context.hold_days = {}

    context.initial_value = None
    context.prev_value = None
    context.peak_value = 0.0
    context.max_dd = 0.0

    print("=" * 64)
    print("【五福 v5 · RQAlpha 日线移植版】初始化完成")
    print("超参组数=%d | 全球池%d只 | 固定池%d只 | 防御=%s" % (
        len(context.hyperparams), len(context.global_pool),
        len(context.fixed_pool), context.defensive_etf))
    print("=" * 64)


# ============================================================
# 每根日线: 决策 + 执行 + 日报
# ============================================================
def handle_bar(context, bar_dict):
    total_value = float(context.portfolio.total_value)
    if context.initial_value is None:
        context.initial_value = total_value
        context.prev_value = total_value
    prev = context.prev_value if context.prev_value and context.prev_value > 0 else total_value
    daily_ret = total_value / prev - 1.0 if prev > 0 else 0.0
    total_ret = total_value / context.initial_value - 1.0 if context.initial_value > 0 else 0.0
    if total_value > context.peak_value:
        context.peak_value = total_value
    cur_dd = (context.peak_value - total_value) / context.peak_value if context.peak_value > 0 else 0.0
    if cur_dd > context.max_dd:
        context.max_dd = cur_dd

    try:
        cache = _build_cache(context)
        if not cache:
            context.prev_value = total_value
            return

        bench_ret = 0.0
        b = cache.get(context.bench_code)
        if b is not None and len(b["c"]) >= 2 and b["c"][-2] > 0:
            bench_ret = float(b["c"][-1] / b["c"][-2] - 1.0)
        context.strategy_ret_hist.append(daily_ret)
        context.bench_ret_hist.append(bench_ret)
        if len(context.strategy_ret_hist) > 10:
            context.strategy_ret_hist.pop(0)
        if len(context.bench_ret_hist) > 10:
            context.bench_ret_hist.pop(0)

        holdings = [c for c in context.stock_universe if _qty(context, c) > 0]
        for c in list(context.highest_close.keys()):
            if c not in holdings:
                context.highest_close.pop(c, None)
        for c in list(context.hold_days.keys()):
            if c not in holdings:
                context.hold_days.pop(c, None)
        for c in holdings:
            context.hold_days[c] = context.hold_days.get(c, 0) + 1
            e = cache.get(c)
            if e is not None and len(e["c"]) >= 1:
                px = float(e["c"][-1])
                context.highest_close[c] = max(context.highest_close.get(c, px), px)

        _update_weak(context, cache)
        _update_bull(context, cache)
        _update_force_defense(context)
        context.attack_boost = bool(context.is_bull and context.bull_level > 0
                                    and not context.is_weak and not context.force_defense)
        mode = "defense" if _defense_mode(context) else ("bull_attack" if context.attack_boost else "attack")
        pool = _build_pool(context, cache)

        force_sell = {}
        for c in holdings:
            e = cache.get(c)
            if e is None or len(e["c"]) < 3:
                continue
            pos = context.portfolio.get_position(c, POSITION_DIRECTION.LONG)
            cost = float(pos.avg_price) if pos is not None and pos.avg_price and pos.avg_price > 0 else float(e["c"][-1])
            hit, why = _check_stoploss(context, c, e["c"], cost, mode)
            if hit:
                force_sell[c] = why
        if _market_crash(context, cache):
            for c in holdings:
                force_sell[c] = "市场大跌保护"
            defensive_hit = True
        else:
            defensive_hit = context.defensive_etf in force_sell

        cur_hold = None
        for c in holdings:
            if c != context.defensive_etf and c not in force_sell:
                cur_hold = c
                break

        candidate, recommend, info = _select(context, cache, pool, force_sell, cur_hold)

        if defensive_hit:
            if candidate is not None:
                target = candidate
                reason = "风控触发改持共识最优"
            else:
                target = None
                reason = "风控触发且无替代->空仓"
        elif candidate is None:
            if context.defensive_etf in cache and context.defensive_etf not in force_sell:
                target = context.defensive_etf
                reason = ("防御期" if _defense_mode(context) else "共识无标的") + "->货币ETF兜底"
            else:
                target = None
                reason = "无可用标的->空仓"
        else:
            target = candidate
            opt = info.get("optimal") or {}
            prefix = ("强防期全球池" if context.force_defense else
                      ("走弱期全球池" if context.is_weak else
                       ("牛市进攻" if context.attack_boost else "稳健共识")))
            reason = "%s: wf=%.1f/%d 动量%.4f 风险Σ%.2f" % (
                prefix, opt.get("wfreq", 0.0), info.get("n", 0),
                opt.get("avg_score", 0.0), opt.get("risk_sum", 0.0))
            if info.get("sticky"):
                reason += " | " + info["sticky"]

        trades = _execute(context, cache, holdings, target, force_sell)
        _report(context, cache, trades, target, reason, recommend, mode,
                daily_ret, total_ret, bench_ret, cur_dd, total_value)
    except Exception as exc:
        print("[异常] %s handle_bar: %s" % (context.now.strftime("%Y-%m-%d"), exc))
    context.prev_value = total_value


# ============================================================
# 数据获取(仅 history_bars, 全部判空+长度校验)
# ============================================================
def _build_hyperparams():
    out = []
    for lb, tag, rows in _HP_SPEC:
        for r2, ma, bw in rows:
            out.append({"lookback": lb, "r2_min": r2, "ma_lb": ma,
                        "bull_weight": bw, "name": "%s_R%d_M%d" % (tag, int(r2 * 100), ma)})
    return out


def _fetch_series(code, count):
    closes = None
    vols = None
    try:
        bars = history_bars(code, count, "1d", "close+volume")
        if bars is not None and len(bars) >= 3:
            closes = np.asarray(bars["close"], dtype=float)
            vols = np.asarray(bars["volume"], dtype=float)
    except Exception:
        closes = None
        vols = None
    if closes is None:
        prices = history_bars(code, count, "1d", "close")
        if prices is None or len(prices) < 3:
            return None
        closes = np.asarray(prices, dtype=float)
        vols = None
    if closes is None or len(closes) < 3:
        return None
    mask = np.isfinite(closes) & (closes > 0)
    if vols is not None and len(vols) == len(closes):
        mask = mask & np.isfinite(vols) & (vols > 0)
        c = closes[mask]
        v = vols[mask]
        if len(c) < 3:
            return None
        money = float(np.mean(c[-3:] * v[-3:]))
    else:
        c = closes[mask]
        if len(c) < 3:
            return None
        money = None
    return c, money


def _build_cache(context):
    cache = {}
    for code in context.stock_universe:
        got = _fetch_series(code, context.fetch_count)
        if got is None:
            continue
        c, money = got
        cache[code] = {"c": c, "money": money}
    return cache


def _qty(context, code):
    try:
        pos = context.portfolio.get_position(code, POSITION_DIRECTION.LONG)
        return float(pos.quantity) if pos is not None else 0.0
    except Exception:
        return 0.0


def _name(code):
    return ETF_NAMES.get(code, code)


def _defense_mode(context):
    return bool(context.is_weak or context.force_defense)


def _compound(rets):
    if not rets:
        return 0.0
    return float(np.prod([1.0 + r for r in rets]) - 1.0)


# ============================================================
# 状态机: 走弱 / 牛市 / 强制防御
# ============================================================
def _update_weak(context, cache):
    above = 0
    below = 0
    valid = 0
    for code in context.proxy_codes:
        e = cache.get(code)
        if e is None or len(e["c"]) < context.weak_ma:
            continue
        c = e["c"]
        px = float(c[-1])
        ma = float(c[-context.weak_ma:].mean())
        valid += 1
        if px > ma:
            above += 1
        elif px < ma:
            below += 1
    if valid == 0:
        return
    weak_in = below >= 3
    weak_out = above >= 3
    if context.is_weak:
        context.weak_days += 1
        if weak_out:
            context.is_weak = False
            context.weak_days = 0
        elif context.weak_days >= context.max_weak_days:
            context.weak_days = 0
    else:
        if weak_in:
            context.is_weak = True
            context.weak_days = 0
            context.is_bull = False
            context.bull_level = 0


def _update_bull(context, cache):
    if context.is_weak or context.force_defense:
        context.is_bull = False
        context.bull_level = 0
        return
    need = max(context.bull_ma, context.bull_mom_lb + 1)
    above = 0
    valid = 0
    moms = []
    for code in context.proxy_codes:
        e = cache.get(code)
        if e is None or len(e["c"]) < need:
            continue
        c = e["c"]
        px = float(c[-1])
        ma = float(c[-context.bull_ma:].mean())
        valid += 1
        if px > ma:
            above += 1
        p0 = float(c[-(context.bull_mom_lb + 1)])
        if p0 > 0:
            moms.append(px / p0 - 1.0)
    if valid == 0:
        context.is_bull = False
        context.bull_level = 0
        return
    avg_mom = float(np.mean(moms)) if moms else 0.0
    breadth_ok = above >= context.bull_breadth_enter
    enter = breadth_ok and avg_mom >= context.bull_mom_thr
    strong = avg_mom >= context.bull_strong_thr
    exit_cond = (above < context.bull_breadth_exit) or (avg_mom < 0.0)
    if context.is_bull:
        if exit_cond:
            context.is_bull = False
            context.bull_level = 0
        elif enter:
            context.bull_level = 2 if strong else 1
        else:
            context.bull_level = 0
    else:
        if enter:
            context.is_bull = True
            context.bull_level = 2 if strong else 1


def _update_force_defense(context):
    if context.force_defense:
        context.force_days_left -= 1
        if context.force_days_left <= 0:
            context.force_defense = False
            context.force_days_left = 0
        return
    loss_thr = context.bull_max_10d_loss if context.is_bull else context.max_10d_loss
    diff_thr = context.bull_max_ret_diff if context.is_bull else context.max_ret_diff
    trigger = False
    if len(context.strategy_ret_hist) >= 10:
        cs = _compound(context.strategy_ret_hist)
        if cs <= loss_thr:
            trigger = True
        elif len(context.bench_ret_hist) >= 10:
            cb = _compound(context.bench_ret_hist)
            if cs - cb <= diff_thr:
                trigger = True
    if trigger:
        span = context.bull_force_defense_span if context.is_bull else context.force_defense_span
        context.force_defense = True
        context.force_days_left = span
        context.is_bull = False
        context.bull_level = 0


def _build_pool(context, cache):
    base = context.global_pool if _defense_mode(context) else context.fixed_pool
    pool = []
    for c in base:
        e = cache.get(c)
        if e is None:
            continue
        m = e["money"]
        if m is None or m > context.money_threshold:
            pool.append(c)
    if not pool:
        pool = [c for c in base if c in cache]
    return pool


# ============================================================
# 动量与风控指标(纯收盘价口径)
# ============================================================
def _momentum(closes, lookback):
    if closes is None or len(closes) < lookback + 1:
        return None
    recent = closes[-(lookback + 1):]
    if np.any(recent <= 0) or np.any(~np.isfinite(recent)):
        return None
    y = np.log(recent)
    x = np.arange(len(y), dtype=float)
    weights = np.linspace(1.0, 2.0, len(y))
    W = weights ** 2
    W_sum = float(np.sum(W))
    if W_sum <= 0:
        return None
    x_bar = float(np.sum(W * x) / W_sum)
    y_bar = float(np.sum(W * y) / W_sum)
    dx = x - x_bar
    dy = y - y_bar
    var_x = float(np.sum(W * dx * dx))
    if var_x <= 0:
        return 0.0, 0.0, 0.0
    slope = float(np.sum(W * dx * dy) / var_x)
    intercept = y_bar - slope * x_bar
    z = slope * 250.0
    if z > 50.0:
        z = 50.0
    ann = float(np.exp(z) - 1.0)
    y_pred = slope * x + intercept
    ss_res = float(np.sum(weights * (y - y_pred) ** 2))
    ss_tot = float(np.sum(weights * (y - y.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return ann * r2, ann, r2


def _atr_close(closes, periods):
    if closes is None or len(closes) < periods + 1:
        return None
    tr = np.abs(np.diff(closes))
    if len(tr) < periods:
        return None
    return float(tr[-periods:].mean())


def _rsi(closes, periods):
    if closes is None or len(closes) < periods + 1:
        return None
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = float(gains[-periods:].mean())
    avg_loss = float(losses[-periods:].mean())
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def _check_cash_like(context, closes, cost):
    p = context.stoploss_params["cash_like"]
    if closes is None or len(closes) < 30 or cost <= 0:
        return False, ""
    px = float(closes[-1])
    profit = px / cost - 1.0
    if profit <= -p["hard_stoploss"]:
        return True, "货币ETF硬止损%.2f%%" % (profit * 100)
    if profit <= -p["max_single_loss_ratio"]:
        return True, "货币ETF亏损超限"
    if px < cost * (1.0 - p["cost_stop_ratio"]):
        return True, "货币ETF成本破位"
    high = context.highest_close.get(context.defensive_etf, px)
    if px < high * (1.0 - p["trailing_stop_ratio"]):
        return True, "货币ETF高点回撤"
    if closes[-4] > 0:
        r3 = px / float(closes[-4]) - 1.0
        if r3 <= p["day_drop_3d_thr"]:
            return True, "货币ETF三日急跌"
    ma5 = float(closes[-6:-1].mean())
    if ma5 > 0 and px / ma5 - 1.0 <= p["day_drop_vs_ma5_thr"]:
        return True, "货币ETF偏离五日均"
    return False, ""


def _check_stoploss(context, code, closes, cost, mode):
    if code == context.defensive_etf:
        return _check_cash_like(context, closes, cost)
    p = context.stoploss_params.get(mode, context.stoploss_params["attack"])
    need = max(p["atr_periods"] * 2, context.trend_ma + 5, 50)
    if closes is None or len(closes) < need or cost <= 0:
        return False, ""
    px = float(closes[-1])
    if px <= 0:
        return False, ""
    profit = px / cost - 1.0
    if profit <= -context.stoploss_limit:
        return True, "硬止损%.2f%%" % (profit * 100)
    ms = p.get("max_single_loss_ratio")
    if ms is not None and profit <= -ms:
        return True, "单标亏损%.2f%%" % (profit * 100)
    if px < cost * (1.0 - p["cost_stop_ratio"]):
        return True, "成本保护破位"
    if not p.get("disable_ma_exit", False):
        ma = float(closes[-context.trend_ma:].mean())
        if px < ma:
            return True, "趋势破MA%d" % context.trend_ma
    atr = _atr_close(closes, p["atr_periods"])
    high = context.highest_close.get(code, px)
    if atr is not None and atr > 0:
        if px < high - p["chandelier_multiplier"] * atr:
            return True, "吊灯止损"
        if px < float(closes[-2]) - p["yoyo_multiplier"] * atr:
            return True, "YOYO止损"
        if px < high * (1.0 - p["trailing_stop_ratio"]):
            return True, "移动止损"
    if profit > p["profit_threshold"]:
        rsi = _rsi(closes, 14)
        if rsi is not None and rsi > p["rsi_overbought"]:
            return True, "RSI超买止盈%.0f" % rsi
    if not p.get("disable_double_takeprofit", False):
        if px >= cost * 2.0:
            return True, "翻倍止盈"
    return False, ""


def _market_crash(context, cache):
    rets = []
    for code in context.proxy_codes:
        e = cache.get(code)
        if e is None or len(e["c"]) < 2:
            continue
        c = e["c"]
        if c[-2] > 0:
            rets.append(float(c[-1] / c[-2] - 1.0))
    if not rets:
        return False
    if min(rets) <= context.crash_single:
        return True
    if float(np.mean(rets)) <= context.crash_composite:
        return True
    return False


# ============================================================
# 同频次分组风险排名
# ============================================================
def _rank_values(values, higher_better):
    n = len(values)
    if n == 0:
        return []
    indexed = []
    for i, v in enumerate(values):
        try:
            fv = float(v)
            if not np.isfinite(fv):
                fv = None
        except Exception:
            fv = None
        indexed.append((i, fv))
    valid = [(i, v) for i, v in indexed if v is not None]
    valid.sort(key=lambda t: t[1], reverse=higher_better)
    ranks = [0.0] * n
    i = 0
    while i < len(valid):
        j = i
        while j + 1 < len(valid) and valid[j + 1][1] == valid[i][1]:
            j += 1
        avg_rank = (i + 1 + j + 1) / 2.0
        for k in range(i, j + 1):
            ranks[valid[k][0]] = avg_rank
        i = j + 1
    for i, v in indexed:
        if v is None:
            ranks[i] = float(n)
    return ranks


# ============================================================
# 多超参共识选股
# ============================================================
def _select(context, cache, pool, force_sell, cur_hold):
    attack = context.attack_boost
    weak = _defense_mode(context)
    hps = context.hyperparams
    n = len(hps)
    candidates = []
    for c in pool:
        if c in force_sell:
            continue
        e = cache.get(c)
        if e is None or len(e["c"]) < context.max_lookback + 1:
            continue
        candidates.append(c)
    if not candidates:
        return None, [], {}

    loss_thr = context.bull_loss if attack else context.loss
    base = {}
    ma_pass = {}
    min_day = {}
    loss_pass = {}
    vol_map = {}
    r2_mean = {}
    for c in candidates:
        closes = cache[c]["c"]
        for lb in context.lb_set:
            m = _momentum(closes, lb)
            if m is None:
                continue
            score, ann, r2 = m
            window = closes[-(lb + 1):]
            run_max = np.maximum.accumulate(window)
            draw = float(np.max((run_max - window) / run_max))
            base[(c, lb)] = (score, r2, draw)
        px = float(closes[-1])
        for ml in context.ma_set:
            ma_pass[(c, ml)] = px > float(closes[-ml:].mean()) * context.ma_threshold
        d1 = float(closes[-1] / closes[-2])
        d2 = float(closes[-2] / closes[-3])
        d3 = float(closes[-3] / closes[-4])
        md = min(1.0, d1, d2, d3)
        min_day[c] = md
        loss_pass[c] = md >= loss_thr
        seg = closes[-21:]
        if len(seg) >= 10:
            vol_map[c] = float(np.std(np.diff(seg) / seg[:-1]))
        else:
            vol_map[c] = 0.02
        r2s = [base[(c, lb)][1] for lb in context.lb_set if (c, lb) in base]
        r2_mean[c] = float(np.mean(r2s)) if r2s else 0.0

    weighted = {}
    appear = {}
    ssum = {}
    swsum = {}
    dsum = {}
    dcnt = {}
    core_pass = set()
    for hp in hps:
        lb = hp["lookback"]
        r2min = hp["r2_min"]
        ml = hp["ma_lb"]
        w = float(hp["bull_weight"]) if attack else 1.0
        r2_thr = max(0.20, r2min - context.bull_r2_relax) if attack else r2min
        max_score = context.bull_max_score_threshold if attack else context.max_score_threshold
        rows = []
        for c in candidates:
            b = base.get((c, lb))
            if b is None:
                continue
            score, r2, draw = b
            if not (context.min_score_threshold <= score <= max_score):
                continue
            if r2 <= r2_thr:
                continue
            if not weak:
                if not ma_pass.get((c, ml), False):
                    continue
                if not loss_pass[c]:
                    continue
            core_pass.add(c)
            rows.append((c, score, draw))
        rows.sort(key=lambda t: t[1], reverse=True)
        for c, score, draw in rows[:context.top_n_per_param]:
            appear[c] = appear.get(c, 0) + 1
            weighted[c] = weighted.get(c, 0.0) + w
            ssum[c] = ssum.get(c, 0.0) + score * w
            swsum[c] = swsum.get(c, 0.0) + w
            dsum[c] = dsum.get(c, 0.0) + draw
            dcnt[c] = dcnt.get(c, 0) + 1
    if not weighted:
        return None, [], {}

    min_wf = context.bull_min_freq_absolute if attack else context.min_freq_absolute
    working = [c for c, wf in weighted.items() if wf >= min_wf]
    if len(working) < context.recommend_count:
        cand = working
        for ratio in (0.30, 0.20, 0.10):
            thr = max(1.5, ratio * n * (1.1 if attack else 1.0))
            cand = [c for c, wf in weighted.items() if wf >= thr]
            if len(cand) >= context.recommend_count:
                working = cand
                break
        else:
            working = cand
    if not working:
        ordered = sorted(weighted.items(), key=lambda kv: kv[1], reverse=True)
        working = [c for c, _ in ordered[:context.recommend_count * 3]]
    if not working:
        return None, [], {}

    rows = []
    for c in working:
        freq = appear.get(c, 0)
        wf = weighted.get(c, 0.0)
        avg = ssum[c] / swsum[c] if swsum.get(c, 0.0) > 0 else 0.0
        rows.append({
            "etf": c, "freq": freq, "wfreq": wf, "avg_score": avg,
            "r2_mean": r2_mean.get(c, 0.0), "min_day": min_day.get(c, 1.0),
            "vol": vol_map.get(c, 0.02),
            "draw": dsum[c] / dcnt[c] if dcnt.get(c, 0) > 0 else 0.0,
            "is_global": c in context.global_set, "core": c in core_pass,
        })
    groups = {}
    for r in rows:
        groups.setdefault(r["freq"], []).append(r)
    for freq, grp in groups.items():
        if len(grp) == 1:
            grp[0]["risk_sum"] = 4.0
            continue
        rv = _rank_values([r["vol"] for r in grp], False)
        rm = _rank_values([r["min_day"] for r in grp], True)
        rr = _rank_values([r["r2_mean"] for r in grp], True)
        rd = _rank_values([r["draw"] for r in grp], False)
        for i, r in enumerate(grp):
            s = rv[i] + rm[i] + rr[i] + rd[i]
            if weak and r["is_global"]:
                s *= 0.92
            r["risk_sum"] = float(s)
    if attack:
        rows.sort(key=lambda r: (-r["wfreq"], -r["avg_score"], r["risk_sum"]))
    else:
        rows.sort(key=lambda r: (-r["wfreq"], r["risk_sum"], -r["avg_score"]))
    recommend = rows[:context.recommend_count]
    optimal = recommend[0] if recommend else None

    sticky = ""
    if context.enable_stickiness and optimal is not None and not weak and cur_hold:
        cur_row = None
        for r in rows:
            if r["etf"] == cur_hold:
                cur_row = r
                break
        if cur_row is not None and cur_row["core"]:
            ratio_thr = context.bull_stickiness_score_ratio if attack else context.stickiness_score_ratio
            gap = optimal["wfreq"] - cur_row["wfreq"]
            sr = optimal["avg_score"] / cur_row["avg_score"] if cur_row["avg_score"] > 1e-8 else 99.0
            if gap < context.stickiness_freq_gap and sr < ratio_thr:
                sticky = "持仓粘性续持%s" % cur_hold
                optimal = cur_row
                recommend = [cur_row] + [r for r in recommend if r["etf"] != cur_hold]
                recommend = recommend[:context.recommend_count]

    target = optimal["etf"] if optimal is not None else None
    info = {"optimal": optimal, "n": n, "sticky": sticky, "attack": attack}
    return target, recommend, info


# ============================================================
# 执行(仅 order_target_percent, 下单前核对持仓)
# ============================================================
def _execute(context, cache, holdings, target, force_sell):
    trades = []
    for c in holdings:
        if c == target and c not in force_sell:
            continue
        if context.portfolio.get_position(c, POSITION_DIRECTION.LONG).quantity > 0:
            order_target_percent(c, 0.0)
            trades.append(("卖出", c, force_sell.get(c, "调仓换标的")))
            context.highest_close.pop(c, None)
            context.hold_days.pop(c, None)
    if target is not None:
        if context.portfolio.get_position(target, POSITION_DIRECTION.LONG).quantity == 0:
            order_target_percent(target, 0.95)
            e = cache.get(target)
            if e is not None and len(e["c"]) >= 1:
                context.highest_close[target] = float(e["c"][-1])
            context.hold_days[target] = 0
            trades.append(("买入", target, "目标仓位95%"))
    return trades


# ============================================================
# 日报
# ============================================================
def _report(context, cache, trades, target, reason, recommend, mode,
            daily_ret, total_ret, bench_ret, cur_dd, total_value):
    date_str = context.now.strftime("%Y-%m-%d")
    if context.force_defense:
        tag = "强制防御"
    elif context.is_weak:
        tag = "走弱防御"
    elif context.is_bull:
        tag = "牛市L%d" % context.bull_level if context.bull_level > 0 else "牛市观察"
    else:
        tag = "正常进攻"
    cs = _compound(context.strategy_ret_hist)
    cb = _compound(context.bench_ret_hist)
    print("=" * 64)
    print("[日报 %s] 模式=%s/%s | 资产%.0f 峰值%.0f" % (date_str, mode, tag, total_value, context.peak_value))
    print("  日收益%+.2f%% 累计%+.2f%% 基准日%+.2f%% | 回撤%.2f%% 最大%.2f%% | 10日复合 策略%+.2f%%/基准%+.2f%%" % (
        daily_ret * 100, total_ret * 100, bench_ret * 100, cur_dd * 100, context.max_dd * 100, cs * 100, cb * 100))
    print("  状态 weak=%s(%d/%d) force=%s bull=L%d boost=%s" % (
        context.is_weak, context.weak_days, context.max_weak_days,
        context.force_defense, context.bull_level, context.attack_boost))
    holdings = [c for c in context.stock_universe if _qty(context, c) > 0]
    if not holdings:
        print("  持仓: 空仓(现金)")
    else:
        for c in holdings:
            pos = context.portfolio.get_position(c, POSITION_DIRECTION.LONG)
            e = cache.get(c)
            px = float(e["c"][-1]) if e is not None and len(e["c"]) >= 1 else 0.0
            cost = float(pos.avg_price) if pos.avg_price and pos.avg_price > 0 else px
            pnl = px / cost - 1.0 if cost > 0 else 0.0
            days = context.hold_days.get(c, 0)
            hi = context.highest_close.get(c)
            hi_s = " 最高收%.3f" % hi if hi else ""
            print("  持仓 %s %s 数量%.0f 成本%.3f 现价%.3f 浮盈%+.2f%% 持仓%d日%s" % (
                c, _name(c), pos.quantity, cost, px, pnl * 100, days, hi_s))
    if trades:
        for act, code, why in trades:
            print("  成交 %s %s %s (%s)" % (act, code, _name(code), why))
    else:
        print("  成交: 无")
    tgt = "空仓" if target is None else "%s %s" % (target, _name(target))
    print("  目标: %s | %s" % (tgt, reason))
    if recommend:
        parts = ["%s wf%.1f 分%.3f" % (r["etf"], r["wfreq"], r["avg_score"]) for r in recommend[:5]]
        print("  推荐: " + " | ".join(parts))