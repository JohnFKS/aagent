"""回归测试:初始资金 cash 必须从 API/编排层透传到 runner。

历史 bug:build_run_config(cash=...) 只把 cash 放进 run_config(用于校验),
但 BacktestAgent.execute 调用 runner.run 时不自动解包 run_config,导致
/api/backtest 与对话/会话回测都按默认 100 万跑,手续费与用户指定本金严重不符。
"""

from __future__ import annotations

from aquant.agents.conversation import run_edit_turn
from aquant.agents.llm import DeterministicLLM
from aquant.agents.orchestrator import run_conversation
from stubs import GOOD_CODE, StubRunner


class _ReplyLLM:
    """对话修正场景:输出带修改说明的完整策略代码块。"""

    def complete(self, system: str, user: str) -> str:
        return f"## 修改说明\n降低仓位。\n\n```python\n{GOOD_CODE}\n```"


def test_run_conversation_passes_cash_to_runner():
    runner = StubRunner()
    run_conversation(
        "生成双均线策略", "2024-01-02", "2024-06-28",
        llm=DeterministicLLM(), runner=runner, cash=20000,
    )
    assert runner.calls, "runner 应被调用"
    assert runner.calls[-1]["kw"].get("cash") == 20000


def test_run_edit_turn_passes_cash_to_runner():
    runner = StubRunner()
    run_edit_turn(
        "把仓位降低", GOOD_CODE, [], "2024-01-02", "2024-06-28",
        llm=_ReplyLLM(), runner=runner, cash=20000,
    )
    assert runner.calls, "runner 应被调用"
    assert runner.calls[-1]["kw"].get("cash") == 20000


def test_no_cash_does_not_inject_kw():
    """未指定 cash 时不附加 kw,保持与旧行为一致(用默认资金)。"""
    runner = StubRunner()
    run_conversation(
        "生成策略", "2024-01-02", "2024-06-28",
        llm=DeterministicLLM(), runner=runner,
    )
    assert runner.calls
    assert "cash" not in runner.calls[-1]["kw"]
