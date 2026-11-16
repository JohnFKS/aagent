"""策略代码行级 diff:后端统一计算,前端只负责渲染。

用 difflib.SequenceMatcher 生成 opcodes,输出:
- rows: [{type: context|add|remove, left_no, right_no, text}],按顺序展示;
- unified: 标准 unified diff 文本(便于复制/归档);
- stats: {added, removed}。
"""

from __future__ import annotations

import difflib
from typing import Any

# 单侧超长行在 diff 视图中的截断长度(避免撑爆前端)
_MAX_LINE_CHARS = 2000


def _clip(line: str) -> str:
    return line if len(line) <= _MAX_LINE_CHARS else line[:_MAX_LINE_CHARS] + " …(已截断)"


def diff_codes(
    old: str,
    new: str,
    *,
    from_label: str = "旧版本",
    to_label: str = "新版本",
) -> dict[str, Any]:
    """返回两个策略源码的结构化 diff(纯函数,无副作用)。"""
    old_lines = str(old or "").splitlines()
    new_lines = str(new or "").splitlines()
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)

    rows: list[dict[str, Any]] = []
    added = 0
    removed = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(i2 - i1):
                rows.append({
                    "type": "context",
                    "left_no": i1 + offset + 1,
                    "right_no": j1 + offset + 1,
                    "text": _clip(old_lines[i1 + offset]),
                })
        elif tag == "delete":
            for offset in range(i2 - i1):
                rows.append({
                    "type": "remove",
                    "left_no": i1 + offset + 1,
                    "right_no": None,
                    "text": _clip(old_lines[i1 + offset]),
                })
                removed += 1
        elif tag == "insert":
            for offset in range(j2 - j1):
                rows.append({
                    "type": "add",
                    "left_no": None,
                    "right_no": j1 + offset + 1,
                    "text": _clip(new_lines[j1 + offset]),
                })
                added += 1
        else:  # replace:先删后增,保持阅读顺序
            for offset in range(i2 - i1):
                rows.append({
                    "type": "remove",
                    "left_no": i1 + offset + 1,
                    "right_no": None,
                    "text": _clip(old_lines[i1 + offset]),
                })
                removed += 1
            for offset in range(j2 - j1):
                rows.append({
                    "type": "add",
                    "left_no": None,
                    "right_no": j1 + offset + 1,
                    "text": _clip(new_lines[j1 + offset]),
                })
                added += 1

    unified = "\n".join(difflib.unified_diff(
        old_lines, new_lines, fromfile=from_label, tofile=to_label, lineterm=""
    ))
    return {
        "from_label": from_label,
        "to_label": to_label,
        "rows": rows,
        "unified": unified,
        "stats": {"added": added, "removed": removed, "total": len(rows)},
    }
