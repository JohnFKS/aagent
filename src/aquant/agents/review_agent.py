"""ReviewAgent:合并规则校验 findings 与 LLM 人工评审 JSON。

设计(借鉴 PA_Agent 的自我复盘循环):
- 规则校验(Layer 1)的 findings 是硬结论,永远保留;
- LLM 评审(validation_reviewer.md 角色)只做"人工级补充",其 JSON 解析失败时
  降级为空列表 + 一条 warning,绝不影响硬门禁;
- 合并后按 (rule, message) 去重,规则结论优先于 LLM 结论。
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable

from aquant.agents.llm import LLMClient
from aquant.agents.prompts import render_prompt
from aquant.validation.findings import Finding

# LLM 评审输出解析失败时的降级标记(合入结果,severity=warning)
PARSE_FAILED_RULE = "llm.review_parse_failed"
_PARSE_FAILED_MESSAGE = (
    "LLM 评审输出解析失败,已降级为仅规则校验结果(不影响 Layer 1 硬门禁)。"
)

_VALID_SEVERITIES = {"error", "warning", "info"}


def _finding_to_dict(f: Finding | dict[str, Any]) -> dict[str, Any]:
    """Finding 对象与 dict 两种输入统一为 dict。"""
    if isinstance(f, Finding):
        d = f.to_dict()
        return {"rule": d["rule"], "severity": d["severity"], "message": d["message"]}
    return {
        "rule": str(f.get("rule", "llm.review")),
        "severity": str(f.get("severity", "info")),
        "message": str(f.get("message", "")),
    }


def _normalize_severity(value: str) -> str:
    """LLM 输出的 severity 归一化;未知取值保守降级为 info。"""
    value = value.strip().lower()
    return value if value in _VALID_SEVERITIES else "info"


def _extract_llm_json(text: str) -> list[dict[str, Any]] | None:
    """从 LLM 输出中提取 {"findings": [...]} 的 findings 列表;失败返回 None。

    容忍 markdown 围栏与前后杂文:直接定位首个 { 到最后一个 } 的片段解析。
    """
    text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("findings"), list):
        return None
    out: list[dict[str, Any]] = []
    for item in data["findings"]:
        if not isinstance(item, dict) or not item.get("rule") or not item.get("message"):
            continue  # 结构不完整的条目直接丢弃
        out.append(
            {
                "rule": str(item["rule"]),
                "severity": _normalize_severity(str(item.get("severity", "info"))),
                "message": str(item["message"]),
            }
        )
    return out


class ReviewAgent:
    """评审子 agent:规则结论 + LLM 复核 → 去重后的 findings 列表。"""

    def __init__(self, llm: LLMClient, rule_findings: Iterable[Finding | dict[str, Any]]) -> None:
        self.llm = llm
        self.rule_findings = [_finding_to_dict(f) for f in rule_findings]

    def review(self, code: str) -> list[dict[str, Any]]:
        """对代码做 LLM 评审并与规则 findings 合并去重。"""
        llm_findings, degraded = self._llm_review(code)
        merged = self._dedupe(self.rule_findings + llm_findings)
        if degraded:
            merged.append({"rule": PARSE_FAILED_RULE, "severity": "warning", "message": _PARSE_FAILED_MESSAGE})
        return merged

    def _llm_review(self, code: str) -> tuple[list[dict[str, Any]], bool]:
        """调用 LLM 评审;返回 (findings, 是否解析失败降级)。"""
        system = render_prompt("validation_reviewer.md", CODE=code)
        raw = self.llm.complete(system, "请按清单评审以上策略代码,只输出 JSON。")
        findings = _extract_llm_json(raw)
        if findings is None:
            return [], True
        return findings, False

    @staticmethod
    def _dedupe(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """按 (rule, 归一化 message) 去重;先出现者优先(规则结论优先)。"""
        seen: set[tuple[str, str]] = set()
        out: list[dict[str, Any]] = []
        for f in findings:
            key = (f["rule"], re.sub(r"\s+", " ", f["message"]).strip())
            if key in seen:
                continue
            seen.add(key)
            out.append(f)
        return out
