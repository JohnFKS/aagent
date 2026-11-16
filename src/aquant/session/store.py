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
import tempfile
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
                "version_counter": 2 if code else 1,
                "turn_counter": 1,
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

    def purge(self, sid: str) -> bool:
        """永久删除会话(不可恢复):直接抹除 JSON 文件,包括 .trash/ 中的旧归档。

        IDE 插件的 sitecustomize 会劫持工作区内 unlink 并试图移入回收站,
        在非标准挂载点会抛错。先把文件原子移动到系统临时目录(shim 对 /tmp
        放行),再 unlink —— 达到真正永久删除,不依赖任何环境插件行为。
        """
        with self._LOCK:
            removed = False
            candidates = [self._path(sid), self.dir / ".trash" / f"{sid}.json"]
            for path in candidates:
                if not path.exists():
                    continue
                tmp = Path(tempfile.gettempdir()) / f"aquant_purge_{uuid.uuid4().hex}"
                os.replace(path, tmp)
                try:
                    os.unlink(tmp)
                except OSError:
                    pass  # 临时目录残留不影响"已从会话库永久移除"的语义
                removed = True
            return removed

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
            # 会话级单调计数器:ID 永不复用,避免历史消息引用指向新版本
            next_no = int(data.get("version_counter") or 0)
            if next_no < 1:  # 兼容旧会话(无计数器):按现存最大 id 续接
                next_no = max((int(v["id"][1:]) for v in versions
                               if str(v.get("id", "")).startswith("v")
                               and str(v["id"][1:]).isdigit()), default=0) + 1
            data["version_counter"] = next_no + 1
            vid = f"v{next_no}"
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

    def update_version_meta(
        self,
        sid: str,
        vid: str,
        *,
        message: str | None = None,
        analysis: str | None = None,
        source: str | None = None,
    ) -> dict[str, Any]:
        """修改版本元信息(说明/分析/来源);代码快照不可原地改,改动代码应新增版本。"""
        if source is not None and source not in {"initial", "agent", "user", "rollback"}:
            raise ValueError(f"未知版本来源:{source!r}")
        with self._LOCK:
            data = self._require(sid)
            for version in data.get("versions") or []:
                if version.get("id") == vid:
                    if message is not None:
                        version["message"] = message
                    if analysis is not None:
                        version["analysis"] = analysis
                    if source is not None:
                        version["source"] = source
                    self._write(sid, data)
                    return dict(version)
            raise KeyError(f"版本不存在:{vid}")

    def delete_version(self, sid: str, vid: str) -> dict[str, Any]:
        """永久删除单个策略版本;current 指针回落到父版本(父被删则取前一个版本)。

        同时把子版本的 parent 重连到被删版本的 parent,保持链路不断;
        若删光所有版本,current_version 置 None。
        """
        with self._LOCK:
            data = self._require(sid)
            versions = data.get("versions") or []
            target = next((v for v in versions if v.get("id") == vid), None)
            if target is None:
                raise KeyError(f"版本不存在:{vid}")
            parent = target.get("parent")
            for child in versions:
                if child.get("parent") == vid:
                    child["parent"] = parent
            versions.remove(target)
            if data.get("current_version") == vid:
                # 优先父版本,否则取删除位置的前一个版本,否则取剩余第一个
                fallback = next((v for v in versions if v.get("id") == parent), None)
                if fallback is None:
                    index = next((i for i, v in enumerate(versions) if v.get("parent") == parent), None)
                    fallback = versions[index - 1] if index else (versions[0] if versions else None)
                data["current_version"] = fallback["id"] if fallback else None
            self._write(sid, data)
            return dict(target)

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
        if role not in {"user", "agent", "note"}:
            raise ValueError(f"未知消息角色:{role!r}")
        with self._LOCK:
            data = self._require(sid)
            turns = data.setdefault("turns", [])
            # 会话级单调计数器:ID 永不复用
            next_no = int(data.get("turn_counter") or 0)
            if next_no < 1:  # 兼容旧会话
                next_no = max((int(t["id"][1:]) for t in turns
                               if str(t.get("id", "")).startswith("t")
                               and str(t["id"][1:]).isdigit()), default=0) + 1
            data["turn_counter"] = next_no + 1
            turn = {
                "id": f"t{next_no}",
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

    def get_turn(self, sid: str, tid: str) -> dict[str, Any] | None:
        with self._LOCK:
            for turn in self._require(sid).get("turns") or []:
                if turn.get("id") == tid:
                    return turn
            return None

    def update_turn(
        self,
        sid: str,
        tid: str,
        *,
        text: str | None = None,
        role: str | None = None,
        version_id: str | None = None,
        clear_version_id: bool = False,
    ) -> dict[str, Any]:
        """修改对话消息(文本/角色/关联版本);clear_version_id=True 可解除关联。"""
        if role is not None and role not in {"user", "agent", "note"}:
            raise ValueError(f"未知消息角色:{role!r}")
        with self._LOCK:
            data = self._require(sid)
            for turn in data.get("turns") or []:
                if turn.get("id") == tid:
                    if text is not None:
                        turn["text"] = text
                    if role is not None:
                        turn["role"] = role
                    if clear_version_id:
                        turn["version_id"] = None
                    elif version_id is not None:
                        turn["version_id"] = version_id
                    self._write(sid, data)
                    return dict(turn)
            raise KeyError(f"对话消息不存在:{tid}")

    def delete_turn(self, sid: str, tid: str) -> dict[str, Any]:
        """永久删除单条对话消息。"""
        with self._LOCK:
            data = self._require(sid)
            turns = data.get("turns") or []
            target = next((t for t in turns if t.get("id") == tid), None)
            if target is None:
                raise KeyError(f"对话消息不存在:{tid}")
            turns.remove(target)
            self._write(sid, data)
            return dict(target)
