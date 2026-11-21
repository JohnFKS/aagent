"""/api/backtest 收益图序列测试:响应必须携带策略净值与基准净值序列。

背景:回测界面要求收益图(策略净值曲线 + A股基准对照线)。runner 已返回
benchmark_portfolio(sys_analyser 配置基准时输出),API 层把它抽取为
benchmark_series 并与 portfolio_series 一起返回,同时回传解析后的 benchmark
参数与图例名称。
"""

from __future__ import annotations

import pandas as pd
import pytest


def _ok_runner():
    class _OkRunner:
        name = "series_stub"

        def run(self, code, start, end, **kw):
            # 记录 kwargs,便于断言 benchmark 透传
            self.last_kw = kw
            idx = pd.DatetimeIndex(pd.to_datetime(
                ["2023-01-03", "2023-01-04", "2023-01-05"]))
            portfolio = pd.DataFrame(
                {"total_value": [1_000_000.0, 1_010_000.0, 1_020_000.0]}, index=idx)
            # 与真实 runner 同语义:配置了基准才输出 benchmark_portfolio
            bm = pd.DataFrame({"unit_net_value": [1.0, 1.005, 0.998]}, index=idx) \
                if kw.get("benchmark") else pd.DataFrame()
            return {
                "summary": {"total_returns": 0.02, "benchmark_total_returns": -0.002},
                "portfolio": portfolio,
                "benchmark_portfolio": bm,
                "trades": [],
                "verdict": None,
                "unsafe": False,
            }

    return _OkRunner()


GOOD_CODE = '''
def init(context):
    context.s1 = "000001.XSHE"


def handle_bar(context, bar_dict):
    prices = history_bars(context.s1, 20, "1d", "close")
    if prices is None or len(prices) < 5:
        return
    if prices[-1] > prices[-5:].mean():
        order_target_percent(context.s1, 0.9)
'''


def _client_with(runner):
    from fastapi.testclient import TestClient

    from aquant.api.main import app

    app.state.runner = runner
    return TestClient(app)


@pytest.fixture()
def client():
    runner = _ok_runner()
    return runner, _client_with(runner)


def test_backtest_returns_series_with_default_benchmark(client):
    runner, tc = client
    resp = tc.post("/api/backtest", json={
        "code": GOOD_CODE, "start": "2023-01-03", "end": "2023-01-05",
    })
    assert resp.status_code == 200
    d = resp.json()
    # 策略净值序列:归一化首日 = 1.0,含 date/net_value
    assert d["portfolio_series"] == [
        {"date": "2023-01-03", "net_value": 1.0},
        {"date": "2023-01-04", "net_value": 1.01},
        {"date": "2023-01-05", "net_value": 1.02},
    ]
    # 基准净值序列(未请求基准时默认上证指数 000001.XSHG)
    assert d["benchmark"] == "000001.XSHG"
    assert d["benchmark_label"] == "上证指数"
    assert d["benchmark_series"][0] == {"date": "2023-01-03", "net_value": 1.0}
    assert d["benchmark_series"][-1]["net_value"] == pytest.approx(0.998, abs=1e-6)
    # benchmark 透传给 runner
    assert runner.last_kw.get("benchmark") == "000001.XSHG"


def test_backtest_benchmark_explicit_and_none(client):
    runner, tc = client
    # 显式沪深300:原样透传 + 图例名
    resp = tc.post("/api/backtest", json={
        "code": GOOD_CODE, "start": "2023-01-03", "end": "2023-01-05",
        "benchmark": "000300.XSHG",
    })
    assert resp.status_code == 200
    d = resp.json()
    assert d["benchmark"] == "000300.XSHG"
    assert d["benchmark_label"] == "沪深300"
    assert runner.last_kw.get("benchmark") == "000300.XSHG"

    # 'none':不设基准,基准序列为空
    resp = tc.post("/api/backtest", json={
        "code": GOOD_CODE, "start": "2023-01-03", "end": "2023-01-05",
        "benchmark": "none",
    })
    assert resp.status_code == 200
    d = resp.json()
    assert d["benchmark"] is None
    assert d["benchmark_label"] == ""
    assert d["benchmark_series"] == []
    assert runner.last_kw.get("benchmark", None) is None


def test_backtest_without_benchmark_data_has_empty_series():
    """runner 未返回 benchmark_portfolio(未配置基准)时序列为空列表,不抛错。"""

    class _NoBmRunner:
        name = "no_bm_stub"

        def run(self, code, start, end, **kw):
            idx = pd.DatetimeIndex(pd.to_datetime(["2023-01-03"]))
            return {
                "summary": {"total_returns": 0.0},
                "portfolio": pd.DataFrame({"total_value": [1_000_000.0]}, index=idx),
                "benchmark_portfolio": pd.DataFrame(),
                "trades": [],
                "verdict": None,
                "unsafe": False,
            }

    tc = _client_with(_NoBmRunner())
    resp = tc.post("/api/backtest", json={
        "code": GOOD_CODE, "start": "2023-01-03", "end": "2023-01-03",
    })
    assert resp.status_code == 200
    d = resp.json()
    assert d["portfolio_series"] != []
    assert d["benchmark_series"] == []
