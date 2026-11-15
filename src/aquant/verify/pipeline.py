"""Layer 4:分层验证流程——样本内(IS)→ 样本外(OOS)→ 模拟盘引导。

同一策略、同一配置,在时间轴上切成不重叠的两段分别回测:
- 样本内段用于"策略设计与参数研究";
- 样本外段是唯一有效性证据;两段结果对比给出 pass/warning/fail 判定;
- 无论结果如何,都输出模拟盘引导清单(第三层验证),回测通过 ≠ 可以实盘。

NULL 基线:若指定 benchmark(基准指数),报告中附带"买入持有基准"对照,
任何策略必须先跑赢"什么都不做的基准"才有讨论价值(NULL 基线方法论,
借鉴 PA_Agent 的实证验证思路)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from aquant.runner.backtest import BacktestResult, run_backtest
from aquant.verify.metrics import compare_is_oos

# 第三层验证(模拟盘)引导清单:回测通过 ≠ 可以实盘,这是进入实盘前的硬门槛。
PAPER_CHECKLIST = [
    "模拟盘运行 ≥ 20 个交易日,期间不得修改参数(防再次过拟合)",
    "对比模拟盘成交价与回测撮合价,偏差 > 0.3% 的订单逐笔复盘(滑点假设校准)",
    "记录每笔订单的实际成交量占当日总成交量比例,验证 volume_percent 假设",
    "核对佣金/印花税/过户费与券商账单一致(费率配置校准)",
    "遇到涨跌停/停牌/分红除息等事件,逐一核对模拟盘与回测处理是否一致",
    "模拟盘表现进入 Layer 4 pass 状态后,方可讨论小资金实盘;实盘永远从小仓位开始",
]

# 随机交易基准(NULL 基线之二):固定种子的随机买卖策略,用来对照
# "闭着眼随机交易"的水平;任何策略跑不赢随机基准即无可讨论价值。
# {oid} 占位符由调用方按 bundle 中实际合约注入。
RANDOM_BASELINE_STRATEGY = '''
import random

_RNG = random.Random(42)  # 固定种子:保证随机基准可复现


def init(context):
    context.s1 = "{oid}"


def handle_bar(context, bar_dict):
    from rqalpha.const import POSITION_DIRECTION

    pos = context.portfolio.get_position(context.s1, POSITION_DIRECTION.LONG)
    if pos.quantity == 0 and _RNG.random() < 0.05:
        order_target_percent(context.s1, 0.5)
    elif pos.quantity > 0 and _RNG.random() < 0.05:
        order_target_percent(context.s1, 0.0)
'''


def _first_cs_oid(bundle_path: str | Path) -> str:
    """从 bundle 的 instruments.pk 取第一个股票合约 oid(随机基准的交易标的)。"""
    import pickle

    with open(Path(bundle_path) / "instruments.pk", "rb") as f:
        instruments = pickle.load(f)
    for ins in instruments:
        d = ins if isinstance(ins, dict) else getattr(ins, "__dict__", {})
        if d.get("type") == "CS":
            return str(d["order_book_id"])
    raise ValueError("bundle 中没有股票合约,无法构造随机基准")


@dataclass
class LayeredVerification:
    start: str
    end: str
    is_split: str                      # 样本内截止日 / 样本外起始日
    is_result: BacktestResult
    oos_result: BacktestResult
    comparison: dict
    random_baseline_returns: float | None = None
    paper_checklist: list[str] = field(default_factory=lambda: list(PAPER_CHECKLIST))

    def to_report(self) -> str:
        """生成人类可读的分屏验证报告。"""
        cmp = self.comparison
        lines = [
            "=" * 64,
            "分层验证报告(Layer 4)",
            f"回测区间 {self.start} ~ {self.end} | 样本内外切分点:{self.is_split}",
            "=" * 64,
            f"{'指标':<18}{'样本内 IS':>14}{'样本外 OOS':>14}",
            "-" * 64,
        ]
        m = {
            "年化收益": ("annualized_returns", "{:>14.2%}"),
            "最大回撤": ("max_drawdown", "{:>14.2%}"),
            "夏普比率": ("sharpe", "{:>14.2f}"),
            "周转率": ("turnover", "{:>14.4f}"),
        }
        im, om = cmp["is_metrics"], cmp["oos_metrics"]
        for label, (key, fmt) in m.items():
            iv, ov = im.get(key), om.get(key)
            lines.append(
                f"{label:<18}{fmt.format(iv) if iv is not None else '—':>14}"
                f"{fmt.format(ov) if ov is not None else '—':>14}"
            )
        lines.append("-" * 64)
        bm_is = self.is_result.summary.get("benchmark_total_returns")
        bm_oos = self.oos_result.summary.get("benchmark_total_returns")
        if bm_is is not None:
            lines.append(
                f"NULL 基线(买入持有 {self.is_result.summary.get('benchmark_symbol', 'benchmark')}):"
            )
            lines.append(f"  样本内段基准收益 {bm_is:.2%}" + (f" | 样本外段基准收益 {bm_oos:.2%}" if bm_oos is not None else ""))
        if self.random_baseline_returns is not None:
            lines.append(f"  随机交易基准收益(全区间,固定种子) {self.random_baseline_returns:.2%}")
        if bm_is is not None or self.random_baseline_returns is not None:
            lines.append("  (策略须先跑赢基准才有讨论价值)")
        lines.append(f"校验结果:{cmp['status'].upper()}")
        for c in cmp["checks"]:
            mark = "✓" if c["passed"] else "✗"
            val = c["value"]
            val_s = f"{val:.4f}" if isinstance(val, float) else str(val)
            lines.append(f"  [{mark}] {c['rule']}: {val_s} (要求 {c['threshold']}) {c['note']}")
        lines.append("")
        lines.append("第三层验证:模拟盘引导清单(回测通过 ≠ 可以实盘)")
        for i, item in enumerate(self.paper_checklist, 1):
            lines.append(f"  {i}. {item}")
        lines.append("=" * 64)
        return "\n".join(lines)


def load_trading_dates(bundle_path: str | Path) -> np.ndarray:
    """从 bundle 读取交易日历(YYYYMMDD int 数组,升序)。"""
    path = Path(bundle_path) / "trading_dates.npy"
    return np.load(path)


def split_is_oos(
    bundle_path: str | Path,
    start: str,
    end: str,
    is_ratio: float = 0.7,
) -> str:
    """按交易日历把 [start,end] 切成 IS/OOS,返回样本外起始日 'YYYY-MM-DD'。"""
    dates = load_trading_dates(bundle_path)
    s = int(start.replace("-", ""))
    e = int(end.replace("-", ""))
    seg = dates[(dates >= s) & (dates <= e)]
    if len(seg) < 40:
        raise ValueError(f"回测区间交易日不足({len(seg)} 天 < 40),无法做分层验证")
    cut = max(1, min(len(seg) - 1, int(len(seg) * is_ratio)))
    oos_start = str(int(seg[cut]))
    return f"{oos_start[:4]}-{oos_start[4:6]}-{oos_start[6:]}"


def layered_verification(
    code: str,
    start: str,
    end: str,
    bundle_path: str | Path,
    *,
    is_ratio: float = 0.7,
    cash: float | None = None,
    benchmark: str | None = None,
    mod_overrides: dict | None = None,
    random_baseline: bool = True,
) -> LayeredVerification:
    """执行完整分层验证(样本内/样本外两次回测 + 随机基准对照 + 对比判定)。"""
    oos_start = split_is_oos(bundle_path, start, end, is_ratio)
    # 样本内截止 = 样本外起始的上一交易日
    dates = load_trading_dates(bundle_path)
    s = int(start.replace("-", ""))
    seg = dates[(dates >= s) & (dates <= int(oos_start.replace("-", "")))]
    is_end_raw = int(seg[-2]) if len(seg) >= 2 else int(seg[-1])
    is_end = f"{is_end_raw:08d}"
    is_end = f"{is_end[:4]}-{is_end[4:6]}-{is_end[6:]}"

    common = dict(bundle_path=bundle_path, cash=cash, benchmark=benchmark, mod_overrides=mod_overrides)
    is_result = run_backtest(code, start, is_end, **common)
    oos_result = run_backtest(code, oos_start, end, **common)

    random_baseline_returns: float | None = None
    if random_baseline:
        baseline_code = RANDOM_BASELINE_STRATEGY.format(oid=_first_cs_oid(bundle_path))
        baseline_result = run_backtest(baseline_code, start, end, **common)
        raw = baseline_result.summary.get("total_returns")
        if raw is not None:
            random_baseline_returns = float(raw)

    comparison = compare_is_oos(is_result.summary, oos_result.summary)
    return LayeredVerification(
        start=start,
        end=end,
        is_split=oos_start,
        is_result=is_result,
        oos_result=oos_result,
        comparison=comparison,
        random_baseline_returns=random_baseline_returns,
    )
