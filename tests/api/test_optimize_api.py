"""API 层自动优化端点测试:/api/optimize(stub 注入、200 结构、422/400 语义)。

stub runner 为 dict 形态(与 RealRunnerAdapter 归一化对齐);stub LLM 注入
app.state;经验库指向 tmp_path 避免污染用户目录。复用 tests/agents/stubs.py
(conftest.py 已注入路径)。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from aquant.agents.experience import ExperienceStore
from aquant.agents.llm import DeterministicLLM
from aquant.api.adapter import RealRunnerAdapter
from aquant.api.main import BUNDLE_GUIDANCE, app
from stubs import BAD_CODE, GOOD_CODE

START, END = "2024-01-02", "2024-06-28"


class _ScriptedRunner:
    """按调用次序返回预设结果;记录 (code, kw) 供断言参数透传。"""

    def __init__(self, results: list[dict[str, Any]]) -> None:
        self.results = list(results)
        self.calls: list[dict[str, Any]] = []

    def run(self, code: str, start: str, end: str, **kw: Any) -> dict[str, Any]:
        self.calls.append({"code": code, "start": start, "end": end, "kw": kw})
        return self.results.pop(0)


def _res(sharpe: float) -> dict[str, Any]:
    """dict 形态回测结果(与 RealRunnerAdapter 归一化键一致)。"""
    return {
        "summary": {
            "total_returns": 0.10,
            "annualized_returns": 0.08,
            "sharpe": sharpe,
            "max_drawdown": -0.08,
            "turnover": 2.0,
        },
        "portfolio": None,
        "trades": [{"side": "buy"}],
        "verdict": {"passed": True, "error_count": 0, "warning_count": 0, "findings": []},
        "unsafe": False,
    }


@pytest.fixture()
def client(tmp_path):
    """注入 stub runner + 确定性 LLM + tmp 经验库;用例结束后恢复默认依赖。

    默认脚本:baseline 0.5 → 第 1 轮候选(DeterministicLLM 模板)1.2 被采纳,
    第 2 轮(默认 rounds=2)两次 1.2 同分不采纳 → 200 且 improved=True。
    """
    runner = _ScriptedRunner([_res(0.5), _res(1.2), _res(1.2), _res(1.2)])
    app.state.llm = DeterministicLLM()
    app.state.runner = runner
    app.state.experience = ExperienceStore(dir=tmp_path)
    with TestClient(app) as c:
        yield c
    app.state.llm = None
    app.state.runner = None
    app.state.experience = None


class TestOptimizeEndpoint:
    def test_optimize_200_structure(self, client, tmp_path):
        """注入 stub runner/LLM:200 + OptimizationResult 结构,best 优于 baseline。"""
        resp = client.post("/api/optimize", json={"code": GOOD_CODE, "start": START, "end": END})
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == {"baseline", "best", "improved", "rounds", "elapsed_seconds", "error"}
        assert body["improved"] is True
        assert body["error"] is None
        assert body["baseline"]["code"] == GOOD_CODE
        assert body["baseline"]["summary"]["sharpe"] == 0.5
        assert body["best"]["summary"]["sharpe"] == 1.2
        assert "handle_bar" in body["best"]["code"]
        # 轮次历史结构:{"round","analysis","kept","summary","rejected","error"}
        entry = body["rounds"][0]
        assert set(entry) == {"round", "analysis", "kept", "summary", "rejected", "error"}
        assert entry["round"] == 1
        assert entry["kept"] is True and entry["rejected"] is False
        assert entry["summary"]["sharpe"] == 1.2
        # 经验库最终 best 已入库(指向 tmp_path,不污染用户目录)
        files = list(tmp_path.glob("*.json"))
        assert len(files) == 1
        record = json.loads(files[0].read_text(encoding="utf-8"))
        assert record["passed"] is True
        assert record["metrics"]["sharpe"] == 1.2

    def test_rounds_and_cash_forwarded(self, client):
        """rounds/cash/overrides 透传:runner 调用携带 cash 与 mod_overrides。"""
        resp = client.post(
            "/api/optimize",
            json={
                "code": GOOD_CODE, "start": START, "end": END,
                "rounds": 1, "cash": 500000,
                "overrides": {"sys_simulation": {"slippage": 0.01}},
            },
        )
        assert resp.status_code == 200
        runner = client.app.state.runner
        assert len(runner.calls) == 2  # rounds=1:baseline + 候选
        assert all(call["kw"].get("cash") == 500000 for call in runner.calls)
        assert all(
            call["kw"].get("mod_overrides") == {"sys_simulation": {"slippage": 0.01}}
            for call in runner.calls
        )

    def test_bad_code_422_with_verdict(self, client):
        """含 ERROR 的传入代码直接 422 + verdict,不进入优化,不触碰 runner。"""
        resp = client.post("/api/optimize", json={"code": BAD_CODE, "start": START, "end": END})
        assert resp.status_code == 422
        body = resp.json()
        assert "校验未通过" in body["detail"]
        assert body["verdict"]["passed"] is False
        assert any(f["rule"] == "ast.shift_negative" for f in body["verdict"]["findings"])
        assert client.app.state.runner.calls == []  # 未进入优化循环

    def test_rounds_out_of_range_422(self, client):
        """rounds 越界(pydantic le=5)→ 422。"""
        resp = client.post(
            "/api/optimize", json={"code": GOOD_CODE, "start": START, "end": END, "rounds": 9}
        )
        assert resp.status_code == 422

    def test_rounds_zero_422(self, client):
        resp = client.post(
            "/api/optimize", json={"code": GOOD_CODE, "start": START, "end": END, "rounds": 0}
        )
        assert resp.status_code == 422

    def test_bundle_missing_returns_400_with_guidance(self, monkeypatch):
        """真实 runner + bundle 缺失 → 400 + 构建指引(而不是 500)。"""
        monkeypatch.setenv("AQUANT_BUNDLE_PATH", "/tmp/no-such-optimize-bundle")
        app.state.runner = RealRunnerAdapter()
        try:
            with TestClient(app) as c:
                resp = c.post(
                    "/api/optimize", json={"code": GOOD_CODE, "start": START, "end": END}
                )
        finally:
            app.state.runner = None
        assert resp.status_code == 400
        body = resp.json()
        assert body["detail"] == BUNDLE_GUIDANCE
        assert "bundle" in body["reason"]
