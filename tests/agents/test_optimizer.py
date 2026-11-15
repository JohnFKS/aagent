"""StrategyOptimizerAgent 测试:AI 自动优化闭环(诊断-改码-对比保留-早停-自愈)。

scripted LLM + dict 形态 stub runner(与 RealRunnerAdapter 归一化一致),
覆盖:一轮采纳 / 未超越保留 baseline / 诊断规则 / 校验重试与防作弊回馈 /
早停 / 执行崩溃自愈 / LLM 全失败不抛 / 经验库记录。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from aquant.agents.experience import ExperienceStore
from aquant.agents.llm import LLMAPIError
from aquant.agents.optimizer import (
    COST_CLUE,
    DRAWDOWN_CLUE,
    NO_TRADE_CLUE,
    REJECT_NOT_BETTER,
    STABLE_CLUE,
    TURNOVER_CLUE,
    WIN_RATE_CLUE,
    _diagnose,
    StrategyOptimizerAgent,
)
from aquant.agents.strategy_editor import ANTI_CHEAT_CLAUSE
from aquant.validation.validator import validate_strategy
from stubs import ConstantLLM, FENCED_BAD_CODE, GOOD_CODE, ScriptedLLM

START, END = "2024-01-02", "2024-06-28"


def _res(sharpe: float, **extra: Any) -> dict[str, Any]:
    """dict 形态回测结果(与 RealRunnerAdapter 归一化键一致)。"""
    summary = {
        "total_returns": 0.10,
        "annualized_returns": 0.08,
        "sharpe": sharpe,
        "max_drawdown": -0.08,
        "turnover": 2.0,
    }
    summary.update(extra)
    return {
        "summary": summary,
        "portfolio": None,
        "trades": [{"side": "buy"}],
        "verdict": None,
        "unsafe": False,
    }


def _fenced(code: str, analysis: str = "## 优化分析(超参数调整):小幅提高快线周期。") -> str:
    """模拟 LLM 优化输出:先 `## 优化分析` 段落,后单个 python 代码块。"""
    return f"{analysis}\n```python\n{code}\n```"


class _ScriptedRunner:
    """按调用次序返回预设结果的 runner;记录 (code, kw) 供断言参数透传。"""

    def __init__(self, results: list[dict[str, Any]]) -> None:
        self.results = list(results)
        self.calls: list[dict[str, Any]] = []

    def run(self, code: str, start: str, end: str, **kw: Any) -> dict[str, Any]:
        self.calls.append({"code": code, "start": start, "end": end, "kw": kw})
        return self.results.pop(0)


class _FailingLLM:
    """每次调用都抛 LLMAPIError 的 FakeLLM(模拟 LLM 服务不可用)。"""

    def __init__(self, message: str = "网络超时") -> None:
        self.message = message
        self.calls = 0

    def complete(self, system: str, user: str) -> str:
        self.calls += 1
        raise LLMAPIError(f"LLM 请求失败:{self.message}")


def _agent(llm) -> StrategyOptimizerAgent:
    return StrategyOptimizerAgent(llm, validate_strategy)


class TestOptimizeLoop:
    """优化主循环:采纳 / 未超越 / 对比保留。"""

    def test_one_round_improvement_adopts_candidate(self):
        """a) baseline sharpe 0.5 → 候选 1.2:improved=True,best 更新,kept=True。"""
        runner = _ScriptedRunner([_res(0.5), _res(1.2)])
        llm = ScriptedLLM([_fenced(GOOD_CODE)])
        result = _agent(llm).optimize(GOOD_CODE, START, END, runner=runner, rounds=1)

        assert result["improved"] is True
        assert result["baseline"]["code"] == GOOD_CODE
        assert result["baseline"]["summary"]["sharpe"] == 0.5
        assert result["best"]["summary"]["sharpe"] == 1.2
        assert result["rounds"][0]["kept"] is True
        assert result["rounds"][0]["rejected"] is False
        assert result["rounds"][0]["error"] is None
        assert result["rounds"][0]["summary"]["sharpe"] == 1.2
        assert result["error"] is None
        # 首轮 = baseline 回测 + 候选回测,恰好 2 次
        assert len(runner.calls) == 2
        assert result["elapsed_seconds"] >= 0

    def test_prompt_carries_code_metrics_diagnosis_and_system_template(self):
        """prompt 工程:系统提示词用 optimizer.md,user 承载代码/指标/诊断线索。"""
        runner = _ScriptedRunner([_res(0.5), _res(1.2)])
        llm = ScriptedLLM([_fenced(GOOD_CODE)])
        _agent(llm).optimize(GOOD_CODE, START, END, runner=runner, rounds=1)

        system, user = llm.calls[0]
        # 系统提示词为硬约束模板(optimizer.md)
        assert "超参数单次调整幅度 ≤50%" in system
        assert "优化目标优先级" in system
        assert "禁止 `.shift(-n)`" in system
        # user 消息:当前代码 + 指标 + 诊断线索
        assert GOOD_CODE in user
        assert "最近一次回测指标" in user
        assert "sharpe: 0.5" in user
        assert "诊断线索" in user
        assert STABLE_CLUE in user  # 指标平稳 → 平稳微调线索

    def test_not_better_keeps_baseline(self):
        """b) 候选 sharpe 0.3 < 0.5:kept=False,best 仍 baseline。"""
        runner = _ScriptedRunner([_res(0.5), _res(0.3)])
        llm = ScriptedLLM([_fenced(GOOD_CODE)])
        result = _agent(llm).optimize(GOOD_CODE, START, END, runner=runner, rounds=1)

        assert result["improved"] is False
        assert result["best"]["code"] == GOOD_CODE
        assert result["best"]["summary"]["sharpe"] == 0.5
        entry = result["rounds"][0]
        assert entry["kept"] is False
        assert entry["rejected"] is False  # 代码有效,只是没跑赢
        assert entry["summary"]["sharpe"] == 0.3
        assert entry["error"] == REJECT_NOT_BETTER

    def test_tie_is_not_adopted_strictly_greater_required(self):
        """同分不采纳:对比保留要求新分严格大于当前最优分。"""
        runner = _ScriptedRunner([_res(0.5), _res(0.5)])
        llm = ScriptedLLM([_fenced(GOOD_CODE)])
        result = _agent(llm).optimize(GOOD_CODE, START, END, runner=runner, rounds=1)
        assert result["improved"] is False
        assert result["rounds"][0]["kept"] is False

    def test_cash_and_overrides_forwarded_to_runner(self):
        """cash / mod_overrides 透传给每一次 runner.run。"""
        runner = _ScriptedRunner([_res(0.5), _res(1.2)])
        llm = ScriptedLLM([_fenced(GOOD_CODE)])
        _agent(llm).optimize(
            GOOD_CODE, START, END, runner=runner, rounds=1,
            cash=500000, mod_overrides={"sys_simulation": {"slippage": 0.01}},
        )
        assert all(call["kw"].get("cash") == 500000 for call in runner.calls)
        assert all(
            call["kw"].get("mod_overrides") == {"sys_simulation": {"slippage": 0.01}}
            for call in runner.calls
        )


class TestDiagnose:
    """诊断规则:按指标自动生成改进线索。"""

    def test_no_trades_clue(self):
        """c) 无成交:诊断线索含"从未成交"。"""
        clues = _diagnose(None, 0, None)
        assert any("从未成交" in c for c in clues)
        clues = _diagnose({"sharpe": 1.0, "turnover": 2.0, "max_drawdown": -0.05}, 0, None)
        assert any("从未成交" in c for c in clues)

    def test_turnover_clue(self):
        clues = _diagnose({"turnover": 25.0, "max_drawdown": -0.1}, 5, None)
        assert TURNOVER_CLUE in clues

    def test_cost_ratio_clue(self):
        """佣金/初始资金 > 2% → 成本线索(cash 注入与平台默认两条路径)。"""
        clues = _diagnose({"total_commission": 30000.0, "turnover": 2.0, "max_drawdown": -0.05}, 10, None)
        assert COST_CLUE in clues  # 缺省按平台默认 100 万
        clues = _diagnose({"total_commission": 5000.0, "turnover": 2.0, "max_drawdown": -0.05}, 10, cash=200000)
        assert COST_CLUE in clues  # 2.5% > 2%

    def test_drawdown_and_win_rate_clues(self):
        clues = _diagnose({"max_drawdown": -0.4, "turnover": 2.0}, 10, None)
        assert DRAWDOWN_CLUE in clues
        clues = _diagnose({"win_rate": 0.2, "turnover": 2.0, "max_drawdown": -0.05}, 10, None)
        assert WIN_RATE_CLUE in clues
        # 无成交时不算胜率线索(规则要求"且有成交")
        clues = _diagnose({"win_rate": 0.2, "turnover": 2.0, "max_drawdown": -0.05}, 0, None)
        assert WIN_RATE_CLUE not in clues

    def test_stable_fallback_clue(self):
        """全部规则未命中 → 平稳微调兜底线索。"""
        clues = _diagnose({"turnover": 2.0, "max_drawdown": -0.05}, 5, None)
        assert clues == [STABLE_CLUE]


class TestValidationRetry:
    """校验-重试-防作弊(轮内重试 ≤2 次,共 3 次生成)。"""

    def test_retry_feedback_carries_rule_id_and_anti_cheat(self):
        llm = ScriptedLLM([FENCED_BAD_CODE, _fenced(GOOD_CODE)])
        runner = _ScriptedRunner([_res(0.5), _res(1.2)])
        result = _agent(llm).optimize(GOOD_CODE, START, END, runner=runner, rounds=1)

        assert result["improved"] is True  # 第 2 次生成通过并采纳
        assert len(llm.calls) == 2
        second_system, second_user = llm.calls[1]
        assert "ast.shift_negative" in second_user  # findings 中的 rule id
        assert ANTI_CHEAT_CLAUSE in second_user
        assert "上一轮代码未通过平台 Layer 1 校验" in second_user
        assert second_system == llm.calls[0][0]  # 系统提示词保持不变

    def test_three_invalid_outputs_round_failed(self):
        """d) 3 次生成均含 shift(-1):轮记 failed,候选从未进入回测。"""
        runner = _ScriptedRunner([_res(0.5)])
        llm = ConstantLLM(FENCED_BAD_CODE)
        result = _agent(llm).optimize(GOOD_CODE, START, END, runner=runner, rounds=1)

        assert result["improved"] is False
        assert result["best"]["summary"]["sharpe"] == 0.5  # best 仍 baseline
        entry = result["rounds"][0]
        assert entry["kept"] is False
        assert entry["rejected"] is True
        assert "ast.shift_negative" in entry["error"]
        assert len(llm.calls) == 3  # 1 次首发 + 2 次轮内重试
        assert len(runner.calls) == 1  # 候选代码从未回测

    def test_no_code_block_counts_as_invalid_output(self):
        """输出无代码块视同校验失败,进入重试预算。"""
        llm = ConstantLLM("本轮只是分析,没有代码块。")
        runner = _ScriptedRunner([_res(0.5)])
        result = _agent(llm).optimize(GOOD_CODE, START, END, runner=runner, rounds=1)
        assert result["rounds"][0]["rejected"] is True
        assert "```python" in result["rounds"][0]["error"]
        assert len(llm.calls) == 3


class TestEarlyStopAndSelfHealing:
    """早停与执行自愈。"""

    def test_early_stop_after_two_rounds_without_improvement(self):
        """e) 连续 2 轮无改进 → 提前终止,rounds 数 < rounds 参数。"""
        runner = _ScriptedRunner([_res(0.5), _res(0.3), _res(0.5), _res(0.3)])
        llm = ScriptedLLM([_fenced(GOOD_CODE), _fenced(GOOD_CODE)])
        result = _agent(llm).optimize(GOOD_CODE, START, END, runner=runner, rounds=3)

        assert len(result["rounds"]) == 2  # < 3:第 2 轮无改进后早停
        assert result["improved"] is False
        assert all(r["kept"] is False for r in result["rounds"])
        assert len(llm.calls) == 2
        assert len(runner.calls) == 4  # 每轮 2 次:baseline 复跑 + 候选

    def test_execution_error_feedback_self_heals(self):
        """f) 候选回测崩溃 → 错误回馈 LLM 重试 → 修复后采纳。"""
        runner = _ScriptedRunner([
            _res(0.5),
            {"summary": None, "portfolio": None, "trades": [], "verdict": None,
             "unsafe": False, "execution_error": "IndexError: only integers..."},
            _res(1.2),
        ])
        llm = ScriptedLLM([_fenced(GOOD_CODE), _fenced(GOOD_CODE)])
        result = _agent(llm).optimize(GOOD_CODE, START, END, runner=runner, rounds=1)

        assert result["improved"] is True
        assert result["rounds"][0]["kept"] is True
        assert result["rounds"][0]["summary"]["sharpe"] == 1.2
        # 第二次 LLM 调用的 user 消息含崩溃错误回馈
        assert "IndexError" in llm.calls[1][1]
        assert "崩溃" in llm.calls[1][1]
        assert "回测执行阶段崩溃" in llm.calls[1][1]

    def test_execution_error_budget_exhausted_round_failed(self):
        """执行自愈预算(≤1 次)耗尽仍崩 → 本轮记 failed。"""
        crash = {"summary": None, "portfolio": None, "trades": [], "verdict": None,
                 "unsafe": False, "execution_error": "RuntimeError: boom"}
        runner = _ScriptedRunner([_res(0.5), crash, crash])
        llm = ScriptedLLM([_fenced(GOOD_CODE), _fenced(GOOD_CODE)])
        result = _agent(llm).optimize(GOOD_CODE, START, END, runner=runner, rounds=1)

        entry = result["rounds"][0]
        assert entry["rejected"] is True and entry["kept"] is False
        assert "RuntimeError: boom" in entry["error"]
        assert len(llm.calls) == 2  # 1 次首发 + 1 次执行自愈重试
        assert result["improved"] is False


class TestLLMFailures:
    """LLM 服务失败:本轮记 failed 继续下一轮;全部失败 → error 汇总不抛。"""

    def test_all_rounds_llm_failure_returns_error_summary(self):
        """g) 全部轮次 LLMAPIError → improved=False,error 汇总,不抛异常。"""
        runner = _ScriptedRunner([_res(0.5), _res(0.5)])
        llm = _FailingLLM("网络超时")
        result = _agent(llm).optimize(GOOD_CODE, START, END, runner=runner, rounds=2)

        assert result["improved"] is False
        assert result["error"] is not None
        assert "网络超时" in result["error"]
        assert "全部 2 轮" in result["error"]
        assert all(r["rejected"] for r in result["rounds"])
        assert all("LLM 调用失败" in r["error"] for r in result["rounds"])
        assert len(runner.calls) == 2  # 每轮仅 baseline 回测

    def test_llm_failure_in_one_round_continues_next_round(self):
        """单轮 LLM 失败记 failed,下一轮恢复并采纳。"""
        runner = _ScriptedRunner([_res(0.5), _res(0.5), _res(1.2)])
        llm = _AlternatingLLM([None, _fenced(GOOD_CODE)])  # 第 1 轮抛错,第 2 轮恢复
        result = _agent(llm).optimize(GOOD_CODE, START, END, runner=runner, rounds=2)

        assert result["rounds"][0]["rejected"] is True
        assert "LLM 调用失败" in result["rounds"][0]["error"]
        assert result["rounds"][1]["kept"] is True
        assert result["improved"] is True
        assert result["error"] is None  # 非全部轮次失败 → 无 error 汇总


class _AlternatingLLM:
    """按脚本决定抛错或返回输出的 FakeLLM。"""

    def __init__(self, script: list[str | None]) -> None:
        self.script = list(script)
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        item = self.script.pop(0)
        if item is None:
            raise LLMAPIError("LLM 请求失败:瞬时断连")
        return item


class TestGuardsAndExperience:
    """构造约束与经验库记录。"""

    def test_llm_none_raises_value_error(self):
        """LLM=None 抛 ValueError:优化必须有 LLM。"""
        with pytest.raises(ValueError, match="优化必须有 LLM"):
            StrategyOptimizerAgent(None, validate_strategy)

    def test_rounds_below_one_raises_value_error(self):
        with pytest.raises(ValueError, match="rounds 必须 ≥ 1"):
            _agent(ScriptedLLM([])).optimize(GOOD_CODE, START, END, runner=None, rounds=0)

    def test_experience_records_final_best(self, tmp_path):
        """注入经验库 → 结束时 record 最终 best(passed=True + 指标)。"""
        store = ExperienceStore(dir=tmp_path)
        runner = _ScriptedRunner([_res(0.5), _res(1.2)])
        llm = ScriptedLLM([_fenced(GOOD_CODE)])
        _agent(llm).optimize(GOOD_CODE, START, END, runner=runner, rounds=1, experience=store)

        files = list(tmp_path.glob("*.json"))
        assert len(files) == 1
        entry = json.loads(files[0].read_text(encoding="utf-8"))
        assert entry["passed"] is True
        assert entry["code"] == GOOD_CODE.strip()  # extract_python_block 会去首尾空白
        assert entry["metrics"]["sharpe"] == 1.2
        assert "AI 自动优化" in entry["requirement"]

    def test_experience_failure_does_not_break_result(self, tmp_path):
        """经验库记录异常只 warn,不影响优化结果返回。"""

        class _BoomStore:
            def record(self, entry):
                raise OSError("disk full")

        runner = _ScriptedRunner([_res(0.5), _res(1.2)])
        llm = ScriptedLLM([_fenced(GOOD_CODE)])
        result = _agent(llm).optimize(
            GOOD_CODE, START, END, runner=runner, rounds=1,
            experience=_BoomStore(),  # type: ignore[arg-type]
        )
        assert result["improved"] is True
