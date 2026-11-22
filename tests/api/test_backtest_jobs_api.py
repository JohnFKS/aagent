"""后台回测任务测试:任务管理器状态机 + API 提交/列表/详情/取消 + token 路由登记。

真实回测子进程不参与测试:API 层用可控 stub 替换 runner,取消链路验证 cancel_event
传递到 run_backtest(通过单独的子进程 mock 测试验证 BacktestCancelled)。
"""

import subprocess
import threading
import time

import pytest
from fastapi.testclient import TestClient

from aquant.api import main as main_mod
from aquant.api.main import app
from aquant.api.token_gate import _path_allowed, _reset_rate_limits
from aquant.runner.backtest_jobs import BacktestJobManager

START, END = "2024-01-02", "2024-06-28"
CODE = "def init(context):\n    pass\n\ndef handle_bar(context, bar_dict):\n    pass\n"


def quick_runner(payload, cancel_event):
    """立即完成的 stub runner:返回与真实 _job_result_payload 兼容的 dict。"""
    return {
        "summary": {"total_returns": 0.1, "sharpe": 1.2},
        "portfolio": [],
        "trades": [],
        "run_log": [],
        "daily_holdings": [],
        "verdict": None,
    }


def wait_status(getter, jid, target, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        detail = getter()
        if detail["status"] == target:
            return detail
        time.sleep(0.03)
    return getter()


@pytest.fixture()
def client(monkeypatch):
    """隔离任务管理器 + 替换真实回测为 stub,避免跑 bundle 与子进程。"""
    monkeypatch.setattr(main_mod, "_run_backtest_job", quick_runner)
    app.state.backtest_jobs = None
    _reset_rate_limits()
    with TestClient(app) as c:
        yield c
    app.state.backtest_jobs = None


class TestBacktestJobManager:
    def test_submit_completed_and_summary(self):
        mgr = BacktestJobManager()
        job = mgr.submit({"start": START, "end": END}, lambda p, e: {"ok": 1})
        wait_status(lambda: {"status": job.status}, job.id, "completed")
        assert job.status == "completed"
        assert job.result == {"ok": 1}
        summary = job.summary()
        assert summary["status"] == "completed"
        assert summary["start"] == START
        assert summary["elapsed_seconds"] is not None
        assert job.detail()["result"] == {"ok": 1}

    def test_failed_status_on_exception(self):
        mgr = BacktestJobManager()

        def boom(payload, cancel_event):
            raise ValueError("boom")

        job = mgr.submit({}, boom)
        wait_status(lambda: {"status": job.status}, job.id, "failed")
        assert job.status == "failed"
        assert "boom" in job.error
        assert job.error_kind == "ValueError"

    def test_cancel_event_and_idempotency(self):
        mgr = BacktestJobManager()
        gate = threading.Event()

        def slow(payload, cancel_event):
            cancel_event.wait(2)

        job = mgr.submit({}, slow)
        assert mgr.cancel(job.id) is True
        assert job.cancel_event.is_set()
        assert mgr.cancel(job.id) is False  # 重复取消返回 False
        assert mgr.cancel("bt_not_exist") is False
        wait_status(lambda: {"status": job.status}, job.id, "completed")
        gate.set()

    def test_list_filter_and_trim(self):
        mgr = BacktestJobManager(max_jobs=2)
        for i in range(3):
            job = mgr.submit({"n": i}, lambda p, e: p["n"])
            wait_status(lambda: {"status": job.status}, job.id, "completed")
        jobs = mgr.list()
        assert len(jobs) == 2  # 超额的老任务被裁剪
        assert {j.request["n"] for j in jobs} == {1, 2}  # 保留最新两个
        assert len(mgr.list(status="completed")) == 2
        assert mgr.list(status="running") == []


class TestBacktestJobsApi:
    def test_submit_list_detail(self, client):
        resp = client.post("/api/backtests", json={"code": CODE, "start": START, "end": END})
        assert resp.status_code == 200
        body = resp.json()
        jid = body["job_id"]
        assert jid.startswith("bt_")
        assert body["status"] in ("running", "completed")  # stub 极快时可能已完成

        detail = wait_status(
            lambda: client.get(f"/api/backtests/{jid}").json(), jid, "completed"
        )
        assert detail["status"] == "completed"
        assert detail["result"]["passed"] is True
        assert detail["request"]["code"] == CODE

        listing = client.get("/api/backtests").json()
        assert listing["count"] >= 1
        assert any(j["id"] == jid for j in listing["jobs"])

        filtered = client.get("/api/backtests?status=completed").json()
        assert any(j["id"] == jid for j in filtered["jobs"])
        assert all(j["status"] == "completed" for j in filtered["jobs"])

    def test_get_missing_job_returns_404(self, client):
        assert client.get("/api/backtests/bt_missing").status_code == 404

    def test_cancel_running_job(self, client, monkeypatch):
        from aquant.runner.backtest import BacktestCancelled

        def blocking_runner(payload, cancel_event):
            while not cancel_event.wait(0.02):
                pass
            raise BacktestCancelled("user cancelled")

        monkeypatch.setattr(main_mod, "_run_backtest_job", blocking_runner)
        resp = client.post("/api/backtests", json={"code": CODE, "start": START, "end": END})
        jid = resp.json()["job_id"]
        time.sleep(0.1)  # 等守护线程进入阻塞

        cancel = client.post(f"/api/backtests/{jid}/cancel")
        assert cancel.status_code == 200
        assert cancel.json() == {"ok": True, "id": jid}

        detail = wait_status(
            lambda: client.get(f"/api/backtests/{jid}").json(), jid, "cancelled"
        )
        assert detail["status"] == "cancelled"
        assert "user cancelled" in detail["error"]
        # 已结束任务重复取消返回 ok=false
        assert client.post(f"/api/backtests/{jid}/cancel").json()["ok"] is False


class TestTokenGateRoutes:
    def test_new_routes_registered(self):
        assert _path_allowed("readonly", "/api/backtests") is True
        assert _path_allowed("readonly", "/api/backtests/bt_1") is True
        assert _path_allowed("readwrite", "/api/backtests/bt_1/cancel") is True

    def test_token_gate_allows_backtests(self, client, monkeypatch):
        monkeypatch.setenv("AQUANT_MCP_TOKENS", '{"tk_test": "readwrite"}')
        _reset_rate_limits()
        resp = client.get("/api/backtests", headers={"x-mcp-token": "tk_test"})
        assert resp.status_code == 200
        resp2 = client.post(
            "/api/backtests",
            json={"code": CODE, "start": START, "end": END},
            headers={"x-mcp-token": "tk_test"},
        )
        assert resp2.status_code == 200


class _FakeProc:
    """模拟回测子进程:poll 保持运行,terminate/kill 记录调用并设置返回码。"""

    def __init__(self, *args, **kwargs):
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9
        return self.returncode

    def communicate(self):
        return "", ""


class TestRunBacktestCancellation:
    def test_cancel_event_terminates_subprocess(self, monkeypatch, tmp_path):
        from aquant.runner import backtest as bt_mod
        from aquant.runner.backtest import BacktestCancelled
        from stubs import GOOD_CODE

        fake = _FakeProc()
        monkeypatch.setattr(bt_mod.subprocess, "Popen", lambda *a, **k: fake)
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        cancel = threading.Event()
        cancel.set()  # 进入轮询即取消

        with pytest.raises(BacktestCancelled):
            bt_mod.run_backtest(
                GOOD_CODE, START, END, bundle_path=bundle, cancel_event=cancel
            )
        assert fake.terminated is True
        assert fake.killed is False

    def test_timeout_kills_subprocess(self, monkeypatch, tmp_path):
        from aquant.runner import backtest as bt_mod
        from stubs import GOOD_CODE

        fake = _FakeProc()
        monkeypatch.setattr(bt_mod.subprocess, "Popen", lambda *a, **k: fake)
        bundle = tmp_path / "bundle"
        bundle.mkdir()

        with pytest.raises(subprocess.TimeoutExpired):
            bt_mod.run_backtest(
                GOOD_CODE, START, END, bundle_path=bundle, timeout=0
            )
        assert fake.killed is True
