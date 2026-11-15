"""API 层竞技场端点测试:/api/arena(stub runner 注入、请求体校验、bundle 预检)。

复用 tests/agents/stubs.py 的 GOOD_CODE/BAD_CODE(conftest.py 已注入路径);
stub runner 为 dict 形态(与 RealRunnerAdapter 归一化后的返回对齐)。
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from aquant.api.adapter import RealRunnerAdapter
from aquant.api.main import BUNDLE_GUIDANCE, app
from stubs import BAD_CODE, GOOD_CODE

START, END = "2024-01-02", "2024-06-28"


class StubArenaRunner:
    """dict 形态 stub runner:按源码内 `# ARENA:<n>` 标记返回固定指标(年化 = n/100)。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def run(self, code: str, start: str, end: str, **kw) -> dict:
        self.calls.append({"code": code, "start": start, "end": end, "kw": kw})
        match = re.search(r"ARENA:(\w+)", code)
        ann = int(match.group(1)) / 100 if match else 0.0
        return {
            "summary": {
                "annualized_returns": ann,
                "sharpe": 1.0 + ann,
                "max_drawdown": -0.08,
                "total_returns": ann * 1.2,
                "turnover": 1.5,
            },
            "verdict": {"passed": True, "error_count": 0, "warning_count": 0, "findings": []},
        }


def _good(marker: str) -> str:
    """合规策略(GOOD_CODE)+ ARENA 指标标记注释(注释不影响 Layer 1 校验)。"""
    return GOOD_CODE + f"\n# ARENA:{marker}\n"


@pytest.fixture()
def client():
    """注入 stub runner(免网络、免 bundle);用例结束后恢复默认依赖。"""
    app.state.runner = StubArenaRunner()
    with TestClient(app) as c:
        yield c
    app.state.runner = None


class TestArenaEndpoint:
    def test_arena_200_structure_and_ranking(self, client):
        """注入 stub runner:200 + 完整结构,按年化降序排名。"""
        resp = client.post(
            "/api/arena",
            json={
                "candidates": [
                    {"name": "甲", "code": _good("10")},
                    {"name": "乙", "code": _good("25")},
                ],
                "start": START,
                "end": END,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == {"ranking", "table", "details", "start", "end", "count", "ranked_count"}
        assert body["count"] == 2
        assert body["ranked_count"] == 2
        assert body["start"] == START and body["end"] == END
        assert [r["name"] for r in body["ranking"]] == ["乙", "甲"]
        assert body["ranking"][0]["annualized_returns"] == 0.25
        assert body["ranking"][0]["rank"] == 1
        # details:全部 ranked,含 metrics
        assert all(d["status"] == "ranked" for d in body["details"].values())
        assert body["details"]["乙"]["metrics"]["annualized_returns"] == 0.25
        # 排名表是文本
        assert "乙" in body["table"]

    def test_arena_rejected_candidate_contained(self, client):
        """被拒候选(未来函数)返回 rejected + verdict,不中断其余候选排名。"""
        resp = client.post(
            "/api/arena",
            json={
                "candidates": [
                    {"name": "未来函数", "code": BAD_CODE},
                    {"name": "合规", "code": _good("12")},
                ],
                "start": START,
                "end": END,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 3 - 1  # 仅 2 个候选
        assert body["ranked_count"] == 1
        rejected = body["details"]["未来函数"]
        assert rejected["status"] == "rejected"
        assert rejected["verdict"]["passed"] is False
        assert any(f["rule"] == "ast.shift_negative" for f in rejected["verdict"]["findings"])
        # 被拒候选从未进入 runner
        assert all(call["code"] != BAD_CODE for call in client.app.state.runner.calls)

    def test_arena_cash_forwarded_to_validation_and_runner(self, client):
        """cash 透传:runner 调用携带 cash kw(校验配置同样使用)。"""
        resp = client.post(
            "/api/arena",
            json={
                "candidates": [{"name": "甲", "code": _good("10")}],
                "start": START,
                "end": END,
                "cash": 500000,
            },
        )
        assert resp.status_code == 200
        assert client.app.state.runner.calls[0]["kw"].get("cash") == 500000


class TestArenaRequestValidation:
    def test_empty_candidates_422(self, client):
        resp = client.post(
            "/api/arena", json={"candidates": [], "start": START, "end": END}
        )
        assert resp.status_code == 422

    def test_over_limit_candidates_422(self, client):
        """候选上限 6:7 个直接 422。"""
        resp = client.post(
            "/api/arena",
            json={
                "candidates": [{"name": f"c{i}", "code": _good("10")} for i in range(7)],
                "start": START,
                "end": END,
            },
        )
        assert resp.status_code == 422

    def test_blank_name_422(self, client):
        """name 非空约束:空串 422。"""
        resp = client.post(
            "/api/arena",
            json={
                "candidates": [{"name": "", "code": _good("10")}],
                "start": START,
                "end": END,
            },
        )
        assert resp.status_code == 422

    def test_duplicate_names_400(self, client):
        resp = client.post(
            "/api/arena",
            json={
                "candidates": [
                    {"name": "重复", "code": _good("10")},
                    {"name": "重复", "code": _good("20")},
                ],
                "start": START,
                "end": END,
            },
        )
        assert resp.status_code == 400
        assert "候选名重复" in resp.json()["detail"]


class TestArenaBundlePrecheck:
    def test_bundle_missing_returns_400_with_guidance(self, monkeypatch):
        """真实 runner + bundle 缺失 → 400 + 构建指引(而不是一堆 failed 的 200)。"""
        monkeypatch.setenv("AQUANT_BUNDLE_PATH", "/tmp/no-such-arena-bundle")
        app.state.runner = RealRunnerAdapter()
        try:
            with TestClient(app) as c:
                resp = c.post(
                    "/api/arena",
                    json={
                        "candidates": [{"name": "甲", "code": GOOD_CODE}],
                        "start": START,
                        "end": END,
                    },
                )
        finally:
            app.state.runner = None
        assert resp.status_code == 400
        body = resp.json()
        assert body["detail"] == BUNDLE_GUIDANCE
        assert "bundle" in body["reason"]
