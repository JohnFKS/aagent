"""tests/mcp 共享配置:把 tests/agents 目录加入 sys.path 以复用 stubs 模块。

与 tests/api/conftest.py 同一套约定:tests 目录不带 __init__.py
(pytest rootdir 插入模式),跨目录导入通过路径注入完成。
"""

import sys
from pathlib import Path

import pytest

_AGENTS_DIR = Path(__file__).resolve().parent.parent / "agents"
if str(_AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENTS_DIR))


@pytest.fixture(autouse=True)
def _mcp_proxy_mode(monkeypatch):
    """既有 MCP 测试基于 HTTP 薄桥(MockTransport):强制 proxy 模式。

    direct 模式(默认)不经 HTTP,单独在 test_mcp_direct.py 覆盖。
    """
    monkeypatch.setenv("AQUANT_MCP_MODE", "proxy")
