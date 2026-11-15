"""Agent 平台层测试:LLM 兜底实现、校验-重试-防作弊、四子 agent 与编排器全链路。"""

import json

import httpx
import pytest

from aquant.agents.backtest_agent import BacktestAgent
from aquant.agents.llm import (
    ENV_API_KEY,
    ENV_BASE_URL,
    ENV_MODEL,
    DeterministicLLM,
    LLMAPIError,
    OpenAICompatLLM,
    make_llm,
)
from aquant.agents.orchestrator import run_conversation
from aquant.agents.review_agent import PARSE_FAILED_RULE, ReviewAgent
from aquant.agents.strategy_editor import (
    ANTI_CHEAT_CLAUSE,
    StrategyEditorAgent,
    StrategyGenerationError,
    extract_python_block,
)
from aquant.agents.validation_agent import ValidationAgent
from aquant.config.schema import build_run_config
from aquant.validation.findings import Severity
from aquant.validation.validator import validate_strategy
from stubs import BAD_CODE, FENCED_BAD_CODE, GOOD_CODE, ConstantLLM, ScriptedLLM, StubRunner

START, END = "2024-01-02", "2024-06-28"


class TestDeterministicLLM:
    """DeterministicLLM:模板合规、参数替换、评审角色分派。"""

    def test_template_passes_code_validation(self):
        code = DeterministicLLM().complete("系统提示词", "帮我写一个双均线策略")
        verdict = validate_strategy(code)
        assert not verdict.has_error

    def test_template_passes_full_validation_with_run_config(self):
        code = DeterministicLLM().complete("系统提示词", "帮我写一个双均线策略")
        verdict = validate_strategy(code, build_run_config(START, END))
        assert not verdict.has_error
        # Layer 3 注入说明(INFO)随配置校验一并产出
        assert any(f.severity is Severity.INFO for f in verdict.findings)

    def test_position_percent_keyword_substitution(self):
        llm = DeterministicLLM()
        assert "context.target_percent = 0.6" in llm.complete("s", "目标仓位 60% 的双均线策略")
        # 越界比例被钳制到保守上限 0.95
        assert "context.target_percent = 0.95" in llm.complete("s", "满仓,200% 杠杆")

    def test_ignores_user_wording_without_percent(self):
        llm = DeterministicLLM()
        assert llm.complete("s", "写个双均线") == llm.complete("s", "请帮我生成一个均线金叉死叉策略,谢谢")

    def test_review_role_dispatch_returns_json(self):
        from aquant.agents.prompts import render_prompt

        system = render_prompt("validation_reviewer.md", CODE=GOOD_CODE)
        raw = DeterministicLLM().complete(system, "评审")
        assert '"findings"' in raw


class TestExtractPythonBlock:
    def test_extracts_fenced_block(self):
        code = extract_python_block("前言\n```python\nprint(1)\n```\n后记")
        assert code == "print(1)"

    def test_returns_none_for_plain_text(self):
        assert extract_python_block("抱歉,我说的是需求背景,没有代码。") is None


class TestOpenAICompatLLM:
    """OpenAI 兼容客户端:请求结构、成功解析与错误包装(monkeypatch httpx.post,免网络)。"""

    @staticmethod
    def _patch(monkeypatch, status_code=200, payload=None, exc=None):
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured.update({"url": url, "json": json, "headers": headers, "timeout": timeout})
            if exc is not None:
                raise exc
            request = httpx.Request("POST", url)
            return httpx.Response(status_code, request=request, json=payload or {})

        monkeypatch.setattr(httpx, "post", fake_post)
        return captured

    def test_success_returns_content(self, monkeypatch):
        captured = self._patch(
            monkeypatch, payload={"choices": [{"message": {"content": "策略代码"}}]}
        )
        llm = OpenAICompatLLM("http://llm.example/v1/", "secret", "glm-4")
        assert llm.complete("system", "user") == "策略代码"
        assert captured["url"] == "http://llm.example/v1/chat/completions"
        assert captured["json"]["model"] == "glm-4"
        assert captured["json"]["messages"] == [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "user"},
        ]
        assert captured["headers"]["Authorization"] == "Bearer secret"
        assert captured["timeout"] == 120.0  # 默认超时已提升(dots3 深度思考实测 >60s)

    def test_http_error_status_raises_llmapi_error(self, monkeypatch):
        self._patch(monkeypatch, status_code=503, payload={"error": "overloaded"})
        llm = OpenAICompatLLM("http://llm.example", "k", "m")
        with pytest.raises(LLMAPIError) as excinfo:
            llm.complete("s", "u")
        assert "503" in str(excinfo.value)

    def test_network_error_raises_llmapi_error(self, monkeypatch):
        self._patch(monkeypatch, exc=httpx.ConnectError("connection refused"))
        llm = OpenAICompatLLM("http://llm.example", "k", "m")
        with pytest.raises(LLMAPIError) as excinfo:
            llm.complete("s", "u")
        assert "connection refused" in str(excinfo.value)

    def test_bad_payload_raises_llmapi_error(self, monkeypatch):
        self._patch(monkeypatch, payload={"unexpected": True})
        llm = OpenAICompatLLM("http://llm.example", "k", "m")
        with pytest.raises(LLMAPIError):
            llm.complete("s", "u")


class TestMakeLLM:
    """环境变量驱动的 LLM 构造。"""

    def _clear_env(self, monkeypatch):
        for name in (ENV_BASE_URL, ENV_API_KEY, ENV_MODEL):
            monkeypatch.delenv(name, raising=False)

    def test_no_env_falls_back_to_deterministic(self, monkeypatch):
        self._clear_env(monkeypatch)
        assert isinstance(make_llm(), DeterministicLLM)

    def test_full_env_builds_openai_compat(self, monkeypatch):
        self._clear_env(monkeypatch)
        monkeypatch.setenv(ENV_BASE_URL, "http://llm.example/v1")
        monkeypatch.setenv(ENV_API_KEY, "k")
        monkeypatch.setenv(ENV_MODEL, "m")
        llm = make_llm()
        assert isinstance(llm, OpenAICompatLLM)
        assert llm.base_url == "http://llm.example/v1"

    def test_partial_env_raises_value_error(self, monkeypatch):
        self._clear_env(monkeypatch)
        monkeypatch.setenv(ENV_BASE_URL, "http://llm.example/v1")
        with pytest.raises(ValueError) as excinfo:
            make_llm()
        assert "配置不完整" in str(excinfo.value)


class TestStrategyEditorAgent:
    """校验-重试-防作弊三件套。"""

    def test_retry_then_success_with_anti_cheat_prompt(self):
        llm = ScriptedLLM([FENCED_BAD_CODE, GOOD_CODE])
        editor = StrategyEditorAgent(llm, validate_strategy)
        code = editor.generate("写一个双均线策略")

        assert "shift" not in code
        assert editor.attempts == 2
        assert len(llm.calls) == 2

        # 第二次调用的 user 消息注入了 findings 文本与防作弊声明
        second_system, second_user = llm.calls[1]
        assert "ast.shift_negative" in second_user
        assert ANTI_CHEAT_CLAUSE in second_user
        assert "不得删除或绕过校验逻辑" in second_user
        assert "上一轮代码未通过平台 Layer 1 校验" in second_user
        # 系统提示词(硬约束模板)保持不变
        assert second_system == llm.calls[0][0]
        assert "禁止" in second_system

    def test_fails_after_three_attempts(self):
        llm = ConstantLLM(FENCED_BAD_CODE)
        with pytest.raises(StrategyGenerationError) as excinfo:
            StrategyEditorAgent(llm, validate_strategy).generate("写一个双均线策略")
        assert excinfo.value.attempts == 3
        assert excinfo.value.verdict.has_error
        assert len(llm.calls) == 3

    def test_no_code_block_counts_as_failed_attempt(self):
        llm = ConstantLLM("策略思路:先看均线,再决定买不买。")
        with pytest.raises(StrategyGenerationError) as excinfo:
            StrategyEditorAgent(llm, validate_strategy).generate("写一个双均线策略")
        assert "```python" in str(excinfo.value)


class TestValidationAgent:
    """校验 agent 与中文摘要分组。"""

    def test_validate_code_only(self):
        verdict = ValidationAgent().validate(GOOD_CODE)
        assert verdict.findings == ()

    def test_summarize_groups_errors_first(self):
        summary = ValidationAgent().summarize(validate_strategy(BAD_CODE))
        assert summary.startswith("校验未通过")
        assert "✗ [ast.shift_negative]" in summary
        # 分组顺序:error → warning → info,✗ 行紧跟结论头
        lines = summary.splitlines()
        assert lines[1].startswith("✗")

    def test_summarize_passed_with_info(self):
        agent = ValidationAgent()
        verdict = agent.validate(GOOD_CODE, build_run_config(START, END))
        summary = agent.summarize(verdict)
        assert summary.startswith("校验通过")
        assert "ℹ [cfg.injected_default]" in summary


class TestBacktestAgent:
    """硬门禁:校验不过绝不调用 runner。"""

    def test_rejects_error_code_without_running_runner(self):
        runner = StubRunner()
        outcome = BacktestAgent(runner).execute(
            BAD_CODE, START, END, run_config=build_run_config(START, END)
        )
        assert outcome["rejected"] is True
        assert outcome["result"] is None
        assert outcome["verdict"].has_error
        assert runner.calls == []  # 绝不调用 runner

    def test_runs_stub_when_validation_passes(self):
        runner = StubRunner()
        outcome = BacktestAgent(runner).execute(
            GOOD_CODE, START, END, run_config=build_run_config(START, END)
        )
        assert outcome["rejected"] is False
        assert outcome["result"]["summary"]["sharpe"] == 1.1
        assert len(runner.calls) == 1

    def test_skips_when_no_runner(self):
        outcome = BacktestAgent(None).execute(GOOD_CODE, START, END)
        assert outcome["skipped"] is True
        assert outcome["rejected"] is False


class TestReviewAgent:
    """规则 findings 与 LLM 评审 JSON 的合并/去重/降级。"""

    def _rule_findings(self):
        return validate_strategy(GOOD_CODE, build_run_config(START, END)).findings

    def test_merges_and_dedupes(self):
        rule_findings = self._rule_findings()
        first = rule_findings[0].to_dict()
        llm_payload = {
            "findings": [
                {"rule": first["rule"], "severity": "info", "message": first["message"]},  # 重复项
                {"rule": "llm.style", "severity": "warning", "message": "均线窗口建议参数化,便于调参对比。"},
            ]
        }
        llm = ScriptedLLM([json.dumps(llm_payload, ensure_ascii=False)])
        merged = ReviewAgent(llm, rule_findings).review(GOOD_CODE)

        dup = [f for f in merged if f["rule"] == first["rule"] and f["message"] == first["message"]]
        assert len(dup) == 1  # 去重后只保留一条
        assert any(f["rule"] == "llm.style" for f in merged)
        assert len(merged) == len(rule_findings) + 1

    def test_parse_failure_degrades_with_warning(self):
        merged = ReviewAgent(ConstantLLM("抱歉,我无法输出 JSON。"), self._rule_findings()).review(GOOD_CODE)
        degraded = [f for f in merged if f["rule"] == PARSE_FAILED_RULE]
        assert len(degraded) == 1
        assert degraded[0]["severity"] == "warning"
        # 规则结论不受影响
        assert any(f["rule"] == "cfg.injected_default" for f in merged)

    def test_deterministic_llm_review_not_degraded(self):
        merged = ReviewAgent(DeterministicLLM(), self._rule_findings()).review(GOOD_CODE)
        assert not any(f["rule"] == PARSE_FAILED_RULE for f in merged)
        assert any(f["rule"] == "llm.manual_review" for f in merged)


class TestOrchestrator:
    """全链路编排:成功路径与 rejected 路径。"""

    def test_full_pipeline_with_stub_runner(self):
        runner = StubRunner()
        result = run_conversation(
            "写一个双均线策略,目标仓位 80%", START, END,
            llm=DeterministicLLM(), runner=runner,
        )
        assert result["ok"] is True
        assert result["rejected"] is False
        # steps 齐全且有序
        assert [s["step"] for s in result["steps"]] == [
            "edit", "validate", "backtest", "review", "report",
        ]
        # 代码与校验结论
        assert "handle_bar" in result["code"]
        assert result["verdict"]["passed"] is True
        # 指标来自 stub runner
        assert result["metrics"]["start"] == START
        assert result["metrics"]["annualized_returns"] == 0.12
        assert result["metrics"]["max_drawdown"] == -0.08
        assert result["metrics"]["sharpe"] == 1.1
        # 评审与报告
        assert result["review"]
        assert "回测报告" in result["report"]
        assert "模拟盘引导" in result["report"]
        # runner 恰好被调用一次
        assert len(runner.calls) == 1
        assert runner.calls[0]["code"] == result["code"]

    def test_rejected_path_never_reaches_backtest(self):
        runner = StubRunner()
        result = run_conversation(
            "写一个偷看明天收盘价的策略", START, END,
            llm=ConstantLLM(FENCED_BAD_CODE), runner=runner,
        )
        assert result["rejected"] is True
        assert result["ok"] is False
        assert result["metrics"] is None
        assert result["code"] is None
        assert runner.calls == []  # 生成阶段就拦截,绝不回测
        assert result["steps"][0]["step"] == "edit"
        assert result["steps"][0]["ok"] is False
        assert result["verdict"]["passed"] is False
        assert any(f["rule"] == "ast.shift_negative" for f in result["verdict"]["findings"])

    def test_runner_error_propagates_for_api_layer(self):
        # runner 内部抛出的 StrategyValidationError 不在 agents 层吞掉:
        # 沿调用链上抛,由 api 层的异常处理器映射为 422 + verdict
        from aquant.runner.backtest import StrategyValidationError

        class RejectingRunner:
            def run(self, code, start, end, **kw):
                raise StrategyValidationError(validate_strategy(BAD_CODE))

        with pytest.raises(StrategyValidationError):
            run_conversation("写一个双均线策略", START, END,
                             llm=DeterministicLLM(), runner=RejectingRunner())
