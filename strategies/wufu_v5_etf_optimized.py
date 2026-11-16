# -*- coding: utf-8 -*-
"""五福改进版 v5 —— ETF 轮动 RQAlpha 移植版（优化版）

在基线 wufu_v5_etf_rotation.py 之上按用户第一轮意见优化：
1. 大幅降低换手：最小持有期 10 个交易日（风控止损除外）+ 增强持仓粘性
   （只有频率领先>=3 且分数比>1.4 才换仓）；
2. 提高动量入选门槛：min_score 0->0.25，r2_min 整体上抬 0.10，连跌过滤 0.97->0.985；
3. 收紧风控：硬止损 8%->6.5%，10 日累计回撤强制防御阈值 -10.5%->-8%，
   保留 MA20 破位与 ATR 吊灯止损；
4. 走弱期(is_weak)或强制防御时一律切 511880.XSHG 货币ETF 空仓避险，不在弱势里轮动；
5. 牛市进攻期保持宽基/主题暴露（正常轮动），止损参数全局统一不放宽。

合规：仅用 history_bars 取截至当前 bar 的一维 ndarray，无未来函数/外部数据；
持仓成本用 pos.avg_price；标的池 13 只真实 ETF，防御 511880.XSHG，基准 000001.XSHG；
单标的 95% 仓位；保持 init / handle_bar 两个入口。
"""

import numpy as np
import math
from rqalpha.const import POSITION_DIRECTION


def init(context):
    # ---- ETF 轮动池（宽基 + 主题 + 跨境 + 债/黄金,均为 bundle 内真实合约）----
    context.pool = [
        '510300.XSHG',  # 沪深300ETF
        '510500.XSHG',  # 中证500ETF
        '159915.XSHE',  # 创业板ETF
        '588000.XSHG',  # 科创50ETF
        '510050.XSHG',  # 上证50ETF
        '512100.XSHG',  # 中证1000ETF
        '512480.XSHG',  # 半导体ETF
        '512800.XSHG',  # 银行ETF
        '512880.XSHG',  # 证券ETF
        '515880.XSHG',  # 通信ETF
        '513050.XSHG',  # 中概互联网ETF
        '513180.XSHG',  # 恒生科技ETF
        '518880.XSHG',  # 黄金ETF
    ]
    context.defensive_etf = '511880.XSHG'          # 银华日利货币ETF
    context.global_pool = ['511880.XSHG', '510300.XSHG']  # 保留定义,防御逻辑已改用 defensive_etf
    context.benchmark = '000001.XSHG'

    # ---- 多维超参（12 组,r2_min 整体抬高 0.10,减少弱信号入场）----
    context.hyperparams = [
        {'lookback': 15, 'r2_min': 0.40, 'ma_lb': 8},
        {'lookback': 15, 'r2_min': 0.50, 'ma_lb': 20},
        {'lookback': 20, 'r2_min': 0.40, 'ma_lb': 8},
        {'lookback': 20, 'r2_min': 0.45, 'ma_lb': 15},
        {'lookback': 20, 'r2_min': 0.50, 'ma_lb': 20},
        {'lookback': 25, 'r2_min': 0.40, 'ma_lb': 8},
        {'lookback': 25, 'r2_min': 0.50, 'ma_lb': 20},
        {'lookback': 30, 'r2_min': 0.45, 'ma_lb': 15},
        {'lookback': 30, 'r2_min': 0.55, 'ma_lb': 25},
        {'lookback': 40, 'r2_min': 0.40, 'ma_lb': 8},
        {'lookback': 40, 'r2_min': 0.50, 'ma_lb': 20},
        {'lookback': 40, 'r2_min': 0.55, 'ma_lb': 25},
    ]
    context.recommend_count = 2
    context.loss = 0.985                           # 连跌过滤 0.97 -> 0.985
    context.ma_threshold = 1.0
    context.min_score = 0.25                       # 0.0 -> 0.25
    context.max_score = 8.0

    # ---- 市场状态机 ----
    context.weak_ma_lookback = 10
    context.is_weak = False
    context.is_bull = False
    context.bull_level = 0
    context.attack_boost = False
    context.bull_breadth_enter = 1
    context.bull_breadth_exit = 0
    context.bull_momentum_thr = 0.03
    context.force_defense = False
    context.force_defense_days = 0
    context.max_force_defense_days = 5
    context.max_10d_loss_threshold = -0.08         # -0.105 -> -0.08

    # ---- 风控参数（全局统一,牛市进攻期亦不放宽）----
    context.stoploss_limit = 0.065                 # 硬止损 0.08 -> 0.065
    context.cost_stop_ratio = 0.09
    context.trailing_stop_ratio = 0.15
    context.trend_ma_lookback = 20                 # MA20 破位保留
    context.atr_periods = 22
    context.chandelier_multiplier = 3.0            # ATR 吊灯止损保留
    context.profit_threshold = 0.15

    # ---- 持仓粘性 + 最小持有期 ----
    context.enable_stickiness = True
    context.stickiness_freq_gap = 3                # 2 -> 3
    context.stickiness_score_ratio = 1.4           # 1.15 -> 1.4
    context.min_hold_days = 10                     # 新增:最小持有交易日数(8~12 取中)

    # ---- 跟踪变量 ----
    context.highest_close = {}
    context.daily_returns_history = []
    context.initial_value = None
    context.previous_value = None
    context.max_value = 0.0
    context.max_drawdown = 0.0
    context.hold_start = {}                        # 新增:记录持仓起始 bar 索引
    context.bar_count = 0                          # 新增:手动计数 bar


def _closes(security, count):
    try:
        return history_bars(security, count, '1d', 'close')
    except Exception:
        return None


def _momentum(closes, lookback, r2_min, ma_lb, loss_thr):
    """加权对数回归动量 + R²,附连跌与均线过滤,返回 dict 或 None。"""
    if closes is None or len(closes) < lookback + 1:
        return None
    recent = closes[-(lookback + 1):]
    if np.any(recent <= 0):
        return None
    y = np.log(recent)
    x = np.arange(len(y), dtype=float)
    w = np.linspace(1.0, 2.0, len(y))
    W = w ** 2
    Ws = np.sum(W)
    xb = np.sum(W * x) / Ws
    yb = np.sum(W * y) / Ws
    dx = x - xb
    dy = y - yb
    vx = np.sum(W * dx * dx)
    if vx <= 0:
        return None
    slope = np.sum(W * dx * dy) / vx
    intercept = yb - slope * xb
    ann = math.exp(slope * 250.0) - 1.0
    yp = slope * x + intercept
    ss_res = np.sum(w * (y - yp) ** 2)
    ss_tot = np.sum(w * (y - np.mean(y)) ** 2)
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    score = ann * r2

    if len(closes) >= 4:
        d1 = closes[-1] / closes[-2]
        d2 = closes[-2] / closes[-3]
        d3 = closes[-3] / closes[-4]
        if min(d1, d2, d3) < loss_thr:             # 使用 context.loss 传入的 0.985
            return None
    if len(closes) >= ma_lb:
        ma = float(np.mean(closes[-ma_lb:]))
        if closes[-1] <= ma * 1.0:
            return None
    return {'score': score, 'ann': ann, 'r2': r2}


def _update_market_state(context):
    idx = _closes(context.benchmark, context.weak_ma_lookback + 2)
    if idx is not None and len(idx) >= context.weak_ma_lookback:
        context.is_weak = float(idx[-1]) < float(np.mean(idx[-context.weak_ma_lookback:]))
    else:
        context.is_weak = False

    above = 0
    moms = []
    for sec in context.pool:
        c = _closes(sec, 21)
        if c is None or len(c) < 20:
            continue
        if float(c[-1]) > float(np.mean(c[-20:])):
            above += 1
        p0 = float(c[-11]) if len(c) >= 11 else float(c[0])
        if p0 > 0:
            moms.append(float(c[-1]) / p0 - 1.0)
    avg_mom = float(np.mean(moms)) if moms else 0.0
    enter = (above >= context.bull_breadth_enter) and (avg_mom >= context.bull_momentum_thr)
    exit_ = (above < context.bull_breadth_exit) or (avg_mom < 0)
    if context.is_bull:
        if exit_:
            context.is_bull = False
            context.bull_level = 0
    elif enter:
        context.is_bull = True
        context.bull_level = 2 if avg_mom > 0.08 else 1
    context.attack_boost = context.is_bull and (not context.is_weak) and (not context.force_defense)


def _consensus(context):
    """多参数动量共识:返回 [(sec, wfreq, avg_score)],按频率与分数降序。"""
    appear = {}
    score_sum = {}
    for hp in context.hyperparams:
        candidates = []
        for sec in context.pool:
            closes = _closes(sec, max(hp['lookback'], hp['ma_lb']) + 40)
            m = _momentum(closes, hp['lookback'], hp['r2_min'], hp['ma_lb'], context.loss)
            if m is None:
                continue
            if not (context.min_score <= m['score'] <= context.max_score):
                continue
            if m['r2'] <= hp['r2_min']:
                continue
            candidates.append((m['score'], sec))
        candidates.sort(reverse=True)
        for rank, (sc, sec) in enumerate(candidates[:context.recommend_count]):
            weight = 1.0 if rank == 0 else 0.5
            appear[sec] = appear.get(sec, 0.0) + weight
            score_sum[sec] = score_sum.get(sec, 0.0) + sc * weight
    if not appear:
        return []
    rows = [(sec, wf, score_sum[sec] / max(wf, 1e-8)) for sec, wf in appear.items()]
    rows.sort(key=lambda x: (-x[1], -x[2]))
    return rows


def _risk_hit(context, sec, pos):
    """多层风控:返回 (是否卖出, 原因)。参数全局统一,牛市进攻期亦不放宽。"""
    if pos is None or pos.quantity <= 0:
        return False, ''
    closes = _closes(sec, max(context.atr_periods, context.trend_ma_lookback) + 5)
    if closes is None or len(closes) < 5:
        return False, ''
    px = float(closes[-1])
    cost = pos.avg_price
    if cost <= 0:
        return False, ''
    if sec not in context.highest_close:
        context.highest_close[sec] = px
    else:
        context.highest_close[sec] = max(context.highest_close[sec], px)
    hi = context.highest_close[sec]
    ret = (px - cost) / cost

    if ret <= -context.stoploss_limit:
        return True, 'hard_stop'
    if px < cost * (1.0 - context.cost_stop_ratio):
        return True, 'cost_stop'
    if px < hi * (1.0 - context.trailing_stop_ratio):
        return True, 'trailing_stop'
    if len(closes) >= context.trend_ma_lookback:
        ma_trend = float(np.mean(closes[-context.trend_ma_lookback:]))
        if px < ma_trend:
            return True, 'ma_exit'
    if len(closes) >= context.atr_periods + 1:
        atr = float(np.mean(np.abs(np.diff(closes[-context.atr_periods - 1:]))))
        if atr > 0 and px < hi - context.chandelier_multiplier * atr:
            return True, 'chandelier'
    if ret > context.profit_threshold and len(closes) >= 15:
        deltas = np.diff(closes[-15:])
        gains = np.where(deltas > 0, deltas, 0.0)
        losses = np.where(deltas < 0, -deltas, 0.0)
        ag = float(np.mean(gains[-14:]))
        al = float(np.mean(losses[-14:]))
        if al > 0 and ag / al > 7.0:
            return True, 'rsi_takeprofit'
    return False, ''


def handle_bar(context, bar_dict):
    context.bar_count += 1  # 手动计数 bar,用于最小持有期

    # 资产与回撤跟踪
    value = context.portfolio.total_value
    if context.initial_value is None:
        context.initial_value = value
    if value > context.max_value:
        context.max_value = value
    if context.max_value > 0:
        dd = (context.max_value - value) / context.max_value
        if dd > context.max_drawdown:
            context.max_drawdown = dd
    if context.previous_value is not None and context.previous_value > 0:
        context.daily_returns_history.append(value / context.previous_value - 1.0)
        if len(context.daily_returns_history) > 10:
            context.daily_returns_history.pop(0)
    context.previous_value = value

    # 市场状态与强制防御
    _update_market_state(context)
    if len(context.daily_returns_history) >= 10:
        cum = float(np.prod([1.0 + r for r in context.daily_returns_history]) - 1.0)
        if cum <= context.max_10d_loss_threshold:
            context.force_defense = True
            context.force_defense_days = 0
    if context.force_defense:
        context.force_defense_days += 1
        if context.force_defense_days >= context.max_force_defense_days:
            context.force_defense = False
            context.force_defense_days = 0

    # 共识选股
    selected = _consensus(context)

    # 当前持仓（含防御 ETF,便于正确跟踪持有天数与调仓）
    cur = None
    cur_pos = None
    for sec in context.pool + [context.defensive_etf]:
        p = context.portfolio.get_position(sec, POSITION_DIRECTION.LONG)
        if p is not None and p.quantity > 0:
            cur = sec
            cur_pos = p
            break

    # 风控卖出判定
    force_sell = False
    if cur is not None:
        force_sell, _ = _risk_hit(context, cur, cur_pos)

    # ---- 目标标的 ----
    target = None
    if context.is_weak or context.force_defense:
        # 走弱/强制防御:一律切 511880 货币ETF 空仓避险,不在弱势里轮动;
        # 属于风控行为,不受最小持有期约束。
        target = context.defensive_etf
    else:
        # 最小持有期:非风控卖出时,持有不足 min_hold_days 不换标的
        hold_days = 0
        if cur is not None:
            hold_days = context.bar_count - context.hold_start.get(cur, context.bar_count)
        in_min_hold = (cur is not None) and (not force_sell) and (hold_days < context.min_hold_days)
        if in_min_hold:
            target = cur
        elif force_sell:
            # 风控止损:允许立即换仓,不受最小持有期约束
            target = selected[0][0] if selected else None
        elif selected:
            top_sec = selected[0][0]
            # 牛市进攻期(is_bull/attack_boost)与其他状态一致,保持 13 只 ETF 池的正常轮动;
            # 单标的 95% 仓位不变,止损参数全局统一,不在牛市放宽。
            if context.enable_stickiness and cur is not None:
                cur_rank = next((i for i, x in enumerate(selected) if x[0] == cur), None)
                if cur_rank is None or cur_rank == 0:
                    target = top_sec
                else:
                    wf_gap = selected[0][1] - selected[cur_rank][1]
                    ratio = selected[0][2] / max(selected[cur_rank][2], 1e-8)
                    # 只有新标的频率领先>=3 且分数比>1.4 才换仓,否则保持原持仓
                    if (wf_gap >= context.stickiness_freq_gap and
                            ratio > context.stickiness_score_ratio):
                        target = top_sec
                    else:
                        target = cur
            else:
                target = top_sec
        # selected 为空且无持仓时,target 保持 None(空仓)

    # 调仓（单标的,95% 仓位）
    if cur is not None and (cur != target or force_sell):
        order_target_percent(cur, 0.0)
        context.highest_close.pop(cur, None)
        context.hold_start.pop(cur, None)
    if target is not None and target != cur:
        order_target_percent(target, 0.95)
        context.hold_start[target] = context.bar_count
