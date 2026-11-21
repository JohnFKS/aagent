"""MCP direct 模式测试:进程内直调 aquant 服务(无 REST 服务、无平台 LLM)。

覆盖:
1. service 层分发:health / validate / 未知端点 / token 闸门 / LLM 门禁;
2. direct 模式工具:契约、标的池、校验(含 findings 可读文本)、排名;
3. direct 模式真实回测(合成 bundle,走完整 rqalpha 子进程 + 报告生成);
4. proxy/direct 隔离:conftest 的 proxy 门禁不影响本文件(显式 direct)。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from aquant.mcp_server import server, service


BAD_CODE = """
import pandas as pd

def init(context):
    context.s1 = "000001.XSHE"

def handle_bar(context, bar_dict):
    prices = history_bars(context.s1, 20, "1d", "close")
    prices.shift(-1)  # 未来函数
"""

GOOD_CODE = """
from rqalpha.const import POSITION_DIRECTION

def init(context):
    context.s1 = "000001.XSHE"
    context.fast = 5
    context.slow = 20

def handle_bar(context, bar_dict):
    prices = history_bars(context.s1, context.slow + 2, "1d", "close")
    if prices is None or len(prices) < context.slow:
        return
    ma_fast = prices[-context.fast:].mean()
    ma_slow = prices[-context.slow:].mean()
    pos = context.portfolio.get_position(context.s1, POSITION_DIRECTION.LONG)
    if ma_fast > ma_slow and pos.quantity == 0:
        order_target_percent(context.s1, 0.95)
    elif ma_fast < ma_slow and pos.quantity > 0:
        order_target_percent(context.s1, 0.0)
"""


@pytest.fixture()
def direct_mode(monkeypatch):
    monkeypatch.setenv("AQUANT_MCP_MODE", "direct")


@pytest.fixture()
def synth_bundle(tmp_path, monkeypatch):
    """合成 bundle(无真实构建哨兵 → 自动扩展不触发)+ AQUANT_BUNDLE_PATH 指向它。"""
    from aquant.data.bundle import build_bundle
    from aquant.data.synthetic_source import SyntheticSource

    src = SyntheticSource(n_instruments=3, start="2023-01-03", end="2023-03-31", seed=5)
    out = build_bundle([src], tmp_path / "bundle")
    monkeypatch.setenv("AQUANT_BUNDLE_PATH", str(out))
    return out


# ---------------------------------------------------------------------------
# service 层分发
# ---------------------------------------------------------------------------

class TestServiceDispatch:
    def test_health(self, direct_mode):
        status, data = service.service_request("GET", "/api/health")
        assert status == 200 and data["status"] == "ok"

    def test_validate_rejects_future_function(self, direct_mode):
        status, data = service.service_request("POST", "/api/validate", {"code": BAD_CODE})
        assert status == 200
        assert data["passed"] is False
        rules = [f["rule"] for f in data["findings"]]
        assert "ast.shift_negative" in rules

    def test_unknown_endpoint_404(self, direct_mode):
        status, data = service.service_request("GET", "/api/nothing")
        assert status == 404

    def test_llm_gated_endpoint_503(self, direct_mode, monkeypatch):
        monkeypatch.delenv("AQUANT_LLM_BASE_URL", raising=False)
        monkeypatch.delenv("AQUANT_LLM_API_KEY", raising=False)
        monkeypatch.delenv("AQUANT_LLM_MODEL", raising=False)
        status, data = service.service_request("POST", "/api/optimize", {"code": GOOD_CODE})
        assert status == 503
        assert "自主闭环" in data["detail"] and "platform_strategy_contract" in data["detail"]

    def test_token_gate_readonly_forbidden(self, direct_mode, monkeypatch):
        monkeypatch.setenv("AQUANT_MCP_TOKEN", "tkn_ro")
        monkeypatch.setenv("AQUANT_MCP_TOKENS", '{"tkn_ro": {"scope": "readonly"}}')
        status, data = service.service_request(
            "POST", "/api/backtest", {"code": GOOD_CODE, "start": "2023-01-03", "end": "2023-02-01"}
        )
        assert status == 403
        monkeypatch.delenv("AQUANT_MCP_TOKEN")
        monkeypatch.delenv("AQUANT_MCP_TOKENS")


# ---------------------------------------------------------------------------
# direct 模式工具
# ---------------------------------------------------------------------------

class TestDirectTools:
    def test_strategy_contract_contains_rules_and_pool(self, direct_mode):
        text = server.platform_strategy_contract()
        assert "init(context)" in text and "handle_bar" in text
        assert "history_bars" in text and "POSITION_DIRECTION" in text
        assert "shift(-n)" in text or "shift(-1)" in text
        assert "标的池" in text  # 附动态标的池段落

    def test_validate_code_direct_returns_findings_text(self, direct_mode):
        text = server.platform_validate_code(BAD_CODE)
        assert "校验未通过" in text and "ast.shift_negative" in text

    def test_rank_strategies_local(self, direct_mode):
        text = server.platform_rank_strategies([
            {"name": "甲", "metrics": {"sharpe": 1.5, "annualized_returns": 0.2, "max_drawdown": 0.1}},
            {"name": "乙", "metrics": {"sharpe": 0.8, "annualized_returns": 0.3, "max_drawdown": 0.4}},
        ])
        assert "最优候选" in text and "甲" in text

    def test_list_instruments_with_synth_bundle(self, direct_mode, synth_bundle):
        text = server.platform_list_instruments()
        assert "数据区间" in text and "2023-01-03" in text

    def test_run_backtest_direct_real_run(self, direct_mode, synth_bundle):
        """direct 模式真实回测:走完整 rqalpha 子进程,输出 JSON + 人读报告。"""
        text = server.platform_run_backtest(GOOD_CODE, "2023-01-10", "2023-02-28")
        json_part, _, report_part = text.partition("\n\n---\n\n")
        parsed = json.loads(json_part)
        assert parsed["passed"] is True
        assert parsed["benchmark"] == "000001.XSHG"  # 默认基准
        assert parsed["benchmark_label"] == "上证指数"
        assert isinstance(parsed["portfolio_series"], list)
        assert "回测报告" in report_part and "核心指标" in report_part

    def test_run_backtest_direct_benchmark_none(self, direct_mode, synth_bundle):
        text = server.platform_run_backtest(GOOD_CODE, "2023-01-10", "2023-02-28", benchmark="none")
        parsed = json.loads(text.partition("\n\n---\n\n")[0])
        assert parsed["benchmark"] is None
        assert parsed["benchmark_series"] == []

    def test_run_backtest_direct_rejected_422(self, direct_mode, synth_bundle):
        text = server.platform_run_backtest(BAD_CODE, "2023-01-10", "2023-02-28")
        assert "回测被校验拦截" in text and "ast.shift_negative" in text

    def test_diagnose_strategy_direct(self, direct_mode, synth_bundle):
        text = server.platform_diagnose_strategy(GOOD_CODE, "2023-01-10", "2023-02-28")
        assert "回测诊断" in text
        assert ("改进线索" in text) or ("未命中任何规则线索" in text)


# ---------------------------------------------------------------------------
# 工具注册
# ---------------------------------------------------------------------------

def test_new_tools_registered():
    tools = asyncio.run(server.mcp.list_tools())
    names = {t.name for t in tools}
    assert {"platform_strategy_contract", "platform_list_instruments",
            "platform_diagnose_strategy", "platform_rank_strategies"} <= names
