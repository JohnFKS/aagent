"""API 层测试:health / validate / chat(stub 注入、bundle 缺失 400、校验拒绝 422)。"""

import pytest
from fastapi.testclient import TestClient

from aquant import __version__
from aquant.agents.llm import DeterministicLLM
from aquant.api.adapter import RealRunnerAdapter
from aquant.api.main import BUNDLE_GUIDANCE, app
from aquant.validation.validator import validate_strategy
from stubs import BAD_CODE, GOOD_CODE, StubRunner

START, END = "2024-01-02", "2024-06-28"


@pytest.fixture()
def client():
    """注入确定性 LLM + stub runner(免网络、免 bundle);用例结束后恢复默认依赖。"""
    app.state.llm = DeterministicLLM()
    app.state.runner = StubRunner()
    with TestClient(app) as c:
        yield c
    app.state.llm = None
    app.state.runner = None


def _restore_state():
    app.state.llm = None
    app.state.runner = None


class TestHealth:
    def test_health_ok(self, client):
        resp = client.get("/api/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["version"] == __version__


class TestValidate:
    def test_good_strategy_passes(self, client):
        resp = client.post(
            "/api/validate", json={"code": GOOD_CODE, "start": START, "end": END}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["passed"] is True
        assert body["error_count"] == 0
        assert body["warning_count"] == 0
        assert body["summary"].startswith("校验通过")

    def test_shift_negative_strategy_fails(self, client):
        resp = client.post("/api/validate", json={"code": BAD_CODE})
        assert resp.status_code == 200
        body = resp.json()
        assert body["passed"] is False
        assert body["error_count"] >= 1
        assert any(f["rule"] == "ast.shift_negative" for f in body["findings"])
        assert "✗" in body["summary"]


class TestChat:
    def test_chat_with_stub_runner_returns_metrics(self, client):
        resp = client.post(
            "/api/chat", json={"text": "写一个双均线策略,仓位 80%", "start": START, "end": END}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["rejected"] is False
        # 完整编排步骤
        assert [s["step"] for s in body["steps"]] == [
            "edit", "validate", "backtest", "review", "report",
        ]
        # 校验与指标
        assert body["verdict"]["passed"] is True
        assert body["metrics"]["annualized_returns"] == 0.12
        assert body["metrics"]["sharpe"] == 1.1
        assert body["metrics"]["start"] == START
        # 报告面向用户
        assert "回测报告" in body["report"]

    def test_chat_bundle_missing_returns_400_with_guidance(self, monkeypatch, tmp_path):
        """真实 runner + bundle 缺失 → 400 + 构建指引(而不是 500)。"""
        monkeypatch.setenv("AQUANT_BUNDLE_PATH", str(tmp_path / "no-such-bundle"))
        app.state.llm = DeterministicLLM()
        app.state.runner = RealRunnerAdapter()
        try:
            with TestClient(app) as c:
                resp = c.post(
                    "/api/chat", json={"text": "写一个双均线策略", "start": START, "end": END}
                )
        finally:
            _restore_state()
        assert resp.status_code == 400
        body = resp.json()
        assert body["detail"] == BUNDLE_GUIDANCE
        assert "AQUANT_BUNDLE_PATH" in body["detail"]
        assert "bundle" in body["reason"]

    def test_chat_runner_strategy_validation_error_maps_to_422(self):
        """runner 执行期抛 StrategyValidationError → 422 + verdict dict。"""
        from aquant.runner.backtest import StrategyValidationError

        class RejectingRunner:
            def run(self, code, start, end, **kw):
                raise StrategyValidationError(validate_strategy(BAD_CODE))

        app.state.llm = DeterministicLLM()
        app.state.runner = RejectingRunner()
        try:
            with TestClient(app) as c:
                resp = c.post(
                    "/api/chat", json={"text": "写一个双均线策略", "start": START, "end": END}
                )
        finally:
            _restore_state()
        assert resp.status_code == 422
        body = resp.json()
        assert "校验未通过" in body["detail"]
        assert body["verdict"]["passed"] is False
        assert any(f["rule"] == "ast.shift_negative" for f in body["verdict"]["findings"])
