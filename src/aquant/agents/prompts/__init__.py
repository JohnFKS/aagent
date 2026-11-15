"""硬约束 prompt 模板包。

模板为纯文本 .md(占位符用 <<<NAME>>> 形式,避免与 JSON/代码中的花括号冲突),
由 :func:`load_prompt` 统一加载。借鉴 PA_Agent 的"硬约束模板 + 尾置必做清单"模式。
"""

from __future__ import annotations

import re
from importlib import resources
from pathlib import Path

_PLACEHOLDER_RE = re.compile(r"<<<([A-Z_]+)>>>")


def render_prompt(name: str, **values: str) -> str:
    """加载模板并替换 <<<KEY>>> 占位符。

    :param name: 模板文件名,如 ``strategy_editor.md``。
    :param values: 占位符取值(键名大写);未提供的占位符替换为空串。
    """
    text = load_prompt(name)
    return _PLACEHOLDER_RE.sub(lambda m: values.get(m.group(1), ""), text)


def load_prompt(name: str) -> str:
    """读取 prompt 模板原文。

    优先走 importlib.resources(兼容常规安装),失败时回退到源码目录
    (兼容 editable 安装的边界情况)。
    """
    try:
        return (resources.files("aquant.agents.prompts") / name).read_text("utf-8")
    except (FileNotFoundError, ModuleNotFoundError, NotImplementedError):
        return (Path(__file__).resolve().parent / name).read_text("utf-8")
