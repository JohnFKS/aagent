"""Agent 平台层:子 agent + 编排器(详见 docs/design.md 第 3 节)。

- llm:LLMClient 协议与 Deterministic/OpenAI 兼容实现;
- strategy_editor / validation_agent / backtest_agent / review_agent:四个子 agent;
- orchestrator:完整编排管线(edit → validate → backtest → review → report)。

解耦约定(ADR 5):本层不 import aquant.runner,回测执行依赖 Runner Protocol,
真实 runner 由 aquant.api.adapter 注入。
"""

from aquant.agents.backtest_agent import BacktestAgent, Runner
from aquant.agents.llm import (
    DeterministicLLM,
    LLMAPIError,
    LLMClient,
    OpenAICompatLLM,
    make_llm,
)
from aquant.agents.orchestrator import run_conversation
from aquant.agents.review_agent import ReviewAgent
from aquant.agents.strategy_editor import (
    StrategyEditorAgent,
    StrategyGenerationError,
    extract_python_block,
)
from aquant.agents.validation_agent import ValidationAgent

__all__ = [
    "BacktestAgent",
    "DeterministicLLM",
    "LLMAPIError",
    "LLMClient",
    "OpenAICompatLLM",
    "ReviewAgent",
    "Runner",
    "StrategyEditorAgent",
    "StrategyGenerationError",
    "ValidationAgent",
    "extract_python_block",
    "make_llm",
    "run_conversation",
]
