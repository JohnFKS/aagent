"""真实回测 Runner 适配器。

职责:把 ``aquant.runner.run_backtest`` 适配为 agents 层 ``Runner`` Protocol。
延迟导入:模块加载时不 import aquant.runner,只有真正执行回测时才引入,
保证无 RQAlpha 运行环境的场景(纯校验/前端联调)也能加载 API 与 Agent 管线
(ADR 5:agents 层与回测层解耦)。
"""

from __future__ import annotations

from typing import Any

from aquant.config.schema import default_bundle_path


class RealRunnerAdapter:
    """aquant.runner.run_backtest 的 Runner Protocol 适配器。

    额外职责:把 BacktestResult 归一化为 Runner Protocol 的 dict 形态,
    summary 用 key_metrics(含 6.5.0 缺失的成本/天数推导项),
    让 agents 层无需感知 rqalpha 版本差异。

    运行时崩溃不抛异常,归一化为 ``execution_error`` 键(错误文本尾部),
    供编排器的"生成-执行自愈闭环"把错误回馈给 LLM 修正
    (FileNotFoundError 属环境问题,仍向上抛给 API 转 400 指引)。
    """

    name = "real_runner"

    def run(self, code: str, start: str, end: str, **kw: Any) -> Any:
        """延迟导入并执行真实回测(bundle 缺失时由 run_backtest 抛 FileNotFoundError)。"""
        from aquant.runner.backtest import BacktestExecutionError, key_metrics, run_backtest  # 延迟导入,保持解耦

        bundle_path = kw.pop("bundle_path", None) or default_bundle_path()
        try:
            result = run_backtest(code, start, end, bundle_path=bundle_path, **kw)
        except BacktestExecutionError as exc:
            return {
                "summary": None,
                "portfolio": None,
                "trades": [],
                "verdict": None,
                "unsafe": False,
                "execution_error": str(exc),
            }
        return {
            "summary": key_metrics(result),
            "portfolio": result.portfolio,
            "trades": result.trades,
            "verdict": result.verdict,
            "unsafe": result.unsafe,
        }
