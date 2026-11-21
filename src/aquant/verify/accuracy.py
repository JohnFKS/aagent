"""回测准确性验证(闭式可对照策略):手算期望值 vs 平台输出,精确到分。

方法论(成熟量化平台的标准做法):
1. **解析对照** —— 构造结果可手算的确定性策略(固定股数、首日买入、末日卖出),
   用 bundle 原始行情 + 平台撮合/成本规则**闭式计算**期望结果,与平台输出逐项比对;
2. **恒等式核对** —— 净值 = 总资产/初始资金、费用合计 = 成交明细合计等内部一致性;
3. **税制对照** —— 验证 PIT 印花税率(2023-08-28 前千一、后万五)按卖出日正确适用。

平台撮合与成本规则(rqalpha 6.5.0 源码核实,闭式计算依据):
- 撮合价:matching_type = current_bar → **下单当日 bar 收盘价**;
- 滑点:PriceRatioSlippage(0.002,双边全额)→ 买入价 = close × 1.002,卖出价 = close × 0.998;
- 佣金:成交金额 × 0.0008,单笔最低 5 元;
- 印花税(pit_tax=True,卖出单边):卖出日 < 2023-08-28 → 金额 × 0.001(千一);
  ≥ 2023-08-28 → 金额 × 0.0005(万五,财政部 2023-08 减半征收);
- T+1 与涨跌停/成交量限制启用(100 股小额单不受约束)。

纯函数入口:``run_accuracy_checks(bundle_path=?, cash=?, shares=?)`` 返回
``{checks, all_passed, report_md, ...}``;CLI 见 scripts/verify_backtest_accuracy.py,
MCP 工具 platform_verify_accuracy / REST 后续可复用同一实现。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import h5py
import numpy as np

from aquant.config.schema import default_bundle_path
from aquant.runner.backtest import key_metrics, run_backtest

# 撮合与成本规则常量(与 defaults.yaml / rqalpha 源码一致)
SLIPPAGE_RATE = 0.002          # PriceRatioSlippage 双边全额
COMMISSION_RATE = 0.0008       # 万八
COMMISSION_MIN = 5.0           # 单笔最低佣金
PIT_TAX_CHANGE = "2023-08-28"  # 印花税减半征收生效日
TAX_OLD = 0.001                # 千一(生效日前)
TAX_NEW = 0.0005               # 万五(生效日起)

_TOLERANCE_MONEY = 0.01        # 金额容差(分)
_TOLERANCE_RATE = 1e-6         # 比率容差


def sell_tax_rate(sell_date: str) -> float:
    """PIT 印花税率:卖出日 < 2023-08-28 → 千一;否则万五。"""
    return TAX_OLD if sell_date < PIT_TAX_CHANGE else TAX_NEW


def _read_bundle(bundle: Path) -> dict:
    """独立读取 bundle 原始行情(h5 直读,不经 rqalpha 引擎)。"""
    cal = np.load(bundle / "trading_dates.npy")
    ymd = lambda v: f"{int(v):08d}"[:4] + "-" + f"{int(v):08d}"[4:6] + "-" + f"{int(v):08d}"[6:]
    calendar = [ymd(v) for v in cal]
    import pickle

    with open(bundle / "instruments.pk", "rb") as f:
        instruments = pickle.load(f)
    cs_oids = [i["order_book_id"] for i in instruments if i.get("type") == "CS"]
    closes: dict[str, dict[str, float]] = {}
    with h5py.File(bundle / "stocks.h5") as h5:
        for oid in cs_oids:
            if oid not in h5:
                continue
            rows = h5[oid][:]
            closes[oid] = {
                f"{int(r['datetime']) // 1_000_000:08d}"[:4] + "-"
                + f"{int(r['datetime']) // 1_000_000:08d}"[4:6] + "-"
                + f"{int(r['datetime']) // 1_000_000:08d}"[6:]: float(r["close"])
                for r in rows
            }
    return {"calendar": calendar, "closes": closes, "cs_oids": cs_oids}


def _closed_form(
    cash0: float, shares: int, close_buy: float, close_sell: float, sell_date: str
) -> dict[str, float]:
    """买入持有卖出的闭式期望结果(逐条公式即撮合/成本规则)。"""
    price_buy = close_buy * (1 + SLIPPAGE_RATE)
    buy_amount = shares * price_buy
    commission_buy = max(buy_amount * COMMISSION_RATE, COMMISSION_MIN)
    cash_after_buy = cash0 - buy_amount - commission_buy

    price_sell = close_sell * (1 - SLIPPAGE_RATE)
    sell_amount = shares * price_sell
    commission_sell = max(sell_amount * COMMISSION_RATE, COMMISSION_MIN)
    tax_sell = sell_amount * sell_tax_rate(sell_date)

    final_cash = cash_after_buy + sell_amount - commission_sell - tax_sell
    return {
        "price_buy": price_buy,
        "buy_amount": buy_amount,
        "commission_buy": commission_buy,
        "price_sell": price_sell,
        "sell_amount": sell_amount,
        "commission_sell": commission_sell,
        "tax_sell": tax_sell,
        "tax_rate": sell_tax_rate(sell_date),
        "final_cash": final_cash,
        "total_returns": final_cash / cash0 - 1,
    }


def run_accuracy_checks(
    bundle_path: str | Path | None = None,
    cash: float = 1_000_000.0,
    shares: int = 100,
) -> dict[str, Any]:
    """运行回测准确性验证套件,返回 {checks, all_passed, report_md, ...}。"""
    bundle = Path(bundle_path or default_bundle_path())
    try:
        data = _read_bundle(bundle)
    except Exception as exc:  # noqa: BLE001 - bundle 缺失/损坏给出可读报告而非异常
        return {
            "checks": [],
            "all_passed": False,
            "report_md": (
                f"# 回测准确性验证报告(闭式对照)\n\n"
                f"bundle 不可用(`{bundle}`:{type(exc).__name__}: {exc})。\n"
                "请先用 aquant.data.build_bundle 构建,或设 AQUANT_BUNDLE_PATH 指向有效 bundle。"
            ),
            "bundle": str(bundle),
        }
    calendar: list[str] = data["calendar"]
    closes: dict[str, dict[str, float]] = data["closes"]

    checks: list[dict[str, Any]] = []

    def check(name: str, expected: Any, actual: Any, *, tol: float = 0.0, note: str = "") -> None:
        if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
            diff = abs(float(expected) - float(actual))
            ok = diff <= tol
            checks.append({
                "name": name, "pass": ok, "expected": expected, "actual": actual,
                "diff": round(diff, 8), "tolerance": tol, "note": note,
            })
        else:
            ok = expected == actual
            checks.append({
                "name": name, "pass": ok, "expected": expected, "actual": actual,
                "diff": None, "tolerance": None, "note": note,
            })

    if not calendar or not closes:
        return {"checks": [], "all_passed": False, "report_md": "bundle 无 CS 行情,无法验证。",
                "bundle": str(bundle)}

    oid = "600519.XSHG" if "600519.XSHG" in closes else data["cs_oids"][0]
    close_map = closes[oid]
    # 买卖日:首/末个"该合约有行情"的交易日
    trade_days = [d for d in calendar if d in close_map]
    if len(trade_days) < 2:
        return {"checks": [], "all_passed": False, "report_md": "交易日不足,无法验证。",
                "bundle": str(bundle)}
    buy_day, sell_day = trade_days[0], trade_days[-1]
    close_buy, close_sell = close_map[buy_day], close_map[sell_day]

    # ---- T1:零交易策略(资金恒等:无任何隐含成本) --------------------------
    t1_code = f'''
def init(context):
    context.s1 = "{oid}"

def handle_bar(context, bar_dict):
    pass
'''
    t1 = run_backtest(t1_code, buy_day, sell_day, bundle_path=bundle, cash=cash)
    t1_metrics = key_metrics(t1)
    t1_trades = t1.trades
    t1_count = len(t1_trades) if hasattr(t1_trades, "__len__") else 0
    check("T1·零交易:成交笔数", 0, t1_count, note="不产生任何订单")
    # 直接核对费用与收益恒为零
    check("T1·零交易:总收益 = 0", 0.0, t1_metrics.get("total_returns") or 0.0, tol=_TOLERANCE_RATE,
          note="不下单就没有任何费用与收益")
    check("T1·零交易:佣金 = 0", 0.0, t1_metrics.get("total_commission") or 0.0, tol=_TOLERANCE_MONEY)
    check("T1·零交易:印花税 = 0", 0.0, t1_metrics.get("total_tax") or 0.0, tol=_TOLERANCE_MONEY)

    # ---- T2:买入持有卖出(闭式对照) ----------------------------------------
    t2_code = f'''
from rqalpha.const import POSITION_DIRECTION

def init(context):
    context.s1 = "{oid}"
    context.bought = False

def handle_bar(context, bar_dict):
    if not context.bought:
        order_shares(context.s1, {shares})
        context.bought = True
    elif str(context.now.date()) == "{sell_day}":
        order_shares(context.s1, -{shares})
'''
    t2 = run_backtest(t2_code, buy_day, sell_day, bundle_path=bundle, cash=cash)
    exp = _closed_form(cash, shares, close_buy, close_sell, sell_day)
    t2_metrics = key_metrics(t2)

    trades = t2.trades
    if hasattr(trades, "to_dict"):
        trade_rows = trades.to_dict("records")
    else:
        trade_rows = [
            {
                "price": getattr(t, "last_price", 0),
                "quantity": getattr(t, "last_quantity", 0),
                "commission": getattr(t, "commission", 0),
                "tax": getattr(t, "tax", 0),
                "side": str(getattr(t, "side", "")),
            }
            for t in (trades or [])
        ]
    buys = [t for t in trade_rows if "BUY" in str(t.get("side", ""))]
    sells = [t for t in trade_rows if "SELL" in str(t.get("side", ""))]

    check("T2·买入日", buy_day, t2.start, note="首交易日即买入")
    check("T2·卖出日", sell_day, t2.end, note="末日卖出")
    check("T2·成交笔数", 2, len(trade_rows), note="一笔买入 + 一笔卖出")

    def _price(row):
        return float(row.get("price", row.get("last_price", 0)) or 0)

    def _qty(row):
        return float(row.get("quantity", row.get("last_quantity", 0)) or 0)

    if buys:
        check("T2·买入价 = close×1.002(滑点)", exp["price_buy"], _price(buys[0]),
              tol=1e-4, note=f"close({buy_day})={close_buy}")
        check("T2·买入佣金 = max(金额×万八, 5)", exp["commission_buy"],
              float(buys[0].get("commission", 0)), tol=_TOLERANCE_MONEY)
    if sells:
        check("T2·卖出价 = close×0.998(滑点)", exp["price_sell"], _price(sells[0]),
              tol=1e-4, note=f"close({sell_day})={close_sell}")
        check("T2·卖出佣金", exp["commission_sell"], float(sells[0].get("commission", 0)),
              tol=_TOLERANCE_MONEY)
        sell_amount_actual = _price(sells[0]) * _qty(sells[0])
        implied_tax_rate = (float(sells[0].get("tax", 0)) / sell_amount_actual) if sell_amount_actual else 0.0
        check("T2·卖出印花税率(PIT)", exp["tax_rate"], implied_tax_rate,
              tol=_TOLERANCE_RATE,
              note=f"卖出日 {sell_day} {'< 2023-08-28 → 千一' if exp['tax_rate'] == TAX_OLD else '≥ 2023-08-28 → 万五'}")

    check("T2·期末总资产(闭式)", exp["final_cash"], t2_metrics.get("total_returns", 0) * cash + cash,
          tol=_TOLERANCE_MONEY, note="cash0 − 买入成本 + 卖出净回款")
    check("T2·总收益率(闭式)", exp["total_returns"], t2_metrics.get("total_returns") or 0.0,
          tol=_TOLERANCE_RATE)

    # ---- T3:恒等式(内部一致性,与价格无关) --------------------------------
    nv_last = None
    if t2.portfolio is not None and len(t2.portfolio):
        try:
            nv_last = float(t2.portfolio["unit_net_value"].iloc[-1])
        except Exception:
            nv_last = None
    if nv_last is not None:
        check("T3·净值恒等式:末净值 = 1 + 总收益", 1 + (t2_metrics.get("total_returns") or 0.0),
              nv_last, tol=_TOLERANCE_RATE)
    check("T3·费用恒等式:佣金合计 = 逐笔合计",
          sum(float(t.get("commission", 0)) for t in trade_rows),
          t2_metrics.get("total_commission") or 0.0, tol=_TOLERANCE_MONEY)
    check("T3·费用恒等式:印花税合计 = 逐笔合计",
          sum(float(t.get("tax", 0)) for t in trade_rows),
          t2_metrics.get("total_tax") or 0.0, tol=_TOLERANCE_MONEY)

    # ---- T4:PIT 印花税率对照(若日历跨越减半征收日) ------------------------
    pre_change = [d for d in trade_days if d < PIT_TAX_CHANGE]
    post_change = [d for d in trade_days if d >= PIT_TAX_CHANGE]
    if pre_change and post_change:
        # 千一段:买入首日、卖出减半日前一天
        sell_day_old = pre_change[-1]
        t4_code = f'''
from rqalpha.const import POSITION_DIRECTION

def init(context):
    context.s1 = "{oid}"
    context.bought = False

def handle_bar(context, bar_dict):
    if not context.bought:
        order_shares(context.s1, {shares})
        context.bought = True
    elif str(context.now.date()) == "{sell_day_old}":
        order_shares(context.s1, -{shares})
'''
        t4 = run_backtest(t4_code, trade_days[0], sell_day_old, bundle_path=bundle, cash=cash)
        t4_metrics = key_metrics(t4)
        t4_trades = t4.trades.to_dict("records") if hasattr(t4.trades, "to_dict") else []
        t4_sells = [t for t in t4_trades if "SELL" in str(t.get("side", ""))]
        if t4_sells:
            row0 = t4_sells[0]
            p_old = float(row0.get("price", row0.get("last_price", 0)) or 0)
            q_old = float(row0.get("quantity", row0.get("last_quantity", 0)) or 0)
            sell_amount_old = p_old * q_old
            implied = float(t4_sells[0]["tax"]) / sell_amount_old if sell_amount_old else 0.0
            check("T4·PIT 税率对照(减半前 = 千一)", TAX_OLD, implied, tol=_TOLERANCE_RATE,
                  note=f"卖出日 {sell_day_old} < {PIT_TAX_CHANGE}")

    all_passed = all(c["pass"] for c in checks)

    # ---- 报告 --------------------------------------------------------------
    lines = [
        "# 回测准确性验证报告(闭式对照)",
        "",
        f"- bundle:`{bundle}`;标的:{oid};股数:{shares};初始资金:{cash:,.0f}",
        f"- 买入日:{buy_day}(close {close_buy});卖出日:{sell_day}(close {close_sell})",
        "- 闭式公式:买入价 = close×1.002(滑点);卖出价 = close×0.998;佣金 = max(金额×0.0008, 5);"
        f"印花税 = 金额×{sell_tax_rate(sell_day)}(PIT:{sell_day} {'≥' if sell_day >= PIT_TAX_CHANGE else '<'} {PIT_TAX_CHANGE})",
        "",
        "| # | 检查项 | 期望 | 实际 | 差异 | 结果 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for i, c in enumerate(checks, 1):
        diff_txt = f"{c['diff']:.6g}" if c["diff"] is not None else "—"
        expected = c["expected"] if not isinstance(c["expected"], float) else round(c["expected"], 6)
        actual = c["actual"] if not isinstance(c["actual"], float) else round(c["actual"], 6)
        lines.append(
            f"| {i} | {c['name']} | {expected} | {actual} | {diff_txt} | {'✅' if c['pass'] else '❌'} |"
        )
    lines.append("")
    lines.append(
        f"**结论:{'✅ 全部通过 —— 平台撮合、滑点、佣金、PIT 印花税与资金核算与闭式手算一致,可验证为准确。' if all_passed else '❌ 存在不一致项,请检查上方差异列。'}**"
    )
    lines.append("")
    lines.append(
        "> 方法:期望值由 bundle 原始行情(h5 直读)+ 平台撮合/成本规则闭式计算,与引擎输出逐项比对;"
        "恒等式类检查与价格路径无关,适用于任何策略。"
    )

    return {
        "bundle": str(bundle),
        "instrument": oid,
        "window": {"buy_day": buy_day, "sell_day": sell_day},
        "expected": exp,
        "checks": checks,
        "all_passed": all_passed,
        "report_md": "\n".join(lines),
    }
