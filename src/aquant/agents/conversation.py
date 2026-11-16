"""多轮对话式策略修正:按对话要求修改已有代码 → 校验 → 回测 → 汇总。

与 StrategyEditorAgent(从零生成)的区别:
- user 消息携带「对话历史 + 用户本轮要求 + 当前策略代码 + 最近指标」;
- LLM 输出要求先写 `## 修改说明` 再给完整代码块,analysis 随版本一并存档;
- 采纳条件是「校验通过」而非「指标更优」——用户意图优先,好坏交给回测与版本对比,
  历史版本永不删除,可在前端回退(docs/design.md 策略工坊扩展)。

复用既有闭环:extract_python_block / ANTI_CHEAT_CLAUSE / _format_feedback / 校验重试 /
orchestrator 的运行时自愈、指标提取、报告渲染与经验库记录,避免重复实现。
"""

from __future__ import annotations

import re
import warnings
from typing import Any

from aquant.agents.backtest_agent import BacktestAgent, Runner
from aquant.agents.experience import ExperienceStore, derive_tags
from aquant.agents.llm import LLMClient, make_llm
from aquant.agents.orchestrator import (
    _build_report,
    _extract_execution_error,
    _extract_metrics,
    _extract_portfolio_series,
    _record_experience,
    _terminated,
)
from aquant.agents.prompts import render_prompt
from aquant.agents.strategy_editor import (
    ANTI_CHEAT_CLAUSE,
    MAX_ATTEMPTS,
    StrategyGenerationError,
    _format_feedback,
    extract_python_block,
)
from aquant.agents.validation_agent import ValidationAgent
from aquant.config.schema import build_run_config
from aquant.validation.findings import Finding, RuleId, Severity, Verdict
from aquant.validation.validator import validate_strategy

MAX_HISTORY_TURNS = 8
_HISTORY_TEXT_LIMIT = 300
_ANALYSIS_RE = re.compile(r"##\s*修改说明\s*\n(.*?)(?=\n```|\Z)", re.DOTALL)
_METRIC_LABELS = (
    ("total_returns", "总收益"),
    ("annualized_returns", "年化收益"),
    ("sharpe", "夏普比率"),
    ("max_drawdown", "最大回撤"),
    ("win_rate", "胜率"),
    ("turnover", "换手率"),
)


def _extract_analysis(raw: str) -> str:
    """提取 `## 修改说明` 段落;缺失时返回空串(不阻塞策略落地)。"""
    match = _ANALYSIS_RE.search(raw or "")
    return match.group(1).strip() if match else ""


def _format_history(history: list[dict[str, Any]] | None) -> str:
    """最近若干轮对话摘要(每轮文本截断,防止 prompt 膨胀)。"""
    turns = list(history or [])[-MAX_HISTORY_TURNS:]
    if not turns:
        return "## 对话历史\n(无历史对话,这是第一轮。)"
    lines = ["## 对话历史(最近若干轮)"]
    for turn in turns:
        role = "用户" if turn.get("role") == "user" else "Agent"
        text = str(turn.get("text") or "").strip().replace("\n", " ")
        if len(text) > _HISTORY_TEXT_LIMIT:
            text = text[:_HISTORY_TEXT_LIMIT] + "…"
        lines.append(f"- {role}:{text}")
    return "\n".join(lines)


def _format_metrics(metrics: dict[str, Any] | None) -> str:
    if not metrics:
        return "## 最近一次回测指标\n(暂无回测指标。)"
    lines = ["## 最近一次回测指标(仅供参考,用户要求优先)"]
    for key, label in _METRIC_LABELS:
        value = metrics.get(key)
        if value is None:
            continue
        if isinstance(value, (int, float)):
            lines.append(f"- {label}:{value:.4f}")
        else:
            lines.append(f"- {label}:{value}")
    return "\n".join(lines)


class StrategyEditAgent:
    """按对话要求修改已有策略:生成 → 校验 → 带 findings 重试(最多 3 次)。"""

    def __init__(
        self,
        llm: LLMClient,
        validator: Any = validate_strategy,
        experience: ExperienceStore | None = None,
    ) -> None:
        self.llm = llm
        self.validator = validator
        self.experience = experience
        self.system = render_prompt("strategy_edit.md")
        self.attempts = 0

    def _cases_section(self, request: str) -> str:
        if self.experience is None:
            return ""
        try:
            cases = self.experience.search(derive_tags(request), top_k=3)
        except Exception as exc:  # 经验库检索失败不阻塞主流程
            warnings.warn(f"经验库检索失败(已忽略):{exc}", stacklevel=2)
            return ""
        if not cases:
            return ""
        lines = ["## 相似历史案例(仅供避坑,禁止照抄)"]
        for case in cases:
            req = str(case.get("requirement") or "").strip()
            if req:
                lines.append(f"- ({'成功' if case.get('passed') else '失败'}){req[:160]}")
        return "\n".join(lines)

    def edit(
        self,
        request: str,
        current_code: str,
        history: list[dict[str, Any]] | None = None,
        *,
        metrics: dict[str, Any] | None = None,
        runtime_error: str | None = None,
    ) -> dict[str, Any]:
        """产出通过 Layer 1 代码校验的修改版策略 + 修改说明。

        :raises StrategyGenerationError: 连续 MAX_ATTEMPTS 次仍未通过校验。
        """
        if not (current_code or "").strip():
            raise ValueError("当前策略代码不能为空,无法执行修改。")
        feedback = ""
        if runtime_error:
            tail = "\n".join(runtime_error.strip().splitlines()[-30:])[-2500:]
            feedback = (
                "## 上一次修改的策略在回测执行阶段崩溃(运行时错误)\n"
                "静态校验未覆盖该错误,请修复后重新输出完整策略:\n```\n"
                f"{tail}\n```\n"
                "常见原因:history_bars 返回 ndarray,不要套 .values/.iloc/.shift 等 DataFrame 方法。\n"
            )
        cases = self._cases_section(request)
        history_section = _format_history(history)
        metrics_section = _format_metrics(metrics)
        last_verdict: Verdict | None = None
        last_code: str | None = None
        analysis = ""

        for attempt in range(1, MAX_ATTEMPTS + 1):
            self.attempts = attempt
            user = (
                f"{history_section}\n\n"
                f"## 用户本轮要求\n{request}\n\n"
                f"{metrics_section}\n\n"
                "## 当前策略代码(修改对象)\n```python\n"
                f"{current_code.strip()}\n```\n"
            )
            if cases:
                user = f"{user}\n{cases}\n"
            if feedback:
                user = f"{user}\n{feedback}\n"
            user = (
                f"{user}\n输出要求:先写 `## 修改说明`,再输出一个完整的 ```python 代码块。\n"
            )
            raw = self.llm.complete(self.system, user)
            code = extract_python_block(raw)
            analysis = _extract_analysis(raw)
            if code is None:
                last_verdict = Verdict((Finding(
                    RuleId.AST_SYNTAX, Severity.ERROR,
                    f"第 {attempt} 次输出未包含 ```python 代码块,无法提取策略源码。",
                ),))
            else:
                last_code = code
                last_verdict = self.validator(code)
            if not last_verdict.has_error:
                return {
                    "code": code,
                    "analysis": analysis,
                    "attempts": attempt,
                    "feedback": feedback,
                }
            feedback = _format_feedback(last_verdict, attempt)
            feedback = f"{feedback}\n{ANTI_CHEAT_CLAUSE}"
        assert last_verdict is not None
        raise StrategyGenerationError(last_verdict, self.attempts, last_code)


def run_edit_turn(
    request: str,
    current_code: str,
    history: list[dict[str, Any]] | None,
    start: str,
    end: str,
    *,
    llm: LLMClient | None = None,
    runner: Runner | None = None,
    experience: ExperienceStore | None = None,
    metrics: dict[str, Any] | None = None,
    cash: float | None = None,
    backtest: bool = True,
) -> dict[str, Any]:
    """执行一轮「按需求改码 → 完整校验 → 回测 → 报告」,返回与 /api/chat 同构结果。

    backtest=False 或 runner=None 时跳过回测(返回 metrics=None),便于快速预览代码修改。
    """
    llm = llm if llm is not None else make_llm()
    validation_agent = ValidationAgent()
    steps: list[dict[str, Any]] = []
    editor = StrategyEditAgent(llm, validate_strategy, experience=experience)

    MAX_RUNTIME_RETRIES = 2
    runtime_error: str | None = None
    runtime_attempt = 0

    while True:
        try:
            outcome = editor.edit(
                request, current_code, history,
                metrics=metrics, runtime_error=runtime_error,
            )
        except StrategyGenerationError as exc:
            steps.append({
                "step": "edit", "ok": False, "attempts": exc.attempts,
                "summary": f"连续 {exc.attempts} 次修改未通过 Layer 1 校验,本轮不回测。",
            })
            _record_experience(experience, user_text=request,
                               code=getattr(exc, "last_code", None), rejected=True, metrics=None)
            return {
                "ok": False, "rejected": True, "steps": steps, "code": None,
                "analysis": "", "verdict": exc.verdict.to_dict(), "metrics": None,
                "portfolio_series": None, "report": None, "error": str(exc),
            }
        code = outcome["code"]
        steps.append({
            "step": "edit", "ok": True, "attempts": outcome["attempts"],
            "summary": f"第 {outcome['attempts']} 次修改通过 Layer 1 代码校验。",
        })

        run_config = build_run_config(start, end, cash=cash)
        verdict = validation_agent.validate(code, run_config)
        steps.append({
            "step": "validate", "passed": not verdict.has_error,
            "summary": validation_agent.summarize(verdict),
        })
        if verdict.has_error:
            return _terminated(steps, code=code, verdict=verdict,
                               error="完整校验未通过(含配置),本轮不回测。")

        if not backtest or runner is None:
            steps.append({
                "step": "backtest", "skipped": True, "rejected": False,
                "summary": "本轮跳过回测(仅修改代码;可随时手动回测)。",
            })
            result_metrics = None
            series: list[dict[str, Any]] = []
            break

        exec_kw: dict[str, Any] = {}
        if cash is not None:
            exec_kw["cash"] = cash
        execution = BacktestAgent(runner, validation_agent).execute(
            code, start, end, run_config=run_config, **exec_kw
        )
        if execution["rejected"]:
            steps.append({
                "step": "backtest", "skipped": False, "rejected": True,
                "summary": execution["reason"],
            })
            return _terminated(steps, code=code, verdict=execution["verdict"],
                               error=execution["reason"])
        result_obj = execution["result"]
        exec_error = _extract_execution_error(result_obj)
        if exec_error and runtime_attempt < MAX_RUNTIME_RETRIES:
            runtime_attempt += 1
            steps = [s for s in steps if s["step"] not in ("edit", "validate")]
            steps.append({
                "step": "backtest", "skipped": False, "rejected": False, "ok": False,
                "summary": (f"第 {runtime_attempt} 次执行崩溃:"
                            f"{exec_error.strip().splitlines()[-1][:120]} —— 错误已回馈编辑器自愈。"),
            })
            runtime_error = exec_error
            continue
        if exec_error:
            steps.append({
                "step": "backtest", "skipped": False, "rejected": True,
                "summary": f"运行时错误自愈 {MAX_RUNTIME_RETRIES} 次后仍失败。",
            })
            _record_experience(experience, user_text=request, code=code,
                               rejected=True, metrics=None)
            return _terminated(steps, code=code, verdict=verdict,
                               error=f"回测运行时错误(自愈 {MAX_RUNTIME_RETRIES} 次后仍失败):{exec_error}")
        result_metrics = _extract_metrics(result_obj, start, end)
        series = _extract_portfolio_series(result_obj)
        steps.append({
            "step": "backtest", "skipped": False, "rejected": False,
            "summary": "回测执行完成,指标已汇总。",
        })
        break

    report = _build_report(code, verdict, result_metrics, [], validation_agent)
    steps.append({"step": "report", "ok": True, "summary": "报告已生成。"})
    _record_experience(experience, user_text=request, code=code,
                       rejected=False, metrics=result_metrics)

    return {
        "ok": True,
        "rejected": False,
        "steps": steps,
        "code": code,
        "analysis": outcome["analysis"],
        "verdict": verdict.to_dict(),
        "metrics": result_metrics,
        "portfolio_series": series,
        "report": report,
        "error": None,
    }
