"""回测实时进度追踪(worker 增量事件 → 主进程汇总 → API 增量查询)。

机制:
- 每个回测分配一个 progress_id,worker 将阶段/日志/交易日进度以 JSONL 追加到事件文件;
- 主进程 run_backtest 在等待子进程的轮询中读取新增事件,汇总到内存 ProgressRegistry;
- API 通过 X-Progress-Id 请求头关联任意回测入口(chat/backtest/arena/verify/optimize/
  sessions/后台任务),前端 GET /api/progress/{id}?since=N 增量拉取,无需 SSE/WebSocket。

事件文件只在单次回测的临时目录中,生命周期随 run_backtest;registry 保留最近 N 个快照,
供请求结束后前端仍可回看完整进度与日志。
"""
from __future__ import annotations

import contextvars
import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# HTTP 请求头名:前端发起回测请求时携带,后端写入 ContextVar 供 run_backtest 读取
PROGRESS_HEADER = "x-progress-id"
_progress_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "aquant_progress_id", default=None
)


def set_progress_id(value: str | None) -> Any:
    """设置当前上下文的 progress_id,返回 token 供 reset_progress_id 复原。"""
    return _progress_id_var.set(value)


def reset_progress_id(token: Any) -> None:
    _progress_id_var.reset(token)


def current_progress_id() -> str | None:
    return _progress_id_var.get()


def _now() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


@dataclass
class ProgressState:
    """单个回测的进度快照 + 事件序列(seq 递增,支持 since 增量拉取)。"""

    id: str
    status: str = "pending"  # pending/running/completed/failed/cancelled
    stage: str = ""
    stage_label: str = ""
    percent: float = 0.0
    current_date: str | None = None
    done_days: int = 0
    total_days: int | None = None
    started_at: str = ""
    updated_at: str = ""
    finished_at: str | None = None
    error: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    _next_seq: int = field(default=0, repr=False)

    def add(
        self,
        type_: str,
        message: str = "",
        *,
        level: str = "INFO",
        data: dict[str, Any] | None = None,
        ts: str | None = None,
    ) -> None:
        ev: dict[str, Any] = {
            "seq": self._next_seq,
            "ts": ts or _now(),
            "type": type_,
            "level": level,
            "message": message,
        }
        if data:
            ev["data"] = data
        self._next_seq += 1
        self.events.append(ev)
        self.updated_at = ev["ts"]
        # 类型化更新摘要字段
        if type_ == "stage":
            d = data or {}
            self.stage = str(d.get("stage", self.stage))
            self.stage_label = str(d.get("label", self.stage_label))
            if "percent" in d:
                self.percent = float(d["percent"])
        elif type_ == "progress":
            d = data or {}
            if "current_date" in d:
                self.current_date = d["current_date"]
            if "done_days" in d:
                self.done_days = int(d["done_days"])
            if "total_days" in d and d["total_days"] is not None:
                self.total_days = int(d["total_days"])
            if "percent" in d:
                self.percent = float(d["percent"])
        elif type_ == "error":
            self.error = message

    def snapshot(self, since: int = 0) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "stage": self.stage,
            "stage_label": self.stage_label,
            "percent": round(self.percent, 1),
            "current_date": self.current_date,
            "done_days": self.done_days,
            "total_days": self.total_days,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "total_events": len(self.events),
            "events": [e for e in self.events if e["seq"] >= since],
        }


class ProgressRegistry:
    """进程内进度快照表(线程安全,保留最近 max_states 个)。"""

    def __init__(self, max_states: int = 200) -> None:
        self._states: dict[str, ProgressState] = {}
        self._max = max_states
        self._lock = threading.RLock()

    def start(self, pid: str) -> ProgressState:
        with self._lock:
            state = ProgressState(id=pid, status="running", started_at=_now(), updated_at=_now())
            self._states[pid] = state
            while len(self._states) > self._max:
                oldest = next(iter(self._states))
                self._states.pop(oldest, None)
            return state

    def get(self, pid: str) -> ProgressState | None:
        with self._lock:
            return self._states.get(pid)

    def finish(self, pid: str, status: str, error: str | None = None) -> None:
        with self._lock:
            state = self._states.get(pid)
            if state is None:
                return
            state.status = status
            state.finished_at = _now()
            if error:
                state.error = error
                state.add("error", error)

    def snapshot(self, pid: str, since: int = 0) -> dict[str, Any] | None:
        state = self.get(pid)
        return state.snapshot(since) if state else None


_registry = ProgressRegistry()


def get_registry() -> ProgressRegistry:
    return _registry


class ProgressWriter:
    """worker 侧:把进度事件以 JSONL 追加到文件(每行一个事件)。

    worker 独立于主进程,无法直接写内存 registry;通过文件做增量通道,
    主进程 ProgressReader 轮询读取新行并合并到 registry。
    """

    def __init__(self, path: str | Path | None) -> None:
        self.path = Path(path) if path else None

    def emit(
        self,
        type_: str,
        message: str = "",
        *,
        level: str = "INFO",
        data: dict[str, Any] | None = None,
    ) -> None:
        if self.path is None:
            return
        ev: dict[str, Any] = {
            "ts": _now(), "type": type_, "level": level, "message": message,
        }
        if data:
            ev["data"] = data
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        except OSError:
            pass  # 进度写入失败绝不阻塞回测


class ProgressReader:
    """主进程侧:按字节偏移增量读取事件文件,返回新事件列表。"""

    def __init__(self, path: str | Path | None) -> None:
        self.path = Path(path) if path else None
        self._offset = 0

    def read_new(self) -> list[dict[str, Any]]:
        if self.path is None:
            return []
        try:
            with open(self.path, "rb") as f:
                f.seek(self._offset)
                chunk = f.read()
                self._offset = f.tell()
        except OSError:
            return []
        events: list[dict[str, Any]] = []
        for line in chunk.decode("utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # 忽略写一半的行,下次 offset 已前移则不重复(可接受)
        return events
