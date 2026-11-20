"""MCP 工具入参与 token 配置的本地校验测试(不发网络请求)。"""
import httpx
import pytest

pytest.importorskip("mcp", reason="未安装 mcp SDK,跳过")
from aquant.mcp_server import server  # noqa: E402


def _block_network(monkeypatch):
    def handler(request):
        raise AssertionError("参数校验失败时不应发起 HTTP 请求")
    monkeypatch.setattr(server, "_TRANSPORT", httpx.MockTransport(handler))


def test_backtest_rejects_bad_date_before_http(monkeypatch):
    _block_network(monkeypatch)
    out = server.platform_run_backtest("def init(context): pass", "2024-13-01", "2024-12-31")
    assert "参数错误" in out and "YYYY-MM-DD" in out


def test_validate_requires_both_dates(monkeypatch):
    _block_network(monkeypatch)
    out = server.platform_validate_code("def init(context): pass", start="2024-01-01")
    assert "参数错误" in out and "同时提供" in out


def test_arena_rejects_duplicate_names(monkeypatch):
    _block_network(monkeypatch)
    code = "def init(context): pass\ndef handle_bar(context, bar_dict): pass"
    out = server.platform_arena(
        [{"name": "A", "code": code}, {"name": "A", "code": code}],
        "2024-01-01", "2024-12-31",
    )
    assert "参数错误" in out and "名称唯一" in out


def test_malformed_token_config_is_visible(monkeypatch):
    _block_network(monkeypatch)
    monkeypatch.setenv("AQUANT_MCP_TOKENS", "{not-json")
    out = server.platform_health()
    assert "MCP 配置错误" in out and "AQUANT_MCP_TOKENS" in out
