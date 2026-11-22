"""后台回测任务管理:提交、状态查询、取消。

任务保存在内存中(进程重启后清空);回测本身仍在独立子进程执行,
取消通过 threading.Event 通知 run_backtest 终止子进程。
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

__all__ = ["BacktestJob", "BacktestJobManager"]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class BacktestJob:
    id: str
    request: dict[str, Any]
    status: str = "running"
    created_at: str = field(default_factory=_now)
    started_at: str | None = None
    finished_at: str | None = None
    result: Any = None
    error: str | None = None
    error_kind: str | None = None
    # 校验拦截时附带 verdict dict,供前端展示 findings(与同步 /api/backtest 对齐)
    error_verdict: Any = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = field(default=None, repr=False)
    progress_id: str | None = None

    @property
    def elapsed_seconds(self) -> float | None:
        if self.started_at is None:
            return None
        end = datetime.fromisoformat(self.finished_at) if self.finished_at else datetime.now()
        return round((end - datetime.fromisoformat(self.started_at)).total_seconds(), 2)

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "elapsed_seconds": self.elapsed_seconds,
            "start": self.request.get("start"),
            "end": self.request.get("end"),
            "strategy_name": self.request.get("strategy_name") or "platform_strategy",
            "error": self.error,
        }

    def detail(self) -> dict[str, Any]:
        return {
            **self.summary(),
            "request": self.request,
            "result": self.result,
            "error_verdict": self.error_verdict,
            "progress_id": self.progress_id or self.id,
        }


class BacktestJobManager:
    def __init__(self, max_jobs: int = 100) -> None:
        self._jobs: dict[str, BacktestJob] = {}
        self._max_jobs = max_jobs
        self._lock = threading.RLock()

    def submit(
        self,
        request: dict[str, Any],
        runner: Callable[[dict[str, Any], threading.Event], Any],
        job_id: str | None = None,
    ) -> BacktestJob:
        job = BacktestJob(id=job_id or f"bt_{uuid.uuid4().hex[:10]}", request=dict(request))
        job.started_at = _now()
        job.progress_id = request.get("_progress_id")

        def target() -> None:
            try:
                job.result = runner(job.request, job.cancel_event)
                job.status = "completed"
            except Exception as exc:
                job.error = str(exc)
                job.error_kind = type(exc).__name__
                verdict = getattr(exc, "verdict", None)
                if verdict is not None:
                    job.error_verdict = (
                        verdict.to_dict() if hasattr(verdict, "to_dict") else verdict
                    )
                job.status = "cancelled" if job.error_kind == "BacktestCancelled" else "failed"
            finally:
                job.finished_at = _now()

        thread = threading.Thread(
            target=target, name=f"backtest-{job.id}", daemon=True
        )
        job.thread = thread
        with self._lock:
            self._jobs[job.id] = job
            self._trim()
        thread.start()
        return job

    def _trim(self) -> None:
        finished = [j for j in self._jobs.values() if j.status != "running"]
        finished.sort(key=lambda j: j.created_at)
        excess = len(self._jobs) - self._max_jobs
        for job in finished[:max(0, excess)]:
            self._jobs.pop(job.id, None)

    def cancel(self, jid: str) -> bool:
        with self._lock:
            job = self._jobs.get(jid)
            if job is None or job.status != "running" or job.cancel_event.is_set():
                return False
            job.cancel_event.set()
            return True

    def get(self, jid: str) -> BacktestJob | None:
        with self._lock:
            return self._jobs.get(jid)

    def list(self, status: str | None = None) -> list[BacktestJob]:
        with self._lock:
            jobs = list(self._jobs.values())
        if status:
            jobs = [j for j in jobs if j.status == status]
        return sorted(jobs, key=lambda j: j.created_at, reverse=True)
