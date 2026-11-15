"""Layer 1 编排入口:validate_strategy()。

Agent 生成(或用户上传)策略代码后、执行回测前的**硬门禁**:
- AST 静态检查(未来函数/可复现性/时序误用)
- 回测配置 lint(T+1/涨跌停/手续费/滑点/成交量,基于 Layer 3 注入后的最终配置)

返回 Verdict;含 error 时 runner 会拒绝执行(见 runner/backtest.py)。
"""

from __future__ import annotations

from typing import Any

from aquant.validation.ast_rules import check_ast
from aquant.validation.config_rules import check_config
from aquant.validation.findings import Verdict


def validate_strategy(code: str, run_config: dict[str, Any] | None = None) -> Verdict:
    """校验策略代码与回测配置。

    :param code: 策略源码
    :param run_config: 将要执行的完整 rqalpha 配置(base+mod);为 None 时
        仅做代码检查(如编辑阶段的即时反馈)。
    """
    findings = check_ast(code)
    if run_config is not None:
        findings.extend(check_config(run_config))
    return Verdict(tuple(findings))
