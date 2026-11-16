"""多轮对话式策略修正 Agent 测试:edit 重试、analysis 提取、上下文注入、跳过回测。"""

from __future__ import annotations

import pytest

from aquant.agents.conversation import StrategyEditAgent, run_edit_turn
from aquant.agents.strategy_editor import StrategyGenerationError
from aquant.validation.findings import Finding, RuleId, Severity, Verdict
from stubs import GOOD_CODE, StubRunner


class _FakeLLM:
    """按调用次序返回回复;记录 (system, user) 供断言 prompt 内容。"""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        return self.replies.pop(0)


def _reply(code: str, analysis: str = "把仓位调低以降低风险。") -> str:
    return f"## 修改说明\n{analysis}\n\n```python\n{code}\n```"


class _AlwaysOkValidator:
    def __call__(self, code: str) -> Verdict:
        return Verdict(())


class _RejectFirstValidator:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, code: str) -> Verdict:
        self.calls += 1
        if self.calls == 1:
            return Verdict((Finding(
                RuleId.AST_SYNTAX, Severity.ERROR, "模拟校验错误:必须修复",
            ),))
        return Verdict(())


class TestStrategyEditAgent:
    def test_edit_returns_code_and_analysis(self):
        llm = _FakeLLM([_reply(GOOD_CODE, "降低仓位到 80%。")])
        agent = StrategyEditAgent(llm, _AlwaysOkValidator())
        outcome = agent.edit("把仓位降到 80%", GOOD_CODE)
        assert outcome["code"].strip() == GOOD_CODE.strip()
        assert "80%" in outcome["analysis"]
        assert outcome["attempts"] == 1
        assert "strategy_edit" in llm.calls[0][0] or "修改纪律" in llm.calls[0][0]

    def test_prompt_carries_history_current_code_and_request(self):
        llm = _FakeLLM([_reply(GOOD_CODE)])
        agent = StrategyEditAgent(llm, _AlwaysOkValidator())
        agent.edit(
            "再加一个 60 日均线过滤", GOOD_CODE,
            history=[{"role": "user", "text": "第一轮需求:双均线"},
                     {"role": "agent", "text": "已生成双均线策略"}],
            metrics={"sharpe": -0.5},
        )
        user = llm.calls[0][1]
        assert "第一轮需求:双均线" in user
        assert "再加一个 60 日均线过滤" in user
        assert GOOD_CODE.strip() in user
        assert "夏普比率" in user

    def test_validation_retry_then_success(self):
        llm = _FakeLLM([_reply(GOOD_CODE), _reply(GOOD_CODE)])
        validator = _RejectFirstValidator()
        agent = StrategyEditAgent(llm, validator)
        outcome = agent.edit("改一下", GOOD_CODE)
        assert outcome["attempts"] == 2
        assert validator.calls == 2
        assert "模拟校验错误" in llm.calls[1][1]

    def test_three_failures_raise_generation_error(self):
        def reject(code: str) -> Verdict:
            return Verdict((Finding(RuleId.AST_SYNTAX, Severity.ERROR, "一直失败"),))
        llm = _FakeLLM([_reply(GOOD_CODE)] * 3)
        agent = StrategyEditAgent(llm, reject)
        with pytest.raises(StrategyGenerationError) as exc:
            agent.edit("改一下", GOOD_CODE)
        assert exc.value.attempts == 3

    def test_empty_current_code_rejected(self):
        agent = StrategyEditAgent(_FakeLLM([]), _AlwaysOkValidator())
        with pytest.raises(ValueError):
            agent.edit("生成策略", "")


class TestRunEditTurn:
    def test_skip_backtest_when_runner_absent(self):
        llm = _FakeLLM([_reply(GOOD_CODE)])
        result = run_edit_turn(
            "把仓位改成 80%", GOOD_CODE, [], "2024-01-02", "2024-06-28",
            llm=llm, runner=None, backtest=True,
        )
        assert result["ok"] is True
        assert result["metrics"] is None
        steps = {s["step"]: s for s in result["steps"]}
        assert steps["backtest"]["skipped"] is True
        assert "修改说明" not in result["analysis"]  # 只取段落正文,标题已剥离

    def test_with_stub_runner_produces_metrics(self):
        llm = _FakeLLM([_reply(GOOD_CODE)])
        runner = StubRunner()
        result = run_edit_turn(
            "改一下", GOOD_CODE, [], "2024-01-02", "2024-06-28",
            llm=llm, runner=runner,
        )
        assert result["ok"] is True
        assert result["metrics"] is not None
        assert any(s["step"] == "backtest" and not s.get("skipped") for s in result["steps"])
