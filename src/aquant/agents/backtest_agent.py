"""BacktestAgent:回测执行的硬门禁。

ADR 5(agents 与回测层解耦)+ ADR 4(回测子进程隔离):本 agent 只依赖
``Runner`` Protocol,不 import aquant.runner;真实 runner 由 api 层注入
(aquant.api.adapter.RealRunnerAdapter),测试注入 stub。

铁律:执行前必须先经 ValidationAgent 校验,存在 ERROR 直接拒绝并返回
``{"rejected": True, "verdict": <Verdict>, ...}``,绝不调用 runner。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from aquant.agents.validation_agent import ValidationAgent
from aquant.validation.findings import Verdict


@runtime_checkable
class Runner(Protocol):
    """回测执行器协议。

    实现方只需提供 ``run(code, start, end, **kw)``;返回结构不做强约束,
    只要能取到 summary dict(对象属性 ``.summary`` 或 dict 的 ``"summary"`` 键)。
    """

    def run(self, code: str, start: str, end: str, **kw: Any) -> Any:
        """执行一次回测。"""
        ...


class BacktestAgent:
    """回测子 agent:校验不过绝不回测(拦截语义借鉴 tick-stock-panel 的高危操作审批)。"""

    def __init__(self, runner: Runner | None, validator: ValidationAgent | None = None) -> None:
        self.runner = runner
        self.validator = validator or ValidationAgent()

    def execute(
        self,
        code: str,
        start: str,
        end: str,
        *,
        run_config: dict[str, Any] | None = None,
        **kw: Any,
    ) -> dict[str, Any]:
        """校验并(在通过时)执行回测。

        :param run_config: 调用方构造的完整 run 配置(通常来自 build_run_config),
            校验同时覆盖代码与配置两层。
        :returns: {"rejected": bool, "verdict": Verdict, "result": Runner 原始返回,
            "skipped": bool(未注入 runner 时跳过,非拒绝), "reason": str}
        """
        verdict = self.validator.validate(code, run_config)
        if verdict.has_error:
            return {
                "rejected": True,
                "verdict": verdict,
                "result": None,
                "skipped": False,
                "reason": "校验存在 ERROR,拒绝执行回测。",
            }
        if self.runner is None:
            return {
                "rejected": False,
                "verdict": verdict,
                "result": None,
                "skipped": True,
                "reason": "未注入 runner,跳过回测(测试可注入 stub,api 层注入 RealRunnerAdapter)。",
            }
        result = self.runner.run(code, start, end, **kw)
        return {"rejected": False, "verdict": verdict, "result": result, "skipped": False, "reason": ""}
