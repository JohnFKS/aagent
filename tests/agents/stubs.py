"""tests/agents 专用 stub:可控输出的 FakeLLM 与固定指标的 StubRunner。

仅用于测试(不走网络、不走 RQAlpha);tests/api 通过其 conftest.py 复用本模块。
"""

from __future__ import annotations

from typing import Any

# 通过 Layer 1 校验的合规策略(与 DeterministicLLM 模板同构)
GOOD_CODE = '''\
def init(context):
    context.s1 = "000001.XSHE"
    context.fast = 5
    context.slow = 20

def handle_bar(context, bar_dict):
    prices = history_bars(context.s1, context.slow + 2, "1d", "close")
    if prices is None or len(prices) < context.slow:
        return
    ma_fast = prices[-context.fast:].mean()
    ma_slow = prices[-context.slow:].mean()
    from rqalpha.const import POSITION_DIRECTION

    pos = context.portfolio.get_position(context.s1, POSITION_DIRECTION.LONG)
    if ma_fast > ma_slow and pos.quantity == 0:
        order_target_percent(context.s1, 0.9)
    elif ma_fast < ma_slow and pos.quantity > 0:
        order_target_percent(context.s1, 0.0)
'''

# 含 .shift(-1) 未来函数的坏策略(首次生成用来触发校验-重试)
BAD_CODE = '''\
def init(context):
    context.s1 = "000001.XSHE"

def handle_bar(context, bar_dict):
    prices = history_bars(context.s1, 20, "1d", "close")
    tomorrow = prices.shift(-1)[0]
    if tomorrow > prices[-1]:
        order_shares(context.s1, 100)
'''

# 坏代码的围栏包裹版本(模拟 LLM 真实输出格式)
FENCED_BAD_CODE = "```python\n" + BAD_CODE + "\n```"


class StubRunner:
    """固定指标的 stub 回测执行器,签名满足 agents.backtest_agent.Runner 协议。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def run(self, code: str, start: str, end: str, **kw: Any) -> dict[str, Any]:
        """记录调用并返回固定的 summary(测试断言用)。"""
        self.calls.append({"code": code, "start": start, "end": end, "kw": kw})
        return {
            "summary": {
                "annualized_returns": 0.12,
                "max_drawdown": -0.08,
                "sharpe": 1.1,
                "turnover": 2.0,
                "total_returns": 0.15,
            }
        }


class ScriptedLLM:
    """按脚本顺序返回输出的 FakeLLM;记录 (system, user) 供断言重试 prompt。"""

    def __init__(self, outputs: list[str]) -> None:
        self.outputs = list(outputs)
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        if not self.outputs:
            raise AssertionError("LLM 被调用次数超出脚本预期(可能存在多余的重试)")
        return self.outputs.pop(0)


class ConstantLLM:
    """永远返回同一文案的 FakeLLM(用于重试耗尽 / 解析失败场景)。"""

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        return self.text
