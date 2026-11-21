"""运行时自愈闭环测试:回测执行崩溃 → 错误回馈生成器 → 自我修正。

真实背景:LLM 生成的 RSI 策略把 history_bars 的 ndarray 当 DataFrame 用,
静态校验发现不了,回测撮合阶段崩溃——自愈闭环把错误回馈给 LLM 修正。
"""

from __future__ import annotations

from typing import Any

from aquant.agents.orchestrator import run_conversation
from aquant.agents.strategy_editor import ANTI_CHEAT_CLAUSE, extract_python_block
from aquant.api.adapter import RealRunnerAdapter

from stubs import GOOD_CODE


class _CaptureLLM:
    """记录每次调用的 user 消息,按脚本返回。"""

    def __init__(self, outputs: list[str]) -> None:
        self.outputs = list(outputs)
        self.calls: list[str] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append(user)
        return self.outputs.pop(0)


def _fenced(code: str) -> str:
    return f"```python\n{code}\n```"


class _ScriptedRunner:
    """按调用次序返回预设结果的 runner(dict 形态,与 RealRunnerAdapter 一致)。"""

    def __init__(self, results: list[dict[str, Any]]) -> None:
        self.results = list(results)
        self.calls = 0

    def run(self, code: str, start: str, end: str, **kw: Any) -> dict[str, Any]:
        self.calls += 1
        return self.results.pop(0)


def _ok_result() -> dict[str, Any]:
    return {
        "summary": {"total_returns": 0.1, "annualized_returns": 0.08,
                    "sharpe": 1.2, "max_drawdown": -0.05, "turnover": 1.5,
                    "start": "2023-01-03", "end": "2024-06-28"},
        "portfolio": None,
        "trades": [],
        "verdict": None,
        "unsafe": False,
    }


class TestEditorRuntimeFeedback:
    def test_runtime_error_appended_to_user_message(self):
        llm = _CaptureLLM([_fenced(GOOD_CODE)])
        editor = _make_editor(llm)
        editor.generate("RSI 策略", runtime_error="IndexError: only integers, slices...")
        assert "回测执行阶段崩溃" in llm.calls[0]
        assert "IndexError" in llm.calls[0]
        assert "history_bars" in llm.calls[0]  # 修复指引在案
        # 运行时反馈不是静态校验失败重试,不含防作弊条款;但含自己的修复要求
        assert ANTI_CHEAT_CLAUSE not in llm.calls[0]
        assert "请修复代码使回测能完整运行" in llm.calls[0]

    def test_no_runtime_error_no_section(self):
        llm = _CaptureLLM([_fenced(GOOD_CODE)])
        editor = _make_editor(llm)
        editor.generate("RSI 策略")
        assert "回测执行阶段崩溃" not in llm.calls[0]


def _make_editor(llm):
    from aquant.agents.strategy_editor import StrategyEditorAgent
    from aquant.validation.validator import validate_strategy

    return StrategyEditorAgent(llm, validate_strategy)


class TestOrchestratorSelfHealing:
    def test_crash_then_heal(self):
        """第一次执行崩溃 → 错误回馈 → 第二次成功,步骤条含自愈标记。"""
        # 调用序列:generate#1 → generate#2(自愈)→ review
        llm = _CaptureLLM([_fenced(GOOD_CODE), _fenced(GOOD_CODE), '{"findings": []}'])
        runner = _ScriptedRunner([
            {"summary": None, "portfolio": None, "trades": [], "verdict": None,
             "unsafe": False, "execution_error": "IndexError: only integers..."},
            _ok_result(),
        ])
        result = run_conversation("RSI 策略", "2023-01-03", "2024-06-28",
                                  llm=llm, runner=runner)
        assert result["ok"] is True
        assert runner.calls == 2
        assert len(llm.calls) == 3
        # 第二次 generate 的 user 消息含运行时错误回馈
        assert "回测执行阶段崩溃" in llm.calls[1]
        heal = [s for s in result["steps"] if s["step"] == "backtest" and s.get("ok") is False]
        assert len(heal) == 1 and "自愈" in heal[0]["summary"]
        assert result["steps"][-1]["step"] == "report"
        assert result["metrics"]["total_returns"] == 0.1
        assert result.get("portfolio_series") is not None

    def test_retry_exhausted_terminates(self):
        """自愈预算耗尽 → 如实终止 + 失败案例入库。"""
        llm = _CaptureLLM([_fenced(GOOD_CODE)] * 3)
        runner = _ScriptedRunner([
            {"summary": None, "portfolio": None, "trades": [], "verdict": None,
             "unsafe": False, "execution_error": "IndexError: boom"},
        ] * 3)
        result = run_conversation("RSI 策略", "2023-01-03", "2024-06-28",
                                  llm=llm, runner=runner)
        assert result["ok"] is False and result["rejected"] is True
        assert "自愈 2 次后仍失败" in result["error"]
        assert len(llm.calls) == 3 and runner.calls == 3
        assert "自愈 2 次后仍失败" in result["steps"][-1]["summary"]

    def test_experience_records_failed_case(self, tmp_path):
        """自愈耗尽的失败案例进入经验库(passed=False)。"""
        import json

        from aquant.agents.experience import ExperienceStore

        llm = _CaptureLLM([_fenced(GOOD_CODE)] * 3)
        runner = _ScriptedRunner([
            {"summary": None, "portfolio": None, "trades": [], "verdict": None,
             "unsafe": False, "execution_error": "IndexError: boom"},
        ] * 3)
        store = ExperienceStore(dir=tmp_path)
        run_conversation("RSI 策略", "2023-01-03", "2024-06-28",
                         llm=llm, runner=runner, experience=store)
        files = list(tmp_path.glob("*.json"))
        assert len(files) == 1
        entry = json.loads(files[0].read_text(encoding="utf-8"))
        assert entry["passed"] is False and entry["code"] is not None


class TestAdapterExecutionError:
    def test_backtest_crash_normalized_to_dict(self, monkeypatch):
        """BacktestExecutionError 不抛出,归一化为 execution_error 键。"""
        import aquant.runner.backtest as rb

        def _boom(*a, **kw):
            raise rb.BacktestExecutionError(1, "IndexError: only integers...")

        monkeypatch.setattr(rb, "run_backtest", _boom)
        out = RealRunnerAdapter().run("code", "2023-01-03", "2024-06-28")
        assert out["summary"] is None
        assert "IndexError" in out["execution_error"]
        assert out["unsafe"] is False


class TestExtractPythonBlockRegression:
    def test_extract_still_works(self):
        assert "def init" in extract_python_block(_fenced(GOOD_CODE))


class TestAdapterInstrumentUnavailable:
    def test_unavailable_instrument_readable_failure(self, monkeypatch):
        """标的不可提供(退市/无行情)→ execution_error 携带可读原因,不进回测。"""
        import aquant.data.extend as ext
        from aquant.api.adapter import RealRunnerAdapter

        def _fail(bundle, start, end, **kw):
            raise ext.InstrumentUnavailableError(
                "标的 999999.XSHE 无法提供行情数据,无法回测:Baostock 区间无日线数据。"
                "请确认代码正确且当前正常上市(已退市/长期停牌的标的无法回测)。"
            )

        monkeypatch.setattr(ext, "ensure_coverage", _fail)
        out = RealRunnerAdapter().run("code", "2023-01-03", "2024-06-28")
        assert out["summary"] is None
        assert "无法提供行情数据" in out["execution_error"]
        assert "正常上市" in out["execution_error"]
        assert out["daily_holdings"] == []

    def test_strategy_oids_extracted_and_passed(self, monkeypatch):
        """adapter 从策略代码提取 oid 传入 ensure_coverage(标自适应入参)。"""
        import aquant.data.extend as ext
        from aquant.api.adapter import RealRunnerAdapter

        seen = {}

        def _fake_coverage(bundle, start, end, **kw):
            seen["oids"] = kw.get("extra_oids")
            return None

        monkeypatch.setattr(ext, "ensure_coverage", _fake_coverage)

        import pandas as pd
        import aquant.runner.backtest as rb

        def _fake_run(*a, **kw):
            return type("R", (), {
                "summary": {"total_returns": 0.0},
                "start": "2023-01-03", "end": "2023-02-01",
                "elapsed_seconds": 0.0, "unsafe": False,
                "portfolio": pd.DataFrame({"x": [1]}),
                "trades": [], "verdict": None, "unsafe": False,
                "benchmark_portfolio": pd.DataFrame(), "run_log": [],
                "strategy_prints": [], "daily_holdings": [],
            })()

        monkeypatch.setattr(rb, "run_backtest", _fake_run)
        code = 'context.s1 = "600519.XSHG"  # 另一个 000905.XSHG 作为基准'
        RealRunnerAdapter().run(code, "2023-01-03", "2023-02-01", benchmark="000300.XSHG")
        assert {"600519.XSHG", "000905.XSHG", "000300.XSHG"} <= seen["oids"]
