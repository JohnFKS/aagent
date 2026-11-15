"""端到端集成测试:四层校验全链路。

链路:SyntheticSource → build_bundle(Layer 2) → run_backtest(Layer 1 门禁 + Layer 3 注入)
     → layered_verification(Layer 4)
"""

from pathlib import Path

import pandas as pd
import pytest

from aquant.runner.backtest import (
    BacktestExecutionError,
    StrategyValidationError,
    key_metrics,
    run_backtest,
)
from aquant.verify.pipeline import layered_verification
from aquant.validation.findings import RuleId

DEMO = Path(__file__).resolve().parents[1] / "strategies" / "demo_ma_cross.py"
BAD_SHIFT = (
    Path(__file__).resolve().parents[1] / "strategies" / "bad_examples" / "lookahead_shift.py"
)


@pytest.fixture(scope="module")
def bundle(tmp_path_factory) -> Path:
    from aquant.data.bundle import build_bundle
    from aquant.data.synthetic_source import SyntheticSource

    out = tmp_path_factory.mktemp("bundle")
    src = SyntheticSource(n_instruments=3, start="2022-01-04", end="2024-12-31", seed=42)
    return build_bundle([src], out)


@pytest.fixture(scope="module")
def demo_code() -> str:
    return DEMO.read_text(encoding="utf-8")


class TestE2E:
    def test_good_strategy_backtest_runs(self, bundle, demo_code):
        """合规策略在合成 bundle 上真实跑通,成本与保守参数生效。"""
        result = run_backtest(demo_code, "2023-01-03", "2024-06-28", bundle_path=bundle)
        assert not result.verdict.has_error
        m = key_metrics(result)
        assert m["trading_days"] and m["trading_days"] > 100
        # 净值序列存在
        assert len(result.portfolio) > 100

    def test_cost_applied(self, bundle, demo_code):
        """手续费/印花税必然生效:策略发生交易时,成本 > 0(从成交明细聚合)。"""
        result = run_backtest(demo_code, "2023-01-03", "2024-06-28", bundle_path=bundle)
        assert len(result.trades) > 0
        assert isinstance(result.trades, pd.DataFrame)  # 6.5.0: 成交明细为 DataFrame
        commission = float(result.trades["commission"].sum())
        tax = float(result.trades["tax"].sum())
        assert commission > 0  # 万八佣金 + 最低 5 元
        assert tax >= 0
        m = key_metrics(result)
        assert m["total_commission"] == pytest.approx(commission, abs=0.01)
        assert m["trading_days"] > 100

    def test_lookahead_strategy_rejected_before_run(self, bundle):
        """未来函数策略在校验层被拒,绝不进入回测。"""
        code = BAD_SHIFT.read_text(encoding="utf-8")
        with pytest.raises(StrategyValidationError) as exc:
            run_backtest(code, "2023-01-03", "2024-06-28", bundle_path=bundle)
        assert any(f.rule == RuleId.AST_SHIFT_NEGATIVE for f in exc.value.verdict.findings)

    def test_aggressive_override_rejected(self, bundle, demo_code):
        """用户试图注入零滑点 → 拒绝执行。"""
        with pytest.raises(StrategyValidationError):
            run_backtest(
                demo_code, "2023-01-03", "2024-06-28", bundle_path=bundle,
                mod_overrides={"sys_simulation": {"slippage": 0.0}},
            )

    def test_layered_verification(self, bundle, demo_code):
        """Layer 4:IS→OOS 两段回测 + 对比报告生成。"""
        lv = layered_verification(demo_code, "2022-01-04", "2024-12-31", bundle_path=bundle)
        assert lv.is_split > "2022-01-04"
        assert lv.comparison["status"] in {"pass", "warning", "fail"}
        assert len(lv.paper_checklist) >= 6
        report = lv.to_report()
        assert "样本内 IS" in report and "样本外 OOS" in report and "模拟盘" in report
