"""多轮对话会话 API 测试:创建/对话/手动编辑/回测/回退/diff 的全链路契约。

stub 注入:SessionStore 指向 tmp_path,LLM 用 DeterministicLLM,runner 用 StubRunner;
用例结束恢复 app.state,避免污染其他测试。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from aquant.agents.experience import ExperienceStore
from aquant.agents.llm import DeterministicLLM
from aquant.api.main import app
from aquant.session import SessionStore
from stubs import BAD_CODE, GOOD_CODE, StubRunner

START, END = "2024-01-02", "2024-06-28"


@pytest.fixture()
def client(tmp_path):
    app.state.sessions = SessionStore(dir=tmp_path / "sessions")
    app.state.experience = ExperienceStore(dir=tmp_path / "exp")
    app.state.llm = DeterministicLLM()
    app.state.runner = StubRunner()
    with TestClient(app) as c:
        yield c
    for key in ("sessions", "experience", "llm", "runner"):
        setattr(app.state, key, None)


def _create(client, code=None):
    payload = {"start": START, "end": END, "title": "测试会话"}
    if code is not None:
        payload["code"] = code
    resp = client.post("/api/sessions", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()["session"]


class TestSessionLifecycle:
    def test_create_get_list_delete(self, client):
        session = _create(client, GOOD_CODE)
        sid = session["id"]
        assert session["current_version"] == "v1"
        assert session["versions"][0]["code"] == GOOD_CODE

        got = client.get(f"/api/sessions/{sid}")
        assert got.status_code == 200
        assert got.json()["session"]["title"] == "测试会话"

        listing = client.get("/api/sessions")
        assert listing.status_code == 200
        assert any(s["id"] == sid for s in listing.json()["sessions"])

        deleted = client.delete(f"/api/sessions/{sid}")
        assert deleted.status_code == 200
        assert client.get(f"/api/sessions/{sid}").status_code == 404
        assert client.get("/api/sessions/missing").status_code == 404


class TestConversationTurns:
    def test_first_turn_generates_strategy_and_version(self, client):
        sid = _create(client)["id"]  # 无初始代码 → 首轮走 run_conversation
        resp = client.post(f"/api/sessions/{sid}/turns", json={
            "text": "生成一个双均线策略,仓位 80%", "backtest": True,
        })
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["ok"] is True
        assert data["version_id"] == "v1"
        assert data["metrics"] is not None

        session = data["session"]
        assert len(session["versions"]) == 1
        assert session["versions"][0]["source"] == "agent"
        turns = session["turns"]
        assert [t["role"] for t in turns] == ["user", "agent"]
        assert turns[1]["version_id"] == "v1"

    def test_second_turn_edits_existing_and_keeps_history(self, client):
        sid = _create(client, GOOD_CODE)["id"]
        resp = client.post(f"/api/sessions/{sid}/turns", json={
            "text": "把仓位改为 80%", "backtest": True,
        })
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["ok"] is True
        assert data["version_id"] == "v2"

        versions = data["session"]["versions"]
        assert len(versions) == 2
        assert versions[1]["parent"] == "v1"
        assert versions[1]["source"] == "agent"
        assert versions[1]["message"] == "把仓位改为 80%"
        assert versions[1]["code"] != GOOD_CODE


class TestManualEditRollbackDiff:
    def test_manual_edit_rollback_and_diff(self, client):
        sid = _create(client, GOOD_CODE)["id"]
        edited = GOOD_CODE.replace("0.9)", "0.6)")
        resp = client.put(f"/api/sessions/{sid}/code", json={
            "code": edited, "message": "降低仓位到 60%",
        })
        assert resp.status_code == 200, resp.text
        assert resp.json()["version"]["source"] == "user"
        assert resp.json()["session"]["current_version"] == "v2"

        versions = client.get(f"/api/sessions/{sid}/versions")
        assert versions.status_code == 200
        assert [v["id"] for v in versions.json()["versions"]] == ["v1", "v2"]

        diff = client.get(f"/api/sessions/{sid}/diff", params={"from_version": "v1", "to_version": "v2"})
        assert diff.status_code == 200, diff.text
        body = diff.json()
        assert body["stats"]["added"] >= 1
        assert body["stats"]["removed"] >= 1
        assert any(r["type"] in {"add", "remove"} for r in body["rows"])

        rolled = client.post(f"/api/sessions/{sid}/rollback", json={"version_id": "v1"})
        assert rolled.status_code == 200, rolled.text
        assert rolled.json()["session"]["current_version"] == "v1"
        # 回退不删历史,可再次对比/前进
        assert len(rolled.json()["session"]["versions"]) == 2
        detail = client.get(f"/api/sessions/{sid}/versions/v2")
        assert detail.status_code == 200
        assert detail.json()["version"]["code"] == edited

    def test_manual_edit_bad_code_422_no_version(self, client):
        sid = _create(client, GOOD_CODE)["id"]
        resp = client.put(f"/api/sessions/{sid}/code", json={"code": BAD_CODE})
        assert resp.status_code == 422, resp.text
        detail = resp.json()["detail"]
        assert detail["verdict"]["error_count"] >= 1
        assert len(client.get(f"/api/sessions/{sid}").json()["session"]["versions"]) == 1


class TestSessionBacktest:
    def test_backtest_updates_current_version_metrics(self, client):
        sid = _create(client, GOOD_CODE)["id"]
        resp = client.post(f"/api/sessions/{sid}/backtest")
        assert resp.status_code == 200, resp.text
        assert resp.json()["metrics"] is not None
        session = resp.json()["session"]
        assert session["versions"][0]["metrics"]["sharpe"] == 1.1
        assert len(session["versions"]) == 1  # 手动回测不新增版本

    def test_backtest_on_empty_session_400(self, client):
        sid = _create(client)["id"]
        resp = client.post(f"/api/sessions/{sid}/backtest")
        assert resp.status_code == 400
