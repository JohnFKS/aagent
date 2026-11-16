"""历史对话/策略版本 CRUD 与永久删除的 API 契约测试。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from aquant.agents.experience import ExperienceStore
from aquant.agents.llm import DeterministicLLM
from aquant.api.main import app
from aquant.session import SessionStore
from stubs import GOOD_CODE, StubRunner

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


def _create(client):
    r = client.post("/api/sessions", json={"start": START, "end": END, "code": GOOD_CODE})
    assert r.status_code == 200
    return r.json()["session"]["id"]


class TestTurnCRUD:
    def test_create_read_update_delete_turn(self, client):
        sid = _create(client)
        created = client.post(f"/api/sessions/{sid}/turns/message", json={
            "role": "note", "text": "原始笔记",
        })
        assert created.status_code == 200, created.text
        tid = created.json()["turn"]["id"]
        assert created.json()["turn"]["role"] == "note"

        got = client.get(f"/api/sessions/{sid}/turns/{tid}")
        assert got.status_code == 200
        assert got.json()["turn"]["text"] == "原始笔记"

        listing = client.get(f"/api/sessions/{sid}/turns")
        assert listing.status_code == 200
        assert len(listing.json()["turns"]) == 1

        updated = client.patch(f"/api/sessions/{sid}/turns/{tid}", json={
            "text": "修改后的笔记", "role": "user",
        })
        assert updated.status_code == 200, updated.text
        assert updated.json()["turn"]["text"] == "修改后的笔记"
        assert updated.json()["turn"]["role"] == "user"

        deleted = client.delete(f"/api/sessions/{sid}/turns/{tid}")
        assert deleted.status_code == 200
        assert client.get(f"/api/sessions/{sid}/turns/{tid}").status_code == 404
        assert client.get(f"/api/sessions/{sid}").json()["session"]["turns"] == []

    def test_turn_ids_unique_after_delete(self, client):
        sid = _create(client)
        first = client.post(f"/api/sessions/{sid}/turns/message", json={
            "role": "note", "text": "a"}).json()["turn"]["id"]
        second = client.post(f"/api/sessions/{sid}/turns/message", json={
            "role": "note", "text": "b"}).json()["turn"]["id"]
        client.delete(f"/api/sessions/{sid}/turns/{first}")
        third = client.post(f"/api/sessions/{sid}/turns/message", json={
            "role": "note", "text": "c"}).json()["turn"]["id"]
        assert first != second != third

    def test_bad_role_422_and_missing_404(self, client):
        sid = _create(client)
        assert client.post(f"/api/sessions/{sid}/turns/message", json={
            "role": "alien", "text": "x"}).status_code == 422
        assert client.get(f"/api/sessions/{sid}/turns/t999").status_code == 404
        assert client.patch(f"/api/sessions/{sid}/turns/t999", json={"text": "x"}).status_code == 404
        assert client.delete(f"/api/sessions/{sid}/turns/t999").status_code == 404

    def test_turn_link_to_version_and_clear(self, client):
        sid = _create(client)
        created = client.post(f"/api/sessions/{sid}/turns/message", json={
            "role": "note", "text": "关联", "version_id": "v1",
        })
        assert created.status_code == 200
        tid = created.json()["turn"]["id"]
        assert created.json()["turn"]["version_id"] == "v1"

        unlinked = client.patch(f"/api/sessions/{sid}/turns/{tid}", json={
            "clear_version_id": True,
        })
        assert unlinked.json()["turn"]["version_id"] is None
        bad = client.patch(f"/api/sessions/{sid}/turns/{tid}", json={"version_id": "v9"})
        assert bad.status_code == 404


class TestVersionCRUD:
    def test_update_version_meta(self, client):
        sid = _create(client)
        updated = client.patch(f"/api/sessions/{sid}/versions/v1", json={
            "message": "重命名说明", "analysis": "补充说明", "source": "user",
        })
        assert updated.status_code == 200, updated.text
        v = updated.json()["version"]
        assert v["message"] == "重命名说明"
        assert v["analysis"] == "补充说明"
        assert v["source"] == "user"
        assert v["code"] == GOOD_CODE  # 代码快照不变

        bad = client.patch(f"/api/sessions/{sid}/versions/v1", json={"source": "alien"})
        assert bad.status_code == 422

    def test_delete_version_falls_back_current_and_relinks_children(self, client):
        sid = _create(client)
        edited = GOOD_CODE.replace("0.9)", "0.6)")
        assert client.put(f"/api/sessions/{sid}/code", json={"code": edited}).status_code == 200
        assert client.put(f"/api/sessions/{sid}/code", json={"code": GOOD_CODE}).status_code == 200
        # v1 -> v2 -> v3, current=v3
        session = client.get(f"/api/sessions/{sid}").json()["session"]
        assert session["current_version"] == "v3"

        # 删当前 v3,current 应回落父版本 v2
        deleted = client.delete(f"/api/sessions/{sid}/versions/v3")
        assert deleted.status_code == 200
        assert deleted.json()["session"]["current_version"] == "v2"
        assert [v["id"] for v in client.get(f"/api/sessions/{sid}/versions").json()["versions"]] == ["v1", "v2"]

        # 删中间 v2,current 回落 parent v1,v3 已不存在
        mid = client.delete(f"/api/sessions/{sid}/versions/v2")
        assert mid.json()["session"]["current_version"] == "v1"

        assert client.delete(f"/api/sessions/{sid}/versions/v9").status_code == 404

    def test_version_ids_unique_after_delete(self, client):
        sid = _create(client)
        v2 = client.put(f"/api/sessions/{sid}/code", json={"code": GOOD_CODE.replace("0.9)", "0.6)")})
        v2id = v2.json()["version"]["id"]
        client.delete(f"/api/sessions/{sid}/versions/{v2id}")
        v3 = client.put(f"/api/sessions/{sid}/code", json={"code": GOOD_CODE.replace("0.9)", "0.7)")})
        assert v3.json()["version"]["id"] == "v3"  # 不复用 v2


class TestPermanentDelete:
    def test_soft_delete_archives_and_purge_wipes(self, client, tmp_path):
        sid = _create(client)
        store_dir = tmp_path / "sessions"
        assert (store_dir / f"{sid}.json").exists()

        soft = client.delete(f"/api/sessions/{sid}")
        assert soft.json()["permanent"] is False
        assert not (store_dir / f"{sid}.json").exists()
        assert (store_dir / ".trash" / f"{sid}.json").exists()
        assert client.get(f"/api/sessions/{sid}").status_code == 404

        purged = client.delete(f"/api/sessions/{sid}?permanent=true")
        assert purged.status_code == 200
        assert purged.json()["permanent"] is True
        assert not (store_dir / ".trash" / f"{sid}.json").exists()
        assert client.delete(f"/api/sessions/{sid}?permanent=true").status_code == 404
