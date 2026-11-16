"""SessionStore:多轮对话会话 + 策略版本快照的文件型持久化。

设计沿用 ExperienceStore 的「目录 + JSON + 可注入 dir」范式,但增加:
- 一次会话一个 JSON 文件(含 turns 与 versions),避免多文件一致性问题;
- 每个版本保存完整代码快照 + 来源(agent/user/initial/rollback)+ 指标与校验结论,
  回退只移动 current_version 指针,历史版本永不删除(全程可见可回退);
- 原子写(临时文件 + os.replace)与进程内 RLock(FastAPI 同步端点跑在线程池)。

目录约定:``~/.aquant/sessions/<sid>.json``;sid 形如 ``s_ab12cd34ef``,
只允许 [A-Za-z0-9_-],防止路径穿越。
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_SESSION_DIR = Path.home() / ".aquant" / "sessions"
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# 会话文件结构版本,便于后续迁移
SCHEMA_VERSION = 1


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class SessionStore:
    """会话仓库:create/get/list/版本追加/回退/对话轮次追加/删除。"""

    _LOCK = threading.RLock()

    def __init__(self, dir: Path | str | None = None) -> None:
        self.dir = Path(dir) if dir is not None else DEFAULT_SESSION_DIR
        self.dir.mkdir(parents=True, exist_ok=True)

    # ---- 路径与底层读写 ---------------------------------------------------
    def _path(self, sid: str) -> Path:
        if not isinstance(sid, str) or not _SAFE_ID.fullmatch(sid):
            raise ValueError(f"非法会话 ID:{sid!r}")
        return self.dir / f"{sid}.json"

    def _read(self, sid: str) -> dict[str, Any] | None:
        path = self._path(sid)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"会话文件不是 JSON 对象:{path}")
        return data

    def _write(self, sid: str, data: dict[str, Any]) -> dict[str, Any]:
        data["updated_at"] = _now()
        path = self._path(sid)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return data

    # ---- 会话生命周期 -----------------------------------------------------
    def create(
        self,
        *,
        start: str,
        end: str,
        code: str | None = None,
        title: str | None = None,
        cash: float | None = None,
    ) -> dict[str, Any]:
        """新建会话;code 非空时落为 v1(initial),否则 sessions 无版本(首轮由 agent 生成)。"""
        with self._LOCK:
            sid = "s_" + uuid.uuid4().hex[:10]
            ts = _now()
            session: dict[str, Any] = {
                "schema": SCHEMA_VERSION,
                "id": sid,
                "created_at": ts,
                "updated_at": ts,
                "title": title or "未命名策略会话",
                "start": start,
                "end": end,
                "cash": cash,
                "current_version": None,
                "versions": [],
                "turns": [],
            }
            if code:
                session["versions"].append({
                    "id": "v1",
                    "parent": None,
                    "code": code,
                    "source": "initial",
                    "message": title or "初始版本",
                    "analysis": "",
                    "created_at": ts,
                    "metrics": None,
                    "verdict": None,
                })
                session["current_version"] = "v1"
            return self._write(sid, session)

    def get(self, sid: str) -> dict[str, Any] | None:
        with self._LOCK:
            return self._read(sid)

    def list(self) -> list[dict[str, Any]]:
        """会话摘要列表(不含完整代码/回测结果),按 updated_at 降序。"""
        with self._LOCK:
            items: list[dict[str, Any]] = []
            for path in self.dir.glob("*.json"):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if not isinstance(data, dict) or not data.get("id"):
                    continue
                items.append({
                    "id": data["id"],
                    "title": data.get("title") or "未命名策略会话",
                    "start": data.get("start"),
                    "end": data.get("end"),
                    "updated_at": data.get("updated_at"),
                    "current_version": data.get("current_version"),
                    "version_count": len(data.get("versions") or []),
                    "turn_count": len(data.get("turns") or []),
                })
            items.sort(key=lambda x: str(x.get("updated_at") or ""), reverse=True)
            return items

    def update(
        self,
        sid: str,
        *,
        title: str | None = None,
        start: str | None = None,
        end: str | None = None,
        cash: float | None = None,
    ) -> dict[str, Any]:
        """更新会话元信息(仅覆盖非 None 字段,避免 PATCH 未传字段被误清空)。"""
        with self._LOCK:
            data = self._require(sid)
            for key, value in (("title", title), ("start", start), ("end", end), ("cash", cash)):
                if value is not None:
                    data[key] = value
            return self._write(sid, data)

    def delete(self, sid: str) -> bool:
        """软删除:把会话文件移入 .trash/,list() 只扫顶层 *.json 故不会列出。

        归档而非硬删符合「历史不丢」设计,也避开部分 IDE 插件对
        Path.unlink / os.unlink 的回收站劫持(其在非标准挂载点会抛错)。
        """
        with self._LOCK:
            path = self._path(sid)
            if not path.exists():
                return False
            trash = self.dir / ".trash"
            trash.mkdir(parents=True, exist_ok=True)
            target = trash / path.name
            if target.exists():  # 同名归档覆盖:目标为旧会话文件,内容已被新删除取代
                os.unlink(target)
            os.replace(path, target)
            return True

    # ---- 版本 -------------------------------------------------------------
    def _require(self, sid: str) -> dict[str, Any]:
        data = self._read(sid)
        if data is None:
            raise KeyError(f"会话不存在:{sid}")
        return data

    @staticmethod
    def _version_summary(version: dict[str, Any]) -> dict[str, Any]:
        """版本摘要(列表展示用,不含完整 code 与 verdict)。"""
        return {
            "id": version.get("id"),
            "parent": version.get("parent"),
            "source": version.get("source"),
            "message": version.get("message"),
            "analysis": version.get("analysis"),
            "created_at": version.get("created_at"),
            "metrics": version.get("metrics"),
            "lines": len(str(version.get("code") or "").splitlines()),
        }

    def get_version(self, sid: str, vid: str) -> dict[str, Any] | None:
        with self._LOCK:
            data = self._require(sid)
            for version in data.get("versions") or []:
                if version.get("id") == vid:
                    return version
            return None

    def current(self, sid: str) -> dict[str, Any] | None:
        with self._LOCK:
            data = self._require(sid)
            return self.get_version(sid, str(data.get("current_version") or ""))

    def list_versions(self, sid: str) -> list[dict[str, Any]]:
        with self._LOCK:
            data = self._require(sid)
            return [self._version_summary(v) for v in data.get("versions") or []]

    def add_version(
        self,
        sid: str,
        *,
        code: str,
        source: str = "agent",
        message: str = "",
        analysis: str = "",
        metrics: dict[str, Any] | None = None,
        verdict: dict[str, Any] | None = None,
        backtest: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """追加完整策略快照并设为当前版本;parent 为追加前的 current。"""
        if not code or not code.strip():
            raise ValueError("版本代码不能为空")
        if source not in {"initial", "agent", "user", "rollback"}:
            raise ValueError(f"未知版本来源:{source!r}")
        with self._LOCK:
            data = self._require(sid)
            versions = data.setdefault("versions", [])
            parent = data.get("current_version")
            vid = f"v{len(versions) + 1}"
            version = {
                "id": vid,
                "parent": parent,
                "code": code,
                "source": source,
                "message": message or "",
                "analysis": analysis or "",
                "created_at": _now(),
                "metrics": metrics,
                "verdict": verdict,
                "backtest": backtest,
            }
            versions.append(version)
            data["current_version"] = vid
            self._write(sid, data)
            return dict(version)

    def update_version(
        self,
        sid: str,
        vid: str,
        *,
        metrics: dict[str, Any] | None = None,
        verdict: dict[str, Any] | None = None,
        backtest: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """原地更新版本的回测结果(手动回测后回写,不新增版本、不改代码)。"""
        with self._LOCK:
            data = self._require(sid)
            for version in data.get("versions") or []:
                if version.get("id") == vid:
                    if metrics is not None:
                        version["metrics"] = metrics
                    if verdict is not None:
                        version["verdict"] = verdict
                    if backtest is not None:
                        version["backtest"] = backtest
                    self._write(sid, data)
                    return dict(version)
            raise KeyError(f"版本不存在:{vid}")

    def rollback(self, sid: str, vid: str) -> dict[str, Any]:
        """回退:只移动 current 指针到历史版本,不删任何快照(可再次前进)。"""
        with self._LOCK:
            data = self._require(sid)
            target = self.get_version(sid, vid)
            if target is None:
                raise KeyError(f"版本不存在:{vid}")
            data["current_version"] = vid
            self._write(sid, data)
            return dict(target)

    # ---- 对话轮次 ---------------------------------------------------------
    def add_turn(
        self,
        sid: str,
        *,
        role: str,
        text: str,
        version_id: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """追加一条消息(role=user/agent);result 存放精简后的执行结果。"""
        if role not in {"user", "agent"}:
            raise ValueError(f"未知消息角色:{role!r}")
        with self._LOCK:
            data = self._require(sid)
            turns = data.setdefault("turns", [])
            turn = {
                "id": f"t{len(turns) + 1}",
                "role": role,
                "text": text,
                "created_at": _now(),
                "version_id": version_id,
                "result": result,
            }
            turns.append(turn)
            self._write(sid, data)
            return turn

    def get_turns(self, sid: str) -> list[dict[str, Any]]:
        with self._LOCK:
            return list(self._require(sid).get("turns") or [])
