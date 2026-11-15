"""ValidationAgent:Layer 1 校验的 Agent 包装 + 面向用户的中文摘要。

校验本身仍是 aquant.validation.validate_strategy(唯一事实来源,本模块不复制规则),
Agent 只负责调用与结果呈现:
- validate(code, run_config):run_config 为 None 时仅校验代码(编辑阶段即时反馈);
- summarize(verdict):按 severity 分组(error→✗ / warning→⚠ / info→ℹ)的中文摘要。
"""

from __future__ import annotations

from typing import Any

from aquant.validation.findings import Severity, Verdict
from aquant.validation.validator import validate_strategy

_SEVERITY_ORDER: tuple[Severity, ...] = (Severity.ERROR, Severity.WARNING, Severity.INFO)
_PREFIX: dict[Severity, str] = {
    Severity.ERROR: "✗",
    Severity.WARNING: "⚠",
    Severity.INFO: "ℹ",
}


class ValidationAgent:
    """校验子 agent:编排管线中的"硬门禁"执行者。"""

    def validate(self, code: str, run_config: dict[str, Any] | None = None) -> Verdict:
        """校验策略代码;提供 run_config 时连同配置 lint 一并检查。"""
        return validate_strategy(code, run_config)

    def summarize(self, verdict: Verdict) -> str:
        """生成面向用户的中文摘要:头部结论 + 按 severity 分组的条目。"""
        if not verdict.findings:
            return "校验通过:未发现任何问题。"
        grouped: dict[Severity, list[str]] = {s: [] for s in _SEVERITY_ORDER}
        for f in verdict.findings:
            grouped[f.severity].append(f"{_PREFIX[f.severity]} [{f.rule.value}] {f.message}")
        info_count = len(grouped[Severity.INFO])
        header = (
            f"校验{'未通过' if verdict.has_error else '通过'}:"
            f"{verdict.error_count} 个错误 / {verdict.warning_count} 个风险提示 / "
            f"{info_count} 条说明。"
        )
        lines = [header]
        for severity in _SEVERITY_ORDER:
            lines.extend(grouped[severity])
        return "\n".join(lines)
