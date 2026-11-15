"""Layer 4:指标提取、IS/OOS 对比判定与多策略候选排名。"""

from __future__ import annotations

from typing import Any


def _num(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None  # 过滤 NaN


def compare_is_oos(is_summary: dict, oos_summary: dict) -> dict[str, Any]:
    """样本内 vs 样本外判定。

    规则(宁可错杀,不可放过):
    - OOS 年化收益为负 → fail(策略不具备样本外有效性)
    - OOS 最大回撤 > min(1.5 × IS 回撤, 40%) → fail
    - OOS 夏普 < 0.5 × IS 夏普(IS 夏普>0 时)→ warning(衰减过大)
    - OOS 周转率与 IS 相差 2 倍以上 → warning(风格漂移信号)
    判定阈值在报告页全部展示,用户可自行复核。
    """
    is_ann, oos_ann = _num(is_summary.get("annualized_returns")), _num(oos_summary.get("annualized_returns"))
    is_dd, oos_dd = abs(_num(is_summary.get("max_drawdown")) or 0.0), abs(
        _num(oos_summary.get("max_drawdown")) or 0.0
    )
    is_sharpe, oos_sharpe = _num(is_summary.get("sharpe")), _num(oos_summary.get("sharpe"))
    is_to, oos_to = _num(is_summary.get("turnover")), _num(oos_summary.get("turnover"))

    checks: list[dict[str, Any]] = []
    status = "pass"

    # 1) 样本外收益
    ok1 = oos_ann is not None and oos_ann > 0
    checks.append(
        {
            "rule": "oos_return_positive",
            "value": oos_ann,
            "threshold": "> 0",
            "passed": ok1,
            "note": "样本外年化收益必须为正,否则策略不具备样本外有效性",
        }
    )
    if not ok1:
        status = "fail"

    # 2) 样本外回撤
    dd_limit = min(is_dd * 1.5 + 1e-12, 0.40)
    ok2 = oos_dd is not None and oos_dd <= dd_limit
    checks.append(
        {
            "rule": "oos_drawdown",
            "value": oos_dd,
            "threshold": f"<= {dd_limit:.4f} (min(1.5×IS, 40%))",
            "passed": ok2,
            "note": "样本外回撤显著放大通常意味着参数过拟合样本内行情",
        }
    )
    if not ok2:
        status = "fail"

    # 3) 夏普衰减
    if is_sharpe is not None and is_sharpe > 0 and oos_sharpe is not None:
        ok3 = oos_sharpe >= 0.5 * is_sharpe
        checks.append(
            {
                "rule": "sharpe_decay",
                "value": oos_sharpe,
                "threshold": f">= {0.5 * is_sharpe:.4f} (0.5×IS)",
                "passed": ok3,
                "note": "夏普腰斩提示策略可能拟合了样本内特有行情",
            }
        )
        if not ok3 and status == "pass":
            status = "warning"

    # 4) 周转率漂移
    if is_to and oos_to:
        ok4 = oos_to <= 2.0 * is_to
        checks.append(
            {
                "rule": "turnover_drift",
                "value": oos_to,
                "threshold": f"<= {2.0 * is_to:.4f} (2×IS)",
                "passed": ok4,
                "note": "样本外周转率暴增往往伴随成交成本激增,警惕过拟合或失效",
            }
        )
        if not ok4 and status == "pass":
            status = "warning"

    return {
        "status": status,
        "is_metrics": {
            "annualized_returns": is_ann,
            "max_drawdown": is_dd,
            "sharpe": is_sharpe,
            "turnover": is_to,
        },
        "oos_metrics": {
            "annualized_returns": oos_ann,
            "max_drawdown": oos_dd,
            "sharpe": oos_sharpe,
            "turnover": oos_to,
        },
        "checks": checks,
    }


def rank_candidates(summaries: dict[str, dict]) -> list[dict[str, Any]]:
    """多策略候选排名(设计依据:多 Agent 同场竞技、按表现动态排名)。

    :param summaries: {候选名: sys_analyser summary dict}
    :returns: 按 (年化收益, 夏普) 降序的排名列表,附排名与核心指标。
    """
    ranked = []
    for name, s in summaries.items():
        ranked.append(
            {
                "name": name,
                "annualized_returns": _num(s.get("annualized_returns")),
                "sharpe": _num(s.get("sharpe")),
                "max_drawdown": abs(_num(s.get("max_drawdown")) or 0.0),
                "total_returns": _num(s.get("total_returns")),
                "turnover": _num(s.get("turnover")),
            }
        )
    ranked.sort(
        key=lambda r: (r["annualized_returns"] if r["annualized_returns"] is not None else float("-inf"),
                       r["sharpe"] if r["sharpe"] is not None else float("-inf")),
        reverse=True,
    )
    for i, r in enumerate(ranked, 1):
        r["rank"] = i
    return ranked


def render_ranking(ranked: list[dict[str, Any]]) -> str:
    """排名表格的文本渲染(用于对比报告与对话回复)。"""
    lines = [f"{'排名':<6}{'候选':<24}{'年化收益':>12}{'夏普':>10}{'最大回撤':>12}"]
    for r in ranked:
        ann = f"{r['annualized_returns']:.2%}" if r["annualized_returns"] is not None else "—"
        shp = f"{r['sharpe']:.2f}" if r["sharpe"] is not None else "—"
        dd = f"{r['max_drawdown']:.2%}" if r["max_drawdown"] is not None else "—"
        lines.append(f"{r['rank']:<6}{r['name']:<24}{ann:>12}{shp:>10}{dd:>12}")
    return "\n".join(lines)
