"""/api/backtest 运行时崩溃透出测试:外部 agent 需拿到 execution_error 才能自纠。

真实场景:外部 agent 策略把 history_bars 单字段返回的一维 ndarray 当结构化数组
按字段名取列,静态校验通过、撮合阶段崩溃——响应必须带 execution_error 与修复指引。
"""

from __future__ import annotations

import pytest


def _crashing_runner():
    class _CrashRunner:
        """校验放行,但模拟 adapter 归一化的运行时崩溃形态。"""

        name = "crash_stub"

        def run(self, code, start, end, **kw):
            return {
                "summary": None,
                "portfolio": None,
                "trades": [],
                "verdict": None,
                "unsafe": False,
                "execution_error": (
                    "回测执行失败(exit=1):\n"
                    "IndexError: only integers, slices ... are valid indices\n"
                    "  line 29 in handle_bar: rsi = calculate_rsi(bars['close'], ...)"
                ),
            }

    return _CrashRunner()


def _client_with(crasher):
    from fastapi.testclient import TestClient

    from aquant.api.main import app

    app.state.runner = crasher
    return TestClient(app)


GOOD_ENOUGH_CODE = '''
def init(context):
    context.s1 = "000001.XSHE"


def handle_bar(context, bar_dict):
    prices = history_bars(context.s1, 20, "1d", "close")
    if prices is None or len(prices) < 5:
        return
    if prices[-1] > prices[-5:].mean():
        order_target_percent(context.s1, 0.9)
'''


def test_execution_error_surfaced_as_422_with_hint():
    client = _client_with(_crashing_runner())
    resp = client.post("/api/backtest", json={
        "code": GOOD_ENOUGH_CODE, "start": "2023-01-03", "end": "2024-06-28",
    })
    assert resp.status_code == 422
    d = resp.json()
    assert "回测执行阶段崩溃" in d["detail"]
    assert "一维 ndarray" in d["detail"]          # 修复指引在案
    assert "line 29 in handle_bar" in d["detail"]  # 原始错误末行随 detail 透出
    assert "IndexError" in d["execution_error"]   # 原始错误可读


def test_no_data_error_mapped_to_readable_range_hint():
    """区间越界(rqalpha There is no data)→ 给出调整日期的具体指引而非通用崩溃文案。"""

    class _NoDataRunner:
        name = "no_data_stub"

        def run(self, code, start, end, **kw):
            return {
                "summary": None,
                "portfolio": None,
                "trades": [],
                "verdict": None,
                "unsafe": False,
                "execution_error": (
                    "回测执行失败(exit=1):\n"
                    "rqalpha.utils.exception.CustomException: There is no data between "
                    "2025-01-01 and 2025-06-30. Please check your data bundle."
                ),
            }

    client = _client_with(_NoDataRunner())
    resp = client.post("/api/backtest", json={
        "code": GOOD_ENOUGH_CODE, "start": "2025-01-01", "end": "2025-06-30",
    })
    assert resp.status_code == 422
    d = resp.json()
    assert "回测区间 2025-01-01 ~ 2025-06-30" in d["detail"]
    assert "无行情数据" in d["detail"]


def test_normal_run_has_no_execution_error_key():
    # 正常 runner(不崩溃)时响应不应包含 execution_error 字段
    class _OkRunner:
        name = "ok_stub"

        def run(self, code, start, end, **kw):
            return {"summary": {"total_returns": 0.1}, "portfolio": None,
                    "trades": [], "verdict": None, "unsafe": False}

    client2 = _client_with(_OkRunner())
    resp = client2.post("/api/backtest", json={
        "code": GOOD_ENOUGH_CODE, "start": "2023-01-03", "end": "2024-06-28",
    })
    assert resp.status_code == 200
    assert "execution_error" not in resp.json()
