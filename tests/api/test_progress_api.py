"""回测实时进度追踪测试:事件文件汇总、registry 增量快照、API 查询与取消状态。"""

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from aquant.api import main as main_mod
from aquant.api.main import app
from aquant.runner import backtest as bt_mod
from aquant.runner.progress import (
    ProgressReader, ProgressRegistry, ProgressState, ProgressWriter,
    set_progress_id,
)
from stubs import GOOD_CODE

START, END = "2024-01-02", "2024-06-28"


@pytest.fixture()
def client():
    app.state.backtest_jobs = None
    with TestClient(app) as c:
        yield c
    app.state.backtest_jobs = None


class TestProgressStateSnapshot:
    def test_incremental_since(self):
        st = ProgressState(id="p1")
        st.add("stage", "开始", data={"stage": "running", "label": "回测中", "percent": 10})
        st.add("log", "第一行日志")
        st.add("progress", "第1日", data={"current_date": "2024-01-02", "done_days": 1})
        snap0 = st.snapshot(since=0)
        assert snap0["total_events"] == 3
        assert len(snap0["events"]) == 3
        assert snap0["stage"] == "running"
        assert snap0["stage_label"] == "回测中"
        assert snap0["percent"] == 10
        assert snap0["current_date"] == "2024-01-02"
        snap2 = st.snapshot(since=2)
        assert len(snap2["events"]) == 1
        assert snap2["events"][0]["seq"] == 2

    def test_registry_finish(self):
        reg = ProgressRegistry()
        reg.start("p2")
        reg.finish("p2", "failed", "回测失败")
        snap = reg.snapshot("p2")
        assert snap["status"] == "failed"
        assert snap["error"] == "回测失败"
        assert any(e["type"] == "error" for e in snap["events"])
        assert reg.snapshot("missing") is None


class TestProgressFileChannel:
    def test_writer_reader_roundtrip(self, tmp_path):
        path = tmp_path / "progress.jsonl"
        writer = ProgressWriter(path)
        reader = ProgressReader(path)
        writer.emit("stage", "初始化", data={"stage": "init"})
        writer.emit("log", "策略 print")
        writer.emit("progress", "第1日", data={"done_days": 1})
        events = reader.read_new()
        assert [e["type"] for e in events] == ["stage", "log", "progress"]
        assert reader.read_new() == []  # 增量:重复读为空

    def test_no_file_is_noop(self):
        writer = ProgressWriter(None)
        writer.emit("log", "静默")  # 不报错
        assert ProgressReader(None).read_new() == []


class TestProgressApi:
    def test_unknown_id_404(self, client):
        assert client.get("/api/progress/no_such_id").status_code == 404

    def test_progress_during_real_backtest(self, client, monkeypatch):
        """真实 run_backtest + progress_id:运行中可查到阶段/日志/进度事件。"""
        monkeypatch.setenv("AQUANT_BUNDLE_PATH", "/home/ubuntu/.aquant/bundle-baostock")
        pid = "bt_test_progress_001"
        result: dict = {}

        def runner():
            from aquant.runner.progress import set_progress_id
            set_progress_id(pid)
            try:
                res = bt_mod.run_backtest(GOOD_CODE, START, END)
                result["ok"] = True
                result["trades"] = len(res.trades)
            except Exception as exc:
                result["error"] = str(exc)

        th = threading.Thread(target=runner, daemon=True)
        th.start()

        # 轮询进度,收集阶段与日志
        seen_types = set()
        deadline = time.time() + 60
        final = None
        while time.time() < deadline:
            resp = client.get("/api/progress/" + pid)
            if resp.status_code == 200:
                snap = resp.json()
                seen_types.update(e["type"] for e in snap["events"])
                final = snap
                if snap["status"] in ("completed", "failed", "cancelled"):
                    break
            time.sleep(0.3)
        th.join(timeout=5)

        assert result.get("ok") is True, result
        assert final is not None
        assert final["status"] == "completed"
        assert "stage" in seen_types
        assert "log" in seen_types
        assert "progress" in seen_types
        assert final["percent"] == 100

    def test_contextvar_from_header(self, client, monkeypatch):
        """X-Progress-Id 请求头经 middleware 注入 ContextVar。"""
        monkeypatch.setenv("AQUANT_BUNDLE_PATH", "/home/ubuntu/.aquant/bundle-baostock")
        pid = "bt_test_header_002"
        resp = client.post(
            "/api/backtests",
            json={"code": GOOD_CODE, "start": START, "end": END},
            headers={"x-progress-id": pid},
        )
        assert resp.status_code == 200
        jid = resp.json()["job_id"]
        deadline = time.time() + 60
        final = None
        while time.time() < deadline:
            pr = client.get("/api/progress/" + pid)
            if pr.status_code == 200:
                final = pr.json()
                if final["status"] in ("completed", "failed", "cancelled"):
                    break
            time.sleep(0.3)
        assert final is not None
        assert final["status"] == "completed"
        assert client.get("/api/backtests/" + jid).json()["status"] == "completed"
