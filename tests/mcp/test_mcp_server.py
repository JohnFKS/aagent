"""MCP server(薄桥)测试:不启动真实后端。

两部分:
1. tool 层 —— 用 httpx.MockTransport 替换 server 模块的 _TRANSPORT,
   覆盖 8 个 tool 的:成功转发、非 2xx → 可读错误文本(不抛异常)、
   X-MCP-Token 头存在性(AQUANT_MCP_TOKEN 单 token 优先,AQUANT_MCP_TOKENS
   多 token 时取第一个 readwrite token,都没有则不带头)。
2. 后端闸门层 —— aquant.api.token_gate 的 X-MCP-Token dependency
   (scope 规则表 + 每 token 限流的完整版):
   设置 token 后无头/错头 → 401,对头 → 通过,readonly token 调写端点 → 403,
   超出 RPM → 429;未配置任何 token → 完全开放。
"""

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

pytest.importorskip("mcp", reason="未安装官方 mcp SDK(pip install mcp),跳过 MCP server 测试")

from aquant.agents.llm import DeterministicLLM  # noqa: E402
from aquant.api import token_gate  # noqa: E402
from aquant.api.main import app  # noqa: E402
from aquant.mcp_server import server  # noqa: E402
from stubs import BAD_CODE, GOOD_CODE, StubRunner  # noqa: E402

START, END = "2024-01-02", "2024-06-28"
TEST_TOKEN = "s3cr3t-tok3n"


# ---------------------------------------------------------------------------
# MockTransport 基建
# ---------------------------------------------------------------------------
class Recorder:
    """记录 MockTransport 收到的请求,供 token 头/请求体断言。"""

    def __init__(self):
        self.requests: list[httpx.Request] = []

    def header(self, name: str) -> list[str | None]:
        return [r.headers.get(name) for r in self.requests]


def _install_routes(monkeypatch, routes: dict) -> Recorder:
    """把 server._TRANSPORT 替换为按 (method, path) 路由的 MockTransport。

    routes: {(method, path): (status_code, json_body)};未命中的返回 404。
    """
    recorder = Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.requests.append(request)
        entry = routes.get((request.method, request.url.path))
        if entry is None:
            return httpx.Response(404, json={"detail": f"no mock route: {request.url.path}"})
        status, body = entry
        return httpx.Response(status, json=body)

    monkeypatch.setattr(server, "_TRANSPORT", httpx.MockTransport(handler))
    return recorder


def _install_broken(monkeypatch, exc: Exception) -> None:
    """MockTransport 内直接抛异常,模拟连接失败/超时。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    monkeypatch.setattr(server, "_TRANSPORT", httpx.MockTransport(handler))


def _assert_json(text: str) -> dict:
    """2xx 语义下返回文本应仍是合法 JSON(LLM 可解析)。"""
    return json.loads(text)


# ---------------------------------------------------------------------------
# tool:platform_health
# ---------------------------------------------------------------------------
class TestPlatformHealth:
    def test_success_forwarding(self, monkeypatch):
        _install_routes(
            monkeypatch,
            {("GET", "/api/health"): (200, {"status": "ok", "version": "0.1.0"})},
        )
        text = server.platform_health()
        assert _assert_json(text) == {"status": "ok", "version": "0.1.0"}

    def test_non_2xx_returns_readable_text(self, monkeypatch):
        _install_routes(
            monkeypatch,
            {("GET", "/api/health"): (503, {"detail": "backend restarting"})},
        )
        text = server.platform_health()  # 不抛异常
        assert "503" in text and "backend restarting" in text


# ---------------------------------------------------------------------------
# tool:platform_validate_code
# ---------------------------------------------------------------------------
class TestPlatformValidateCode:
    def test_success_forwarding_sends_payload(self, monkeypatch):
        body = {
            "passed": True,
            "error_count": 0,
            "warning_count": 0,
            "findings": [],
            "summary": "校验通过",
        }
        recorder = _install_routes(
            monkeypatch, {("POST", "/api/validate"): (200, body)}
        )
        text = server.platform_validate_code(GOOD_CODE, start=START, end=END)
        assert _assert_json(text)["passed"] is True
        # 转发的请求体包含 code/start/end
        sent = json.loads(recorder.requests[0].content)
        assert sent == {"code": GOOD_CODE, "start": START, "end": END}

    def test_optional_dates_omitted_when_none(self, monkeypatch):
        recorder = _install_routes(
            monkeypatch,
            {("POST", "/api/validate"): (200, {"passed": True, "findings": []})},
        )
        server.platform_validate_code(GOOD_CODE)
        sent = json.loads(recorder.requests[0].content)
        assert sent == {"code": GOOD_CODE}

    def test_rejected_includes_findings_summary(self, monkeypatch):
        """200 内 passed=False(校验拦截)→ findings 摘要拼进返回文本。"""
        body = {
            "passed": False,
            "error_count": 1,
            "warning_count": 0,
            "findings": [{"rule": "ast.shift_negative", "message": "未来函数"}],
            "summary": "✗ 发现未来函数",
        }
        _install_routes(monkeypatch, {("POST", "/api/validate"): (200, body)})
        text = server.platform_validate_code(BAD_CODE)
        assert "校验未通过" in text
        assert "ast.shift_negative" in text
        # 完整 JSON 仍在文本中,LLM 可继续解析
        assert _assert_json(text.split("完整结果 JSON:\n", 1)[1])["passed"] is False

    def test_422_returns_readable_error_not_exception(self, monkeypatch):
        """pydantic 参数拦截(422,detail 为 list)→ 可读错误文本。"""
        body = {
            "detail": [
                {"loc": ["body", "code"], "msg": "String should have at least 1 character",
                 "type": "string_too_short"}
            ]
        }
        _install_routes(monkeypatch, {("POST", "/api/validate"): (422, body)})
        text = server.platform_validate_code("")
        assert "HTTP 422" in text
        assert "string_too_short" in text
        assert "建议" in text

    def test_401_returns_readable_error(self, monkeypatch):
        _install_routes(
            monkeypatch, {("POST", "/api/validate"): (401, {"detail": "token mismatch"})}
        )
        text = server.platform_validate_code(GOOD_CODE)
        assert "401" in text and "token mismatch" in text


# ---------------------------------------------------------------------------
# tool:platform_run_backtest
# ---------------------------------------------------------------------------
class TestPlatformRunBacktest:
    def test_success_forwarding(self, monkeypatch):
        body = {
            "passed": True,
            "verdict": {"passed": True, "error_count": 0},
            "metrics": {"annualized_returns": 0.12, "sharpe": 1.1},
            "trades_count": 3,
        }
        recorder = _install_routes(
            monkeypatch, {("POST", "/api/backtest"): (200, body)}
        )
        text = server.platform_run_backtest(GOOD_CODE, START, END, cash=100000.0)
        parsed = _assert_json(text)
        assert parsed["passed"] is True
        assert parsed["metrics"]["sharpe"] == 1.1
        sent = json.loads(recorder.requests[0].content)
        assert sent == {"code": GOOD_CODE, "start": START, "end": END, "cash": 100000.0}

    def test_422_rejected_structured_text(self, monkeypatch):
        """校验拦截 422 → verdict/findings 摘要 + 指引,不抛异常。"""
        body = {
            "detail": "策略校验未通过,拒绝执行回测。",
            "verdict": {
                "passed": False,
                "error_count": 1,
                "warning_count": 0,
                "findings": [{"rule": "ast.shift_negative", "message": "未来函数"}],
            },
        }
        _install_routes(monkeypatch, {("POST", "/api/backtest"): (422, body)})
        text = server.platform_run_backtest(BAD_CODE, START, END)
        assert "422" in text
        assert "ast.shift_negative" in text
        assert "platform_validate_code" in text  # 指引

    def test_400_bundle_missing_guidance(self, monkeypatch):
        body = {"detail": "回测数据 bundle 不存在,请设置 AQUANT_BUNDLE_PATH 或先构建 bundle。"}
        _install_routes(monkeypatch, {("POST", "/api/backtest"): (400, body)})
        text = server.platform_run_backtest(GOOD_CODE, START, END)
        assert "400" in text
        assert "bundle" in text
        assert "AQUANT_BUNDLE_PATH" in text

    def test_cash_omitted_when_none(self, monkeypatch):
        recorder = _install_routes(
            monkeypatch,
            {("POST", "/api/backtest"): (200, {"passed": True, "metrics": {}})},
        )
        server.platform_run_backtest(GOOD_CODE, START, END)
        sent = json.loads(recorder.requests[0].content)
        assert sent == {"code": GOOD_CODE, "start": START, "end": END}


# ---------------------------------------------------------------------------
# token 头存在性(AQUANT_MCP_TOKEN 单 token / AQUANT_MCP_TOKENS 多 token)
# ---------------------------------------------------------------------------
class TestTokenHeader:
    def test_token_attached_when_env_set(self, monkeypatch):
        routes = {
            ("GET", "/api/health"): (200, {"status": "ok"}),
            ("POST", "/api/validate"): (200, {"passed": True, "findings": []}),
            ("POST", "/api/backtest"): (200, {"passed": True, "metrics": {}}),
        }
        recorder = _install_routes(monkeypatch, routes)
        monkeypatch.setenv("AQUANT_MCP_TOKEN", TEST_TOKEN)
        server.platform_health()
        server.platform_validate_code(GOOD_CODE)
        server.platform_run_backtest(GOOD_CODE, START, END)
        assert recorder.requests
        for req in recorder.requests:
            assert req.headers["x-mcp-token"] == TEST_TOKEN

    def test_no_token_header_when_env_unset(self, monkeypatch):
        routes = {
            ("GET", "/api/health"): (200, {"status": "ok"}),
            ("POST", "/api/backtest"): (200, {"passed": True, "metrics": {}}),
        }
        recorder = _install_routes(monkeypatch, routes)
        monkeypatch.delenv("AQUANT_MCP_TOKEN", raising=False)
        monkeypatch.delenv("AQUANT_MCP_TOKENS", raising=False)
        server.platform_health()
        server.platform_run_backtest(GOOD_CODE, START, END)
        assert recorder.header("x-mcp-token") == [None, None]

    def test_multi_token_env_prefers_first_readwrite(self, monkeypatch):
        """AQUANT_MCP_TOKENS 多 token:取第一个 readwrite token 作为出站头。"""
        routes = {("POST", "/api/backtest"): (200, {"passed": True, "metrics": {}})}
        recorder = _install_routes(monkeypatch, routes)
        monkeypatch.delenv("AQUANT_MCP_TOKEN", raising=False)
        monkeypatch.setenv(
            "AQUANT_MCP_TOKENS",
            json.dumps({
                "tkn_ro": {"scope": "readonly", "name": "只读访客"},
                "tkn_rw": {"scope": "readwrite", "name": "mcp默认"},
            }),
        )
        server.platform_run_backtest(GOOD_CODE, START, END)
        assert recorder.header("x-mcp-token") == ["tkn_rw"]

    def test_single_token_takes_priority_over_multi(self, monkeypatch):
        monkeypatch.setenv("AQUANT_MCP_TOKEN", TEST_TOKEN)
        monkeypatch.setenv(
            "AQUANT_MCP_TOKENS", json.dumps({"tkn_rw": {"scope": "readwrite"}})
        )
        routes = {("GET", "/api/health"): (200, {"status": "ok"})}
        recorder = _install_routes(monkeypatch, routes)
        server.platform_health()
        assert recorder.header("x-mcp-token") == [TEST_TOKEN]

    def test_no_header_when_multi_token_env_invalid(self, monkeypatch):
        """AQUANT_MCP_TOKENS 非法 JSON → 不附加头(fail 静默,由后端闸门兜底)。"""
        recorder = _install_routes(monkeypatch, {("GET", "/api/health"): (200, {})})
        monkeypatch.delenv("AQUANT_MCP_TOKEN", raising=False)
        monkeypatch.setenv("AQUANT_MCP_TOKENS", "{not-json")
        server.platform_health()
        assert recorder.header("x-mcp-token") == [None]


# ---------------------------------------------------------------------------
# tool:platform_optimize
# ---------------------------------------------------------------------------
class TestPlatformOptimize:
    def test_success_forwarding_sends_full_payload(self, monkeypatch):
        body = {
            "improved": True,
            "best": {"code": "improved-code", "summary": "夏普 1.1 → 1.6"},
            "baseline": {"code": GOOD_CODE},
            "rounds": [{"round": 1, "improved": True}],
            "elapsed_seconds": 240.5,
        }
        recorder = _install_routes(
            monkeypatch, {("POST", "/api/optimize"): (200, body)}
        )
        text = server.platform_optimize(
            GOOD_CODE, START, END, rounds=3, cash=200000.0,
            overrides={"mod": {"slippage": 0.002}},
        )
        parsed = _assert_json(text)
        assert parsed["improved"] is True
        assert parsed["best"]["code"] == "improved-code"
        sent = json.loads(recorder.requests[0].content)
        assert sent == {
            "code": GOOD_CODE,
            "start": START,
            "end": END,
            "rounds": 3,
            "cash": 200000.0,
            "overrides": {"mod": {"slippage": 0.002}},
        }

    def test_defaults_send_rounds_2_without_cash_overrides(self, monkeypatch):
        recorder = _install_routes(
            monkeypatch,
            {("POST", "/api/optimize"): (200, {"improved": False, "best": {}})},
        )
        server.platform_optimize(GOOD_CODE, START, END)
        sent = json.loads(recorder.requests[0].content)
        assert sent == {"code": GOOD_CODE, "start": START, "end": END, "rounds": 2}

    def test_read_timeout_at_least_900s(self, monkeypatch):
        """optimize 每轮 = LLM 生成 + 两次回测(数分钟级),httpx 读超时必须 ≥900s。"""
        recorder = _install_routes(
            monkeypatch, {("POST", "/api/optimize"): (200, {"improved": False})}
        )
        server.platform_optimize(GOOD_CODE, START, END)
        timeout = recorder.requests[0].extensions.get("timeout", {})
        assert timeout.get("read", 0) >= 900

    def test_422_rejected_includes_findings_not_exception(self, monkeypatch):
        """校验拦截 422 + verdict → findings 摘要 + 指引,不抛异常。"""
        body = {
            "detail": "策略校验未通过,拒绝进入优化。",
            "verdict": {
                "passed": False,
                "error_count": 1,
                "warning_count": 0,
                "findings": [{"rule": "ast.shift_negative", "message": "未来函数"}],
            },
        }
        _install_routes(monkeypatch, {("POST", "/api/optimize"): (422, body)})
        text = server.platform_optimize(BAD_CODE, START, END)
        assert "422" in text
        assert "ast.shift_negative" in text
        assert "platform_validate_code" in text  # 指引先校验改码
        assert "建议" in text

    def test_400_bundle_guidance(self, monkeypatch):
        body = {"detail": "回测数据 bundle 不存在,请先构建。", "reason": "no bundle"}
        _install_routes(monkeypatch, {("POST", "/api/optimize"): (400, body)})
        text = server.platform_optimize(GOOD_CODE, START, END)
        assert "400" in text
        assert "bundle" in text
        assert "AQUANT_BUNDLE_PATH" in text

    def test_502_llm_fault_hint(self, monkeypatch):
        body = {"detail": "LLM 服务暂时不可用,请稍后重试。", "reason": "timeout"}
        _install_routes(monkeypatch, {("POST", "/api/optimize"): (502, body)})
        text = server.platform_optimize(GOOD_CODE, START, END)
        assert "502" in text
        assert "LLM" in text

    def test_connect_error_returns_readable_text(self, monkeypatch):
        _install_broken(monkeypatch, httpx.ConnectError("connection refused"))
        text = server.platform_optimize(GOOD_CODE, START, END)  # 不抛异常
        assert "无法连接" in text
        assert "AQUANT_API_BASE" in text


# ---------------------------------------------------------------------------
# tool:platform_verify_layered
# ---------------------------------------------------------------------------
class TestPlatformVerifyLayered:
    def test_success_forwarding_sends_payload(self, monkeypatch):
        body = {
            "status": "pass",
            "comparison": {"status": "pass", "is_metrics": {}, "oos_metrics": {}},
            "checks": {"oos_beats_random": True},
            "report": "分层验证报告",
            "random_baseline_returns": [0.01, -0.02],
            "paper_checklist": ["小资金模拟盘 2 周"],
        }
        recorder = _install_routes(monkeypatch, {("POST", "/api/verify"): (200, body)})
        text = server.platform_verify_layered(
            GOOD_CODE, START, END, is_ratio=0.8,
            overrides={"mod": {"slippage": 0.002}},
        )
        parsed = _assert_json(text)
        assert parsed["status"] == "pass"
        assert parsed["paper_checklist"] == ["小资金模拟盘 2 周"]
        sent = json.loads(recorder.requests[0].content)
        assert sent == {
            "code": GOOD_CODE,
            "start": START,
            "end": END,
            "is_ratio": 0.8,
            "overrides": {"mod": {"slippage": 0.002}},
        }

    def test_default_is_ratio_07_without_overrides(self, monkeypatch):
        recorder = _install_routes(
            monkeypatch, {("POST", "/api/verify"): (200, {"status": "pass"})}
        )
        server.platform_verify_layered(GOOD_CODE, START, END)
        sent = json.loads(recorder.requests[0].content)
        assert sent == {"code": GOOD_CODE, "start": START, "end": END, "is_ratio": 0.7}

    def test_422_rejected_includes_findings_not_exception(self, monkeypatch):
        body = {
            "detail": "策略校验未通过,拒绝执行分层验证。",
            "verdict": {
                "passed": False,
                "error_count": 1,
                "warning_count": 0,
                "findings": [{"rule": "ast.shift_negative", "message": "未来函数"}],
            },
        }
        _install_routes(monkeypatch, {("POST", "/api/verify"): (422, body)})
        text = server.platform_verify_layered(BAD_CODE, START, END)
        assert "422" in text
        assert "ast.shift_negative" in text
        assert "建议" in text

    def test_400_param_problem_guidance(self, monkeypatch):
        """区间 <40 个交易日等参数问题 → 400 + 可读指引。"""
        body = {"detail": "分层验证参数无法执行:区间不足 40 个交易日。"}
        _install_routes(monkeypatch, {("POST", "/api/verify"): (400, body)})
        text = server.platform_verify_layered(GOOD_CODE, START, END)
        assert "400" in text
        assert "40 个交易日" in text

    def test_connect_error_returns_readable_text(self, monkeypatch):
        _install_broken(monkeypatch, httpx.ConnectError("connection refused"))
        text = server.platform_verify_layered(GOOD_CODE, START, END)  # 不抛异常
        assert "无法连接" in text
        assert "AQUANT_API_BASE" in text


# ---------------------------------------------------------------------------
# tool:platform_get_kline
# ---------------------------------------------------------------------------
class TestPlatformGetKline:
    def test_success_forwarding_query_params(self, monkeypatch):
        candles = [
            {"date": "2024-01-02", "open": 10.0, "close": 10.2,
             "high": 10.5, "low": 9.9, "volume": 100000},
        ]
        recorder = _install_routes(
            monkeypatch, {("GET", "/api/kline"): (200, {"oid": "000001.XSHE",
                                                        "candles": candles})}
        )
        text = server.platform_get_kline("000001.XSHE", START, END)
        parsed = _assert_json(text)
        assert parsed["candles"][0]["close"] == 10.2
        assert dict(recorder.requests[0].url.params) == {
            "oid": "000001.XSHE",
            "start": START,
            "end": END,
        }

    def test_400_unknown_oid_readable_text(self, monkeypatch):
        """oid 不在 bundle / bundle 缺失 → 400 + 指引,不抛异常。"""
        body = {"detail": "oid 不在 bundle:000009.XSHE", "reason": "not found"}
        _install_routes(monkeypatch, {("GET", "/api/kline"): (400, body)})
        text = server.platform_get_kline("000009.XSHE", START, END)
        assert "400" in text
        assert "000009.XSHE" in text
        assert "建议" in text

    def test_400_bundle_missing_guidance(self, monkeypatch):
        body = {"detail": "回测数据 bundle 不存在,请设置 AQUANT_BUNDLE_PATH。"}
        _install_routes(monkeypatch, {("GET", "/api/kline"): (400, body)})
        text = server.platform_get_kline("000001.XSHE", START, END)
        assert "AQUANT_BUNDLE_PATH" in text

    def test_connect_error_returns_readable_text(self, monkeypatch):
        _install_broken(monkeypatch, httpx.ConnectError("connection refused"))
        text = server.platform_get_kline("000001.XSHE", START, END)  # 不抛异常
        assert "无法连接" in text
        assert "AQUANT_API_BASE" in text


# ---------------------------------------------------------------------------
# tool:platform_arena
# ---------------------------------------------------------------------------
class TestPlatformArena:
    def test_success_forwarding_candidates(self, monkeypatch):
        body = {
            "ranking": [{"name": "甲", "annualized_returns": 0.2, "sharpe": 1.5}],
            "table": "名次 | 候选 | 年化",
            "details": [{"name": "甲", "status": "ok"}],
        }
        candidates = [
            {"name": "甲", "code": GOOD_CODE},
            {"name": "乙", "code": BAD_CODE},
        ]
        recorder = _install_routes(monkeypatch, {("POST", "/api/arena"): (200, body)})
        text = server.platform_arena(candidates, START, END)
        parsed = _assert_json(text)
        assert parsed["ranking"][0]["name"] == "甲"
        sent = json.loads(recorder.requests[0].content)
        assert sent == {"candidates": candidates, "start": START, "end": END}

    def test_422_invalid_candidates_readable_text(self, monkeypatch):
        """pydantic 参数拦截(候选数量/字段不合法,422)→ 可读文本不抛异常。"""
        body = {
            "detail": [
                {"loc": ["body", "candidates"], "msg": "List should have at most 6 items",
                 "type": "too_long"}
            ]
        }
        _install_routes(monkeypatch, {("POST", "/api/arena"): (422, body)})
        text = server.platform_arena(
            [{"name": f"c{i}", "code": GOOD_CODE} for i in range(7)], START, END
        )
        assert "422" in text
        assert "too_long" in text
        assert "建议" in text

    def test_400_duplicate_name_readable_text(self, monkeypatch):
        """候选名重复 → 400 + 中文 detail(指引提示候选名唯一)。"""
        body = {"detail": "候选名重复:同场竞技的候选名必须唯一。"}
        _install_routes(monkeypatch, {("POST", "/api/arena"): (400, body)})
        text = server.platform_arena(
            [{"name": "同", "code": GOOD_CODE}, {"name": "同", "code": BAD_CODE}],
            START, END,
        )
        assert "400" in text
        assert "候选名重复" in text

    def test_connect_error_returns_readable_text(self, monkeypatch):
        _install_broken(monkeypatch, httpx.ConnectError("connection refused"))
        text = server.platform_arena(
            [{"name": "甲", "code": GOOD_CODE}], START, END
        )  # 不抛异常
        assert "无法连接" in text
        assert "AQUANT_API_BASE" in text


# ---------------------------------------------------------------------------
# tool:platform_workflow_guide(静态文本,不转发后端)
# ---------------------------------------------------------------------------
class TestPlatformWorkflowGuide:
    def test_contains_seven_steps_and_validate_entry(self):
        text = server.platform_workflow_guide()
        assert "七步" in text
        assert "platform_validate_code" in text
        assert "platform_run_backtest" in text
        assert "platform_optimize" in text
        assert "platform_verify_layered" in text
        assert "platform_arena" in text
        assert "platform_get_kline" in text

    def test_contains_template_and_api_constraints(self):
        text = server.platform_workflow_guide()
        assert "history_bars" in text and "ndarray" in text
        assert "POSITION_DIRECTION" in text
        assert "strategies/" in text and "reports/" in text

    def test_is_plain_text_no_network(self, monkeypatch):
        """不转发后端:未配置 transport 也直接返回文本。"""
        monkeypatch.setattr(server, "_TRANSPORT", None)
        text = server.platform_workflow_guide()
        assert text.startswith("AQ-Platform")
        assert "防过拟合" in text


# ---------------------------------------------------------------------------
# 连接失败 → 可读文本(不抛异常)
# ---------------------------------------------------------------------------
class TestConnectionFailure:
    def test_connect_error_returns_readable_text(self, monkeypatch):
        _install_broken(monkeypatch, httpx.ConnectError("connection refused"))
        for call in (
            server.platform_health,
            lambda: server.platform_validate_code(GOOD_CODE),
            lambda: server.platform_run_backtest(GOOD_CODE, START, END),
        ):
            text = call()  # 不抛异常
            assert "无法连接" in text
            assert "AQUANT_API_BASE" in text


# ---------------------------------------------------------------------------
# MCP 工具注册(mcp SDK 可用性已在模块顶部 importorskip)
# ---------------------------------------------------------------------------
class TestToolRegistration:
    ALL_TOOLS = {
        "platform_health",
        "platform_validate_code",
        "platform_run_backtest",
        "platform_optimize",
        "platform_verify_layered",
        "platform_get_kline",
        "platform_arena",
        "platform_workflow_guide",
        "platform_session_create",
        "platform_session_list",
        "platform_session_get",
        "platform_session_turn",
        "platform_session_edit_code",
        "platform_session_backtest",
        "platform_session_rollback",
        "platform_session_versions",
        "platform_session_diff",
    }

    def test_eight_tools_registered_with_description(self):
        tools = asyncio.run(server.mcp.list_tools())
        names = {t.name for t in tools}
        assert names == self.ALL_TOOLS
        for tool in tools:
            assert tool.description  # 每个 tool 都有中文描述

    def test_tools_list_count_is_17(self):
        """tools/list 数量 = 17(8 个既有工具 + 9 个多轮会话工具)。"""
        tools = asyncio.run(server.mcp.list_tools())
        assert len(tools) == 17

    def test_long_running_tools_have_time_expectations_in_description(self):
        """optimize/verify/arena 的 description 必须写明耗时预期,供外部 agent 决策。"""
        tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
        assert "耗时" in tools["platform_optimize"].description
        assert "900s" in tools["platform_optimize"].description
        assert "耗时" in tools["platform_verify_layered"].description
        assert "耗时" in tools["platform_arena"].description


# ---------------------------------------------------------------------------
# 后端 X-MCP-Token 闸门(TestClient + monkeypatch.setenv;闸门在 token_gate)
# ---------------------------------------------------------------------------
class TestApiTokenGate:
    @pytest.fixture()
    def client(self):
        app.state.llm = DeterministicLLM()
        app.state.runner = StubRunner()
        with TestClient(app) as c:
            yield c
        app.state.llm = None
        app.state.runner = None

    @pytest.fixture(autouse=True)
    def _clear_rate_buckets(self):
        token_gate._reset_rate_limits()
        yield
        token_gate._reset_rate_limits()

    def test_open_when_token_unset(self, client, monkeypatch):
        """未设置任何 token 环境变量 → 完全开放(本机单用户模式)。"""
        monkeypatch.delenv("AQUANT_MCP_TOKEN", raising=False)
        monkeypatch.delenv("AQUANT_MCP_TOKENS", raising=False)
        resp = client.post("/api/validate", json={"code": GOOD_CODE})
        assert resp.status_code == 200
        assert resp.json()["passed"] is True

    def test_missing_header_gets_401(self, client, monkeypatch):
        """设置 token 后,受保护端点无头请求 → 401(统一中文文案);/api/health 不受闸门限制。"""
        monkeypatch.setenv("AQUANT_MCP_TOKEN", TEST_TOKEN)
        for path, payload in (
            ("/api/validate", {"code": GOOD_CODE}),
            ("/api/backtest", {"code": GOOD_CODE, "start": START, "end": END}),
            ("/api/chat", {"text": "写一个双均线策略", "start": START, "end": END}),
        ):
            resp = client.post(path, json=payload)
            assert resp.status_code == 401, path
            assert resp.json()["detail"] == "无效或缺失的 API Token"
        assert client.get("/api/health").status_code == 200  # 健康检查保持开放

    def test_wrong_header_gets_401(self, client, monkeypatch):
        monkeypatch.setenv("AQUANT_MCP_TOKEN", TEST_TOKEN)
        resp = client.post(
            "/api/validate", json={"code": GOOD_CODE}, headers={"X-MCP-Token": "wrong"}
        )
        assert resp.status_code == 401

    def test_correct_header_passes(self, client, monkeypatch):
        """对头请求 → 通过闸门,业务正常执行。"""
        monkeypatch.setenv("AQUANT_MCP_TOKEN", TEST_TOKEN)
        headers = {"X-MCP-Token": TEST_TOKEN}
        resp = client.post(
            "/api/validate", json={"code": GOOD_CODE, "start": START, "end": END},
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["passed"] is True
        resp = client.post(
            "/api/chat", json={"text": "写一个双均线策略", "start": START, "end": END},
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_readonly_token_scope_gate(self, client, monkeypatch):
        """多 token 配置:readonly token 可 validate,调写端点(backtest)→ 403。"""
        monkeypatch.setenv(
            "AQUANT_MCP_TOKENS",
            json.dumps({"tkn_ro": {"scope": "readonly"}, TEST_TOKEN: {"scope": "readwrite"}}),
        )
        resp = client.post(
            "/api/validate", json={"code": GOOD_CODE}, headers={"X-MCP-Token": "tkn_ro"}
        )
        assert resp.status_code == 200
        resp = client.post(
            "/api/backtest", json={"code": GOOD_CODE, "start": START, "end": END},
            headers={"X-MCP-Token": "tkn_ro"},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Token 权限不足(scope=readonly 无法访问该端点)"
        # readwrite token 不受限
        resp = client.post(
            "/api/validate", json={"code": GOOD_CODE}, headers={"X-MCP-Token": TEST_TOKEN}
        )
        assert resp.status_code == 200

    def test_rate_limit_429(self, client, monkeypatch):
        """token rpm=2:第 3 次请求 → 429 + 中文 body(滑动窗口按 token 计数)。"""
        monkeypatch.setenv(
            "AQUANT_MCP_TOKENS", json.dumps({"tkn_rl": {"scope": "readonly", "rpm": 2}})
        )
        headers = {"X-MCP-Token": "tkn_rl"}
        for _ in range(2):
            assert client.post(
                "/api/validate", json={"code": GOOD_CODE}, headers=headers
            ).status_code == 200
        resp = client.post("/api/validate", json={"code": GOOD_CODE}, headers=headers)
        assert resp.status_code == 429
        assert resp.json()["detail"] == "请求过于频繁(RPM=2),请稍后再试"
