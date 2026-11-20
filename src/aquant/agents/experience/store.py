"""经验库文件存储(补充上游仓库被 .gitignore 误忽略的模块)。"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

DEFAULT_EXPERIENCE_DIR = Path.home() / ".aquant" / "experience"
MAX_CODE_LINES = 40

# 标签规则:顺序即输出顺序;ASCII 关键词按字母数字边界匹配。
_TAG_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("均线", ("均线", "金叉", "死叉", "ma")),
    ("macd", ("macd",)),
    ("rsi", ("rsi", "超卖", "超买")),
    ("价值", ("价值", "估值", "pe", "pb", "基本面")),
    ("etf", ("etf",)),
    ("打板", ("打板", "涨停", "龙头")),
    ("止损", ("止损",)),
    ("仓位", ("仓位", "持仓")),
    ("分红", ("分红", "股息")),
)

_CODE_RE = re.compile(r"\b\d{6}\.(?:XSHE|XSHG|BJ)\b")
_ASCII_BOUNDARY_CACHE: dict[str, re.Pattern[str]] = {}


def _matches(text: str, keyword: str) -> bool:
    if keyword.isascii():
        pattern = _ASCII_BOUNDARY_CACHE.get(keyword)
        if pattern is None:
            pattern = re.compile(
                rf"(?<![A-Za-z0-9]){re.escape(keyword)}(?![A-Za-z0-9])",
                re.IGNORECASE,
            )
            _ASCII_BOUNDARY_CACHE[keyword] = pattern
        return pattern.search(text) is not None
    return keyword in text


def derive_tags(requirement: str, code: str | None = None) -> list[str]:
    """从需求文本与策略代码提取经验库标签(关键词在前,标的代码在后)。"""
    text = requirement or ""
    tags: list[str] = []
    for tag, keywords in _TAG_RULES:
        if any(_matches(text, keyword) for keyword in keywords):
            tags.append(tag)
    for instrument in _CODE_RE.findall(code or ""):
        if instrument not in tags:
            tags.append(instrument)
    return tags


def _parse_ts(value: Any) -> datetime:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return datetime.min


class ExperienceStore:
    """按目录 + JSON 落盘的经验库,检索按标签重叠数排序、同分取新。"""

    def __init__(self, dir: Path | str | None = None) -> None:
        self.dir = Path(dir) if dir is not None else DEFAULT_EXPERIENCE_DIR

    def record(self, entry: dict[str, Any]) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        data = dict(entry)
        data.setdefault("ts", datetime.now().isoformat(timespec="seconds"))
        now = datetime.now()
        filename = f"{now:%Y%m%d%H%M%S%f}-{uuid.uuid4().hex[:6]}.json"
        path = self.dir / filename
        path.write_text(
            json.dumps(data, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        return path

    def search(self, query_tags: Iterable[str], top_k: int = 3) -> list[dict[str, Any]]:
        query = set(query_tags or [])
        if not query or top_k <= 0 or not self.dir.exists():
            return []
        scored: list[tuple[int, datetime, dict[str, Any]]] = []
        for path in self.dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            if not isinstance(data, dict):
                continue
            entry_tags = data.get("tags")
            if not isinstance(entry_tags, (list, tuple, set)):
                continue
            overlap = len(query & set(entry_tags))
            if overlap:
                scored.append((overlap, _parse_ts(data.get("ts")), data))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [data for _, _, data in scored[:top_k]]
