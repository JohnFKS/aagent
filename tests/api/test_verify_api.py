"""POST /api/verify(分层验证端点)与 /api/backtest overrides(手动模式)测试。

- bundle:/tmp/aquant-bundle-selftest 存在则复用,否则 build_bundle +
  SyntheticSource 现造(与 tests/api/test_kline.py 同法);分层验证要求区间
  ≥40 个交易日,两种来源均覆盖 2022~2024,用例区间取 2023-01-03~2024-06-28;
- /api/verify:合规策略 200(status ∈ pass/warning/fail,report 含"模拟盘",
  checks 非空);is_ratio 越界 422;坏策略(shift(-1))→ 422 + verdict;
  overrides 保守(slippage 0.005)→ 200 / 激进(slippage 0.0)→ 422 且
  findings 含 cfg.zero_slippage;bundle 缺失(monkeypatch default_bundle_path)
  → 400 + BUNDLE_GUIDANCE;
- /api/backtest overrides(手动模式):保守 → 200;激进 → 422
  (Layer 1 校验是硬门禁,激进值在执行前被拦,零新逻辑)。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aquant.api import main as api_main
from aquant.api.main import BUNDLE_GUIDANCE, app
from aquant.data.bundle import build_bundle
from aquant.data.synthetic_source import SyntheticSource

# 分层验证用例区间(两种 bundle 来源都完整覆盖;约 360 个交易日 ≥ 40)
START, END = "2023-01-03", "2024-06-28"

_SELFTEST_BUNDLE = Path("/tmp/aquant-bundle-selftest")
_STRATEGIES_DIR = Path(__file__).resolve().parents[2] / "strategies"


@pytest.fixture(scope="module")
def bundle(tmp_path_factory) -> Path:
    """优先复用 /tmp/aquant-bundle-selftest;否则现造合成 bundle(区间足够长)。"""
    if _SELFTEST_BUNDLE.exists():
        return _SELFTEST_BUNDLE
    src = SyntheticSource(n_instruments=2, start="2022-01-03", end="2024-12-31", seed=42)
    return build_bundle([src], tmp_path_factory.mktemp("verify-bundle"))


@pytest.fixture()
def bundle_env(bundle: Path, monkeypatch) -> Path:
    """把 AQUANT_BUNDLE_PATH 指到该 bundle(default_bundle_path 每请求读环境变量)。"""
    monkeypatch.setenv("AQUANT_BUNDLE_PATH", str(bundle))
    return bundle


@pytest.fixture()
def client():
    """真实 runner 路径(verify/backtest 均不走 llm);用例前后恢复默认依赖。"""
    app.state.llm = None
    app.state.runner = None
    with TestClient(app) as c:
        yield c
    app.state.llm = None
    app.state.runner = None


@pytest.fixture(scope="module")
def good_code() -> str:
    """合规示例策略(双均线择时,与 CLI/e2e 同一策略)。"""
    return (_STRATEGIES_DIR / "demo_ma_cross.py").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def bad_code() -> str:
    """未来函数坏策略(.shift(-1)),Layer 1 以 ERROR 拦截。"""
    return (_STRATEGIES_DIR / "bad_examples" / "lookahead_shift.py").read_text(encoding="utf-8")


class TestVerifyOk:
    def test_good_strategy_200_full_payload(self, client, bundle_env, good_code):
        """合规策略 → 200:status 合法、report 含"模拟盘"、checks 非空、结构完整。"""
        resp = client.post(
            "/api/verify",
            json={"code": good_code, "start": START, "end": END},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] in {"pass", "warning", "fail"}
        assert body["status"] == body["comparison"]["status"]
        # 分屏对比三件套(顶层与 comparison 内一致)
        assert body["checks"] == body["comparison"]["checks"]
        assert body["is_metrics"] == body["comparison"]["is_metrics"]
        assert body["oos_metrics"] == body["comparison"]["oos_metrics"]
        assert len(body["checks"]) > 0
        # 样本内外切分点落在区间内
        assert START < body["is_split"] <= END
        # 第三层验证:模拟盘引导清单与随机基准
        assert len(body["paper_checklist"]) >= 6
        assert isinstance(body["random_baseline_returns"], float)
        # 人类可读报告:分层验证表头 + 模拟盘清单
        assert "分层验证报告" in body["report"]
        assert "模拟盘" in body["report"]
        assert body["start"] == START and body["end"] == END

    def test_cash_and_benchmark_forwarded(self, client, bundle_env, good_code):
        """cash/benchmark 可选参数:提供时仍 200(透传给 run_backtest)。"""
        resp = client.post(
            "/api/verify",
            json={
                "code": good_code,
                "start": START,
                "end": END,
                "cash": 500000,
                # 000001.XSHG 基准:两种 bundle 来源(自测/合成)的 indexes.h5 均恒有
                "benchmark": "000001.XSHG",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["status"] in {"pass", "warning", "fail"}


class TestVerifyRequestValidation:
    @pytest.mark.parametrize("ratio", [0.3, 0.95])
    def test_is_ratio_out_of_range_422(self, client, good_code, ratio):
        """is_ratio 越界(<0.5 或 >0.9)→ Pydantic 422,不进入验证管线。"""
        resp = client.post(
            "/api/verify",
            json={"code": good_code, "start": START, "end": END, "is_ratio": ratio},
        )
        assert resp.status_code == 422

    def test_is_ratio_boundary_ok(self, client, bundle_env, good_code):
        """边界值 0.5 合法(进入管线并 200)。"""
        resp = client.post(
            "/api/verify",
            json={"code": good_code, "start": START, "end": END, "is_ratio": 0.5},
        )
        assert resp.status_code == 200

    def test_missing_code_422(self, client):
        resp = client.post("/api/verify", json={"start": START, "end": END})
        assert resp.status_code == 422


class TestVerifyBadStrategy:
    def test_lookahead_strategy_422_with_verdict(self, client, bundle_env, bad_code):
        """未来函数策略 → 422 + verdict dict(ast.shift_negative),与 /api/backtest 同语义。"""
        resp = client.post(
            "/api/verify",
            json={"code": bad_code, "start": START, "end": END},
        )
        assert resp.status_code == 422
        body = resp.json()
        assert "校验未通过" in body["detail"]
        assert body["verdict"]["passed"] is False
        assert any(f["rule"] == "ast.shift_negative" for f in body["verdict"]["findings"])


class TestVerifyOverrides:
    def test_conservative_overrides_200(self, client, bundle_env, good_code):
        """更保守的滑点(0.005 > 平台默认 0.002)→ 200,Layer 1 放行。"""
        resp = client.post(
            "/api/verify",
            json={
                "code": good_code,
                "start": START,
                "end": END,
                "overrides": {"sys_simulation": {"slippage": 0.005}},
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] in {"pass", "warning", "fail"}
        assert "模拟盘" in body["report"]

    def test_aggressive_overrides_422(self, client, bundle_env, good_code):
        """零滑点(激进)→ 422,findings 含 cfg.zero_slippage。"""
        resp = client.post(
            "/api/verify",
            json={
                "code": good_code,
                "start": START,
                "end": END,
                "overrides": {"sys_simulation": {"slippage": 0.0}},
            },
        )
        assert resp.status_code == 422
        findings = resp.json()["verdict"]["findings"]
        assert any(f["rule"] == "cfg.zero_slippage" for f in findings)


class TestVerifyBundleMissing:
    def test_missing_bundle_400_guidance(self, client, monkeypatch, good_code):
        """bundle 缺失(monkeypatch default_bundle_path)→ 400 + BUNDLE_GUIDANCE。"""
        monkeypatch.setattr(
            api_main, "default_bundle_path", lambda: Path("/nonexistent/verify-bundle")
        )
        resp = client.post(
            "/api/verify",
            json={"code": good_code, "start": START, "end": END},
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["detail"] == BUNDLE_GUIDANCE
        assert "bundle" in body["reason"]


class TestBacktestOverrides:
    """/api/backtest 手动模式:用户自选更保守的回测参数(激进值 Layer 1 拒绝)。"""

    def test_conservative_overrides_200(self, client, bundle_env, good_code):
        """更保守的滑点 → 200 + 指标(overrides 透传到真实回测)。"""
        resp = client.post(
            "/api/backtest",
            json={
                "code": good_code,
                "start": START,
                "end": END,
                "overrides": {"sys_simulation": {"slippage": 0.005}},
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["passed"] is True
        assert body["verdict"]["passed"] is True
        metrics = body["metrics"]
        assert metrics["start"] == START and metrics["end"] == END
        assert metrics["trading_days"] and metrics["trading_days"] > 100

    def test_aggressive_overrides_422_with_finding(self, client, bundle_env, good_code):
        """零滑点(激进)→ 422,findings 含 cfg.zero_slippage(执行前被拦)。"""
        resp = client.post(
            "/api/backtest",
            json={
                "code": good_code,
                "start": START,
                "end": END,
                "overrides": {"sys_simulation": {"slippage": 0.0}},
            },
        )
        assert resp.status_code == 422
        body = resp.json()
        assert "校验未通过" in body["detail"]
        assert any(f["rule"] == "cfg.zero_slippage" for f in body["verdict"]["findings"])

    def test_without_overrides_still_200(self, client, bundle_env, good_code):
        """不带 overrides:现状行为回归(默认保守参数照常回测)。"""
        resp = client.post(
            "/api/backtest",
            json={"code": good_code, "start": START, "end": END},
        )
        assert resp.status_code == 200
        assert resp.json()["passed"] is True
