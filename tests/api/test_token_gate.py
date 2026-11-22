"""token_gate(多 token + scope 规则表 + 每 token 限流)API 层测试。

覆盖 docs/design.md §1.3 tick-stock-panel scope 门禁采纳项的完整版:
- 闸门关闭(未配置任何 token)→ 完全放行且不限流(现状回归,零改动通过);
- AQUANT_MCP_TOKENS 多 token:readonly 只能 /api/validate(写端点 403)、
  readwrite 全过、无效/缺失 token 401、/api/health 永远开放;
- 每 token 滑窗限流:rpm=2 时第 3 次 429(中文 body),窗口滑出后恢复,
  按 token 分别计数;
- AQUANT_MCP_TOKEN 单 token 向后兼容(=readwrite);
- 配置优先级与容错:AQUANT_MCP_TOKENS 优先于 AQUANT_MCP_TOKEN,
  非法 JSON / 未知 scope 条目 fail-closed 或忽略;
- scope 规则表(SCOPE_ROUTES / _SCOPE_LEVEL / 工厂 fail-fast)单元断言。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from aquant.agents.llm import DeterministicLLM
from aquant.api import token_gate
from aquant.api.main import app
from aquant.api.token_gate import (
    DEFAULT_RPM,
    SCOPE_ROUTES,
    require_mcp_token,
)
from stubs import GOOD_CODE

START, END = "2024-01-02", "2024-06-28"

READONLY_TOKEN = "tkn_ro_123"
READWRITE_TOKEN = "tkn_rw_456"

TOKENS_JSON = json.dumps(
    {
        READONLY_TOKEN: {"scope": "readonly", "name": "只读访客", "rpm": 60},
        READWRITE_TOKEN: {"scope": "readwrite", "name": "mcp默认", "rpm": 60},
    },
    ensure_ascii=False,
)


class GateStubRunner:
    """dict 形态 stub runner:summary/verdict/trades 齐全,backtest/arena 通用。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def run(self, code: str, start: str, end: str, **kw) -> dict:
        self.calls.append({"code": code, "start": start, "end": end, "kw": kw})
        return {
            "summary": {
                "annualized_returns": 0.12,
                "max_drawdown": -0.08,
                "sharpe": 1.1,
                "turnover": 2.0,
                "total_returns": 0.15,
            },
            "verdict": {"passed": True, "error_count": 0, "warning_count": 0, "findings": []},
            "trades": [{"datetime": "2024-01-03", "side": "buy"}],
        }


@pytest.fixture()
def client():
    """注入确定性 LLM + stub runner;用例结束后恢复默认依赖。"""
    app.state.llm = DeterministicLLM()
    app.state.runner = GateStubRunner()
    with TestClient(app) as c:
        yield c
    app.state.llm = None
    app.state.runner = None


@pytest.fixture(autouse=True)
def _clean_gate_state(monkeypatch):
    """每个用例回到「闸门关闭」基线并清空限流窗口(测试间隔离)。"""
    monkeypatch.delenv("AQUANT_MCP_TOKENS", raising=False)
    monkeypatch.delenv("AQUANT_MCP_TOKEN", raising=False)
    token_gate._reset_rate_limits()
    yield
    token_gate._reset_rate_limits()


def _tokens(monkeypatch, table: dict) -> None:
    monkeypatch.setenv("AQUANT_MCP_TOKENS", json.dumps(table, ensure_ascii=False))


# ---------------------------------------------------------------------------
# 闸门关闭:现状回归(未配置任何 token → 全部放行)
# ---------------------------------------------------------------------------
class TestGateClosed:
    def test_all_endpoints_open_without_any_token(self, client):
        resp = client.post("/api/validate", json={"code": GOOD_CODE})
        assert resp.status_code == 200
        assert resp.json()["passed"] is True
        resp = client.post("/api/backtest", json={"code": GOOD_CODE, "start": START, "end": END})
        assert resp.status_code == 200
        assert resp.json()["trades_count"] == 1
        resp = client.post("/api/chat", json={"text": "写一个双均线策略", "start": START, "end": END})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        resp = client.post(
            "/api/arena",
            json={"candidates": [{"name": "A", "code": GOOD_CODE}], "start": START, "end": END},
        )
        assert resp.status_code == 200
        assert client.get("/api/health").status_code == 200

    def test_no_rate_limiting_when_gate_closed(self, client):
        """闸门关闭时不限流:快速连发多次 validate 全部 200。"""
        for _ in range(5):
            resp = client.post("/api/validate", json={"code": GOOD_CODE})
            assert resp.status_code == 200


# ---------------------------------------------------------------------------
# AQUANT_MCP_TOKENS 多 token:scope 两档
# ---------------------------------------------------------------------------
class TestMultiTokenScopes:
    @pytest.fixture()
    def gated(self, client, monkeypatch):
        _tokens(monkeypatch, json.loads(TOKENS_JSON))
        return client

    def test_readonly_token_can_validate(self, gated):
        resp = gated.post(
            "/api/validate", json={"code": GOOD_CODE}, headers={"X-MCP-Token": READONLY_TOKEN}
        )
        assert resp.status_code == 200
        assert resp.json()["passed"] is True

    def test_readonly_token_forbidden_on_write_endpoints(self, gated):
        """readonly 调写端点 → 403 + 固定中文文案;健康检查仍开放。"""
        for path, payload in (
            ("/api/backtest", {"code": GOOD_CODE, "start": START, "end": END}),
            ("/api/chat", {"text": "写一个双均线策略", "start": START, "end": END}),
            ("/api/arena", {"candidates": [{"name": "A", "code": GOOD_CODE}],
                             "start": START, "end": END}),
            ("/api/optimize", {"code": GOOD_CODE, "start": START, "end": END}),
        ):
            resp = gated.post(path, json=payload, headers={"X-MCP-Token": READONLY_TOKEN})
            assert resp.status_code == 403, path
            assert resp.json()["detail"] == "Token 权限不足(scope=readonly 无法访问该端点)"
        assert gated.get("/api/health").status_code == 200

    def test_readwrite_token_passes_everything(self, gated):
        headers = {"X-MCP-Token": READWRITE_TOKEN}
        resp = gated.post("/api/validate", json={"code": GOOD_CODE}, headers=headers)
        assert resp.status_code == 200
        resp = gated.post(
            "/api/backtest", json={"code": GOOD_CODE, "start": START, "end": END}, headers=headers
        )
        assert resp.status_code == 200
        assert resp.json()["metrics"]["sharpe"] == 1.1
        resp = gated.post(
            "/api/chat", json={"text": "写一个双均线策略", "start": START, "end": END},
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        resp = gated.post(
            "/api/arena",
            json={"candidates": [{"name": "A", "code": GOOD_CODE}], "start": START, "end": END},
            headers=headers,
        )
        assert resp.status_code == 200
        resp = gated.post(
            "/api/optimize", json={"code": GOOD_CODE, "start": START, "end": END}, headers=headers
        )
        assert resp.status_code == 200
        assert resp.json()["improved"] is False  # stub 指标恒定,不会严格超越基准

    def test_invalid_token_gets_401(self, gated):
        resp = gated.post(
            "/api/validate", json={"code": GOOD_CODE}, headers={"X-MCP-Token": "tkn_wrong"}
        )
        assert resp.status_code == 401
        assert resp.json()["detail"] == "无效或缺失的 API Token"

    def test_missing_token_gets_401(self, gated):
        resp = gated.post("/api/validate", json={"code": GOOD_CODE})
        assert resp.status_code == 401
        assert resp.json()["detail"] == "无效或缺失的 API Token"

    def test_health_open_even_with_gate_on(self, gated):
        """闸门开启后健康检查依旧无闸门(且不消耗任何 token 限流额度)。"""
        assert gated.get("/api/health").status_code == 200


# ---------------------------------------------------------------------------
# 每 token 滑动窗口限流
# ---------------------------------------------------------------------------
class TestRateLimit:
    def test_third_request_gets_429(self, client, monkeypatch):
        """token rpm=2:前两次 200,第 3 次 429 + 中文 body。"""
        _tokens(monkeypatch, {"tkn_rl": {"scope": "readonly", "rpm": 2}})
        headers = {"X-MCP-Token": "tkn_rl"}
        payload = {"code": GOOD_CODE}
        assert client.post("/api/validate", json=payload, headers=headers).status_code == 200
        assert client.post("/api/validate", json=payload, headers=headers).status_code == 200
        resp = client.post("/api/validate", json=payload, headers=headers)
        assert resp.status_code == 429
        assert resp.json()["detail"] == "请求过于频繁(RPM=2),请稍后再试"

    def test_rate_limit_is_per_token(self, client, monkeypatch):
        """A 把额度用尽,B(默认 RPM)不受影响:窗口按 token 分别计数。"""
        _tokens(
            monkeypatch,
            {"tkn_a": {"scope": "readonly", "rpm": 1}, "tkn_b": {"scope": "readonly"}},
        )
        payload = {"code": GOOD_CODE}
        assert client.post(
            "/api/validate", json=payload, headers={"X-MCP-Token": "tkn_a"}
        ).status_code == 200
        assert client.post(
            "/api/validate", json=payload, headers={"X-MCP-Token": "tkn_a"}
        ).status_code == 429
        resp = client.post("/api/validate", json=payload, headers={"X-MCP-Token": "tkn_b"})
        assert resp.status_code == 200

    def test_window_slides_after_60s(self, client, monkeypatch):
        """滑窗 60 秒:被限流后把假时钟推过窗口,额度恢复(旧时间戳被剪掉)。"""
        clock = {"now": 1000.0}
        fake = SimpleNamespace(monotonic=lambda: clock["now"])
        monkeypatch.setattr(token_gate, "time", fake)
        _tokens(monkeypatch, {"tkn_rl": {"scope": "readonly", "rpm": 2}})
        headers = {"X-MCP-Token": "tkn_rl"}
        payload = {"code": GOOD_CODE}
        assert client.post("/api/validate", json=payload, headers=headers).status_code == 200
        assert client.post("/api/validate", json=payload, headers=headers).status_code == 200
        assert client.post("/api/validate", json=payload, headers=headers).status_code == 429
        clock["now"] += token_gate.RATE_WINDOW_SECONDS + 1  # 窗口整体滑出
        assert client.post("/api/validate", json=payload, headers=headers).status_code == 200

    def test_default_rpm_is_60(self):
        """未声明 rpm 的 token 使用默认 60(_allow_request 直测,免发 61 个请求)。"""
        token_gate._reset_rate_limits()
        assert all(token_gate._allow_request("tkn_default", DEFAULT_RPM) for _ in range(60))
        assert token_gate._allow_request("tkn_default", DEFAULT_RPM) is False
        token_gate._reset_rate_limits()


# ---------------------------------------------------------------------------
# AQUANT_MCP_TOKEN 单 token 向后兼容(=readwrite)
# ---------------------------------------------------------------------------
class TestSingleTokenCompat:
    def test_single_token_behaves_as_readwrite(self, client, monkeypatch):
        monkeypatch.setenv("AQUANT_MCP_TOKEN", "s3cr3t-tok3n")
        headers = {"X-MCP-Token": "s3cr3t-tok3n"}
        resp = client.post("/api/validate", json={"code": GOOD_CODE}, headers=headers)
        assert resp.status_code == 200
        resp = client.post(
            "/api/backtest", json={"code": GOOD_CODE, "start": START, "end": END}, headers=headers
        )
        assert resp.status_code == 200  # 单 token 等价 readwrite,可访问写端点
        resp = client.post("/api/validate", json={"code": GOOD_CODE})
        assert resp.status_code == 401  # 缺头仍 401
        resp = client.post(
            "/api/validate", json={"code": GOOD_CODE}, headers={"X-MCP-Token": "wrong"}
        )
        assert resp.status_code == 401  # 错头仍 401

    def test_single_token_rate_limited_with_default_rpm(self, client, monkeypatch):
        """单 token 兼容模式同样限流(默认 RPM=60)。"""
        monkeypatch.setenv("AQUANT_MCP_TOKEN", "s3cr3t-tok3n")
        headers = {"X-MCP-Token": "s3cr3t-tok3n"}
        token_gate._reset_rate_limits()
        for _ in range(60):
            assert client.post(
                "/api/validate", json={"code": GOOD_CODE}, headers=headers
            ).status_code == 200
        resp = client.post("/api/validate", json={"code": GOOD_CODE}, headers=headers)
        assert resp.status_code == 429
        assert resp.json()["detail"] == "请求过于频繁(RPM=60),请稍后再试"


# ---------------------------------------------------------------------------
# 配置优先级与容错(fail-closed)
# ---------------------------------------------------------------------------
class TestConfigPriorityAndErrors:
    def test_tokens_env_takes_priority_over_single_token(self, client, monkeypatch):
        """两个变量同时设置时,AQUANT_MCP_TOKENS 是唯一权威 token 表。"""
        _tokens(monkeypatch, {"tkn_only": {"scope": "readwrite"}})
        monkeypatch.setenv("AQUANT_MCP_TOKEN", "s3cr3t-tok3n")  # 不再生效
        resp = client.post(
            "/api/validate", json={"code": GOOD_CODE}, headers={"X-MCP-Token": "tkn_only"}
        )
        assert resp.status_code == 200
        resp = client.post(
            "/api/validate", json={"code": GOOD_CODE}, headers={"X-MCP-Token": "s3cr3t-tok3n"}
        )
        assert resp.status_code == 401

    def test_invalid_json_fails_closed(self, client, monkeypatch, capsys):
        """AQUANT_MCP_TOKENS 非法 JSON → 全部 401(绝不能静默放开),stderr 告警。"""
        monkeypatch.setenv("AQUANT_MCP_TOKENS", "{not-json")
        monkeypatch.setenv("AQUANT_MCP_TOKEN", "s3cr3t-tok3n")  # 兜底也不生效
        resp = client.post(
            "/api/validate", json={"code": GOOD_CODE}, headers={"X-MCP-Token": "s3cr3t-tok3n"}
        )
        assert resp.status_code == 401
        assert "fail-closed" in capsys.readouterr().err

    def test_top_level_not_object_fails_closed(self, client, monkeypatch):
        monkeypatch.setenv("AQUANT_MCP_TOKENS", '["tkn_x"]')
        resp = client.post("/api/validate", json={"code": GOOD_CODE})
        assert resp.status_code == 401

    def test_empty_tokens_object_fails_closed(self, client, monkeypatch):
        """显式配置空表 = 闸门开但无有效 token → 401(与「未配置」区分)。"""
        monkeypatch.setenv("AQUANT_MCP_TOKENS", "{}")
        resp = client.post("/api/validate", json={"code": GOOD_CODE})
        assert resp.status_code == 401

    def test_unknown_scope_entry_ignored_valid_entry_kept(self, client, monkeypatch):
        """单条配置坏只淘汰自己(401),不拖垮整张表。"""
        _tokens(
            monkeypatch,
            {"tkn_bad": {"scope": "admin"}, "tkn_ok": {"scope": "readwrite"}},
        )
        resp = client.post(
            "/api/validate", json={"code": GOOD_CODE}, headers={"X-MCP-Token": "tkn_bad"}
        )
        assert resp.status_code == 401
        resp = client.post(
            "/api/validate", json={"code": GOOD_CODE}, headers={"X-MCP-Token": "tkn_ok"}
        )
        assert resp.status_code == 200

    def test_shorthand_scope_string_value(self, client, monkeypatch):
        """value 简写为 scope 字符串亦可:{"tkn_y": "readonly"}。"""
        _tokens(monkeypatch, {"tkn_y": "readonly"})
        resp = client.post(
            "/api/validate", json={"code": GOOD_CODE}, headers={"X-MCP-Token": "tkn_y"}
        )
        assert resp.status_code == 200
        resp = client.post(
            "/api/backtest", json={"code": GOOD_CODE, "start": START, "end": END},
            headers={"X-MCP-Token": "tkn_y"},
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# scope 规则表与工厂(单元断言,无需客户端)
# ---------------------------------------------------------------------------
class TestScopeRuleTable:
    def test_scope_routes_table_contents(self):
        # /api/kline(K 线行情,GET 只读)登记进 readonly;readwrite 恒为全量端点
        # /api/verify(分层验证)登记进 readwrite(会触发回测,非只读)
        # /api/optimize(AI 自动优化闭环)登记进 readwrite(触发 LLM 与回测,非只读)
        # /api/backtests 的 GET(列表/详情)归 readonly,POST(提交/取消)归 readwrite
        assert set(SCOPE_ROUTES["readonly"]) == {
            "/api/validate", "/api/health", "/api/kline", "/api/backtests", "/api/backtests/*",
        }
        assert set(SCOPE_ROUTES["readwrite"]) == {
            "/api/validate",
            "/api/health",
            "/api/backtest",
            "/api/backtests",
            "/api/backtests/*",
            "/api/chat",
            "/api/arena",
            "/api/kline",
            "/api/verify",
            "/api/optimize",
            "/api/sessions",
            "/api/sessions/*",
        }

    def test_scope_allows_matrix(self):
        readonly_level = token_gate._SCOPE_LEVEL["readonly"]
        readwrite_level = token_gate._SCOPE_LEVEL["readwrite"]
        # readonly token:validate 可以,写端点不行
        assert token_gate._scope_allows("readonly", readonly_level, "/api/validate") is True
        assert token_gate._scope_allows("readonly", readwrite_level, "/api/backtest") is False
        # readwrite token:全部可以
        for path in ("/api/validate", "/api/backtest", "/api/chat", "/api/arena"):
            assert token_gate._scope_allows("readwrite", readonly_level, path) is True
            assert token_gate._scope_allows("readwrite", readwrite_level, path) is True
        # 会话族动态子路径由 "/*" 前缀规则覆盖,未登记路径仍 fail-closed
        assert token_gate._scope_allows("readwrite", readwrite_level, "/api/sessions") is True
        assert token_gate._scope_allows("readwrite", readwrite_level, "/api/sessions/s_abc/turns") is True
        assert token_gate._scope_allows("readonly", readonly_level, "/api/sessions/s_abc/turns") is False
        assert token_gate._scope_allows("readwrite", readwrite_level, "/api/future") is False

    def test_factory_rejects_unknown_scope(self):
        """声明未登记的 scope → 工厂 fail-fast(启动期暴露拼写错误)。"""
        with pytest.raises(ValueError, match="未知 scope"):
            require_mcp_token(scope="admin")

    def test_load_tokens_none_when_unset(self, monkeypatch):
        monkeypatch.delenv("AQUANT_MCP_TOKENS", raising=False)
        monkeypatch.delenv("AQUANT_MCP_TOKEN", raising=False)
        assert token_gate._load_tokens() is None  # 闸门关闭
        monkeypatch.setenv("AQUANT_MCP_TOKEN", "tk")
        tokens = token_gate._load_tokens()
        assert tokens is not None and tokens["tk"].scope == "readwrite"
        assert tokens["tk"].rpm == DEFAULT_RPM
