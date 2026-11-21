"""回测结果人读报告(Markdown)—— 供 agent 在自己框架的对话里直接引用汇报。

输入是 RealRunnerAdapter 归一化后的结果 dict(summary/portfolio/benchmark_portfolio/
trades/verdict/bundle_extension 等),与 /api/backtest、MCP direct 模式同构。
报告只做确定性的数字整理与解读,不做 LLM 润色 —— agent 拿到后用自己的口吻转述。
"""

from __future__ import annotations

from typing import Any

import pandas as pd

_PERCENT_KEYS = {
    "total_returns", "annualized_returns", "benchmark_total_returns",
    "max_drawdown", "volatility", "alpha", "beta", "turnover", "win_rate",
}

_LABELS = {
    "total_returns": "总收益",
    "annualized_returns": "年化收益",
    "benchmark_total_returns": "基准收益",
    "max_drawdown": "最大回撤",
    "sharpe": "夏普比率",
    "volatility": "波动率",
    "alpha": "Alpha",
    "beta": "Beta",
    "turnover": "换手率",
    "win_rate": "胜率",
    "total_commission": "总佣金",
    "total_tax": "印花税",
    "trading_days": "交易日",
    "start": "起始日",
    "end": "结束日",
}


def _fmt(key: str, value: Any) -> str:
    if value is None or value == "":
        return "—"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    if key in _PERCENT_KEYS:
        return f"{num * 100:.2f}%"
    if key in ("total_commission", "total_tax"):
        return f"{num:,.2f} 元"
    if key == "sharpe" or key == "beta":
        return f"{num:.2f}"
    return f"{num:,.2f}" if abs(num) >= 1000 else f"{num:.4f}"


def _pct(v: Any) -> str:
    try:
        num = float(v)
        return f"{num * 100:.2f}%"
    except (TypeError, ValueError):
        return "—"


def _net_value_summary(series: list[dict[str, Any]]) -> dict[str, Any] | None:
    """净值序列摘要:首末值/峰值/谷底与最大回撤区间。"""
    pts = []
    for item in series or []:
        date = str(item.get("date", ""))[:10]
        try:
            value = float(item.get("net_value"))
        except (TypeError, ValueError):
            continue
        if date and value > 0:
            pts.append((date, value))
    if len(pts) < 2:
        return None
    peak_val, trough_val, peak = pts[0][1], pts[0][1], pts[0]
    max_dd, dd_peak, dd_trough = 0.0, pts[0], pts[0]
    cur_peak = pts[0]
    for date, value in pts:
        if value >= cur_peak[1]:
            cur_peak = (date, value)
            continue
        dd = 1 - value / cur_peak[1]
        if dd > max_dd:
            max_dd, dd_peak, dd_trough = dd, cur_peak, (date, value)
        if value < trough_val:
            trough_val = value
        if value > peak_val:
            peak_val = value
    return {
        "first": pts[0], "last": pts[-1], "peak": (pts[0][0], peak_val),
        "trough": (pts[0][0], trough_val), "max_dd": max_dd,
        "dd_window": (dd_peak[0], dd_trough[0]) if max_dd > 0 else None,
    }


def format_backtest_report(result: dict[str, Any], *, benchmark_label: str | None = None) -> str:
    """把回测结果整理为面向用户的 Markdown 报告(agent 对话可直接引用)。

    :param result: 兼容两种键名 —— REST/MCP 响应的 ``metrics`` 与 adapter 归一化的
        ``summary``;其余键(verdict/portfolio_series/trades/bundle_extension)同构。
    """
    summary = result.get("metrics") or result.get("summary") or {}
    metrics = summary if summary else {}
    lines: list[str] = ["## 回测报告", ""]

    # ---- 头部:区间 + 徽章 ----
    unsafe = bool(metrics.get("unsafe") or result.get("unsafe"))
    exec_error = result.get("execution_error")
    start_day = metrics.get("start") or result.get("start")
    end_day = metrics.get("end") or result.get("end")
    lines.append(f"- 回测区间:{start_day or '—'} ~ {end_day or '—'}"
                 f"(交易日 {metrics.get('trading_days', '—')} 天)")
    if unsafe:
        lines.append("- ⚠️ 本次结果带 unsafe 标记:校验存在被豁免的 ERROR,仅供研究对比。")
    ext = result.get("bundle_extension")
    if isinstance(ext, dict) and ext.get("extended"):
        lines.append(f"- 🔄 数据已自动更新:{ext['prior']['end']} → {ext['now']['end']}"
                     "(拉取最新行情,旧数据已清理)")
    lines.append("")

    if exec_error:
        lines.append("### 回测执行失败")
        lines.append("")
        lines.append("策略通过了静态校验,但撮合阶段崩溃。常见原因与修法:")
        lines.append("")
        lines.append("- `history_bars` 单字段请求返回**一维数组**,直接 `prices[-n:].mean()`," 
                     "不要 `prices['close']` 取列;")
        lines.append("- 下单前判断数组长度与标的当日是否停牌;")
        lines.append("")
        lines.append("```")
        lines.append(str(exec_error)[-600:])
        lines.append("```")
        return "\n".join(lines)

    # ---- 核心指标表 ----
    lines.append("### 核心指标")
    lines.append("")
    lines.append("| 指标 | 数值 |")
    lines.append("| --- | --- |")
    show_keys = [
        "total_returns", "annualized_returns", "benchmark_total_returns",
        "max_drawdown", "sharpe", "volatility", "alpha", "beta",
        "turnover", "win_rate", "total_commission", "total_tax",
    ]
    for key in show_keys:
        if key in metrics:
            lines.append(f"| {_LABELS.get(key, key)} | {_fmt(key, metrics.get(key))} |")
    lines.append("")

    # ---- 基准对比 ----
    bm_total = metrics.get("benchmark_total_returns")
    total = metrics.get("total_returns")
    label = benchmark_label or "A股基准"
    if bm_total is not None and total is not None:
        try:
            excess = float(total) - float(bm_total)
            verdict_txt = "跑赢" if excess > 0 else ("跑输" if excess < 0 else "持平")
            lines.append(f"### 相对基准({label})")
            lines.append("")
            lines.append(
                f"- 策略 { _pct(total) } vs 基准 { _pct(bm_total) } → "
                f"**{verdict_txt} {abs(excess) * 100:.2f} 个百分点**"
                f"(Alpha {_fmt('alpha', metrics.get('alpha'))},Beta {_fmt('beta', metrics.get('beta'))})"
            )
            lines.append("")
        except (TypeError, ValueError):
            pass

    # ---- 净值曲线摘要 ----
    nv = _net_value_summary(result.get("portfolio_series"))
    if nv:
        lines.append("### 净值走势(首日归一 = 1.0)")
        lines.append("")
        lines.append(
            f"- 期末净值 {_fmt('net', nv['last'][1])}"
            f"(区间峰值 {_fmt('net', nv['peak'][1])},谷底 {_fmt('net', nv['trough'][1])})"
        )
        if nv["dd_window"]:
            lines.append(
                f"- 最大回撤 **{_pct(nv['max_dd'])}**,出现在 {nv['dd_window'][0]} → {nv['dd_window'][1]}"
            )
        lines.append("")

    # ---- 成交与成本 ----
    trades = result.get("trades") or []
    trades_count = result.get("trades_count", 0)
    if trades and not isinstance(trades, pd.DataFrame):
        trades_count = len(trades)
    lines.append("### 成交与成本")
    lines.append("")
    lines.append(
        f"- 成交 {trades_count} 笔;"
        f"总佣金 {_fmt('total_commission', metrics.get('total_commission'))};"
        f"印花税 {_fmt('total_tax', metrics.get('total_tax'))}"
        "(万八佣金 + 千一印花税 + 0.2% 滑点已计入收益)"
    )
    lines.append("")

    # ---- 校验发现 ----
    verdict = result.get("verdict") or {}
    findings = verdict.get("findings") or []
    if findings:
        lines.append("### 校验结论(Layer 1)")
        lines.append("")
        for f in findings:
            severity = f.get("severity", "")
            icon = {"error": "✗", "warning": "⚠", "info": "ℹ"}.get(str(severity).lower(), "•")
            lines.append(f"- {icon} [{f.get('rule', '')}] {f.get('message', '')}")
        lines.append("")

    # ---- 成交明细(前 20 笔) ----
    trades = result.get("trades") or []
    if trades and not isinstance(trades, pd.DataFrame):
        lines.append("### 成交明细(最近 20 笔,完整明细见 trades 字段)")
        lines.append("")
        lines.append("| 时间 | 标的 | 方向 | 价格 | 数量 | 金额 | 费用 |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for t in list(reversed(trades))[:20]:
            side = "买入" if str(t.get("side", "")).upper() == "BUY" else "卖出"
            oid = t.get("order_book_id", "")
            name = t.get("symbol", "")
            amount = t.get("amount", float(t.get("price", 0)) * float(t.get("quantity", 0)))
            fee = float(t.get("commission", 0) or 0) + float(t.get("tax", 0) or 0)
            lines.append(
                f"| {str(t.get('date', ''))[:16]} | {oid}({name}) | {side} "
                f"| {float(t.get('price', 0)):,.3f} | {int(t.get('quantity', 0))} "
                f"| {float(amount):,.2f} | {fee:,.2f} |"
            )
        lines.append("")

    # ---- 运行日志(末 40 行,聚宽式) ----
    run_log = result.get("run_log") or {}
    log_lines = run_log.get("lines") or [] if isinstance(run_log, dict) else []
    if log_lines:
        lines.append("### 运行日志(末 40 行,完整日志见 run_log 字段)")
        lines.append("")
        lines.append("```")
        lines.extend(log_lines[-40:])
        lines.append("```")
        lines.append("")

    # ---- 下一步建议(确定性规则,不含 LLM) ----
    lines.append("### 提示")
    lines.append("")
    hints: list[str] = []
    if metrics.get("total_returns") == 0 and not trades:
        hints.append("区间内零成交:信号未触发(可能阈值过严),可放宽条件或换标的/区间再试。")
    if isinstance(metrics.get("max_drawdown"), (int, float)) and metrics["max_drawdown"] > 0.3:
        hints.append("最大回撤 >30%:考虑加趋势过滤/止损降低回撤。")
    if isinstance(metrics.get("turnover"), (int, float)) and metrics["turnover"] > 0.2:
        hints.append("换手率偏高:成本侵蚀收益,可加信号过滤降低交易频率。")
    if bm_total is None:
        hints.append("本次未设基准:可传入 benchmark 参数(如 000001.XSHG)对照大盘。")
    if not hints:
        hints.append("防过拟合:用 platform_verify_layered 做样本内/样本外分层验证后再下结论。")
    for hint in hints:
        lines.append(f"- {hint}")
    lines.append("")
    return "\n".join(lines)
