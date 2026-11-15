"""多策略竞技场测试:校验门禁、失败隔离、并行执行、排名正确性与 runner=None 分支。

stub runner 为 dict 形态(与 RealRunnerAdapter 归一化后的返回对齐);
runner=None 分支直连 run_backtest,需真实 bundle(/tmp/aquant-bundle-selftest
存在才执行,否则跳过——CI/无数据环境不受影响)。
"""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path

import pytest

from aquant.runner.backtest import BacktestExecutionError
from aquant.verify.arena import run_arena
from aquant.verify.metrics import render_ranking

START, END = "2024-01-02", "2024-06-28"
SELFTEST_BUNDLE = Path("/tmp/aquant-bundle-selftest")


def _good_strategy(marker: str) -> str:
    """合规策略模板(过 Layer 1 校验;内嵌 ARENA 标记供 stub 识别,不参与执行)。"""
    return (
        f"# ARENA:{marker}\n"
        "def init(context):\n"
        '    context.s1 = "000001.XSHE"\n'
        "\n"
        "def handle_bar(context, bar_dict):\n"
        "    prices = history_bars(context.s1, 20, '1d', 'close')\n"
        "    if prices is None or len(prices) < 20:\n"
        "        return\n"
        "    if prices[-5:].mean() > prices[-20:].mean():\n"
        "        order_target_percent(context.s1, 0.9)\n"
    )


# 含 .shift(-1) 未来函数的坏策略(必然被 Layer 1 拒绝)
BAD_SHIFT_CODE = (
    "def init(context):\n"
    '    context.s1 = "000001.XSHE"\n'
    "\n"
    "def handle_bar(context, bar_dict):\n"
    "    prices = history_bars(context.s1, 20, '1d', 'close')\n"
    "    tomorrow = prices.shift(-1)[0]\n"
    "    order_shares(context.s1, 100)\n"
)


class StubArenaRunner:
    """dict 形态 stub runner(签名对齐 ArenaRunner Protocol)。

    按源码内 `# ARENA:<n>` 标记返回固定指标(年化 = n/100);
    marker 命中 raise_marker 时抛 BacktestExecutionError(失败候选)。
    记录全部调用与并发峰值,供排名与并行断言。
    """

    def __init__(self, raise_marker: str | None = None, sleep: float = 0.0) -> None:
        self.calls: list[dict] = []
        self.raise_marker = raise_marker
        self.sleep = sleep
        self._lock = threading.Lock()
        self._active = 0
        self.max_concurrent = 0

    def run(self, code: str, start: str, end: str, **kw):
        marker = re.search(r"ARENA:(\w+)", code).group(1)
        with self._lock:
            self.calls.append({"code": code, "start": start, "end": end, "kw": kw})
            self._active += 1
            self.max_concurrent = max(self.max_concurrent, self._active)
        try:
            if marker == self.raise_marker:
                raise BacktestExecutionError(1, "子进程崩溃:stub 注入的回测失败")
            if self.sleep:
                time.sleep(self.sleep)
            ann = int(marker) / 100
            return {
                "summary": {
                    "annualized_returns": ann,
                    "sharpe": 1.0 + ann,
                    "max_drawdown": -0.08,
                    "total_returns": ann * 1.2,
                    "turnover": 1.5,
                },
                "verdict": {"passed": True, "error_count": 0, "warning_count": 0, "findings": []},
            }
        finally:
            with self._lock:
                self._active -= 1


class TestRunArenaRanking:
    def test_three_candidates_reject_and_rank_order(self):
        """3 候选:未来函数被拒(不执行),2 个合规候选按年化降序排名。"""
        runner = StubArenaRunner()
        out = run_arena(
            [
                {"name": "甲", "code": _good_strategy("10")},
                {"name": "乙", "code": _good_strategy("25")},
                {"name": "丙", "code": BAD_SHIFT_CODE},
            ],
            START,
            END,
            runner=runner,
        )
        assert out["count"] == 3
        assert out["ranked_count"] == 2
        assert out["start"] == START and out["end"] == END

        # 排名:乙(25%) > 甲(10%),按年化降序,附名次
        assert [r["name"] for r in out["ranking"]] == ["乙", "甲"]
        assert [r["rank"] for r in out["ranking"]] == [1, 2]
        anns = [r["annualized_returns"] for r in out["ranking"]]
        assert anns == sorted(anns, reverse=True)
        assert out["ranking"][0]["annualized_returns"] == 0.25

        # 被拒候选:rejected + verdict dict,且从未进入回测
        rejected = out["details"]["丙"]
        assert rejected["status"] == "rejected"
        assert rejected["verdict"]["passed"] is False
        assert any(f["rule"] == "ast.shift_negative" for f in rejected["verdict"]["findings"])
        assert all(call["code"] != BAD_SHIFT_CODE for call in runner.calls)

        # 合规候选:ranked + metrics
        assert out["details"]["甲"]["status"] == "ranked"
        assert out["details"]["甲"]["metrics"]["annualized_returns"] == 0.10

        # 排名表由 render_ranking 渲染
        assert out["table"] == render_ranking(out["ranking"])
        assert "乙" in out["table"]

    def test_runner_receives_no_bundle_path_kw(self):
        """runner 路径不透传 bundle_path(由 runner 自行处理,如 RealRunnerAdapter)。"""
        runner = StubArenaRunner()
        run_arena([{"name": "甲", "code": _good_strategy("10")}],
                  START, END, bundle_path="/tmp/whatever", runner=runner)
        assert len(runner.calls) == 1
        assert "bundle_path" not in runner.calls[0]["kw"]
        assert runner.calls[0]["start"] == START and runner.calls[0]["end"] == END


class TestRunArenaFailureIsolation:
    def test_failed_candidate_does_not_break_arena(self):
        """执行期异常候选标记 failed(附错误摘要),其余候选照常排名。"""
        runner = StubArenaRunner(raise_marker="13")
        out = run_arena(
            [
                {"name": "幸存者", "code": _good_strategy("20")},
                {"name": "崩溃者", "code": _good_strategy("13")},
            ],
            START,
            END,
            runner=runner,
        )
        assert out["ranked_count"] == 1
        assert out["ranking"][0]["name"] == "幸存者"

        failed = out["details"]["崩溃者"]
        assert failed["status"] == "failed"
        assert "BacktestExecutionError" in failed["error"]
        assert "崩溃" in failed["error"]
        # 失败者无 metrics;成功者无 error
        assert "metrics" not in failed
        assert "error" not in out["details"]["幸存者"]

    def test_all_candidates_failed_or_rejected_gives_empty_ranking(self):
        """全场被淘汰时排名为空但不抛错(table 只有表头)。"""
        runner = StubArenaRunner(raise_marker="10")
        out = run_arena(
            [
                {"name": "坏策略", "code": BAD_SHIFT_CODE},
                {"name": "崩溃者", "code": _good_strategy("10")},
            ],
            START,
            END,
            runner=runner,
        )
        assert out["ranked_count"] == 0
        assert out["ranking"] == []
        assert out["details"]["坏策略"]["status"] == "rejected"
        assert out["details"]["崩溃者"]["status"] == "failed"


class TestRunArenaParallel:
    def test_parallel_execution_observes_concurrency(self):
        """4 候选并行回测:全部排名成功,且观察到并发重叠(线程池生效)。"""
        runner = StubArenaRunner(sleep=0.15)
        cands = [{"name": f"候选{i}", "code": _good_strategy(str(10 + i))} for i in range(4)]
        out = run_arena(cands, START, END, runner=runner)
        assert out["ranked_count"] == 4
        assert len(runner.calls) == 4
        # 串行执行时并发峰值恒为 1;线程池并行下 4 个 sleep 任务必然重叠
        assert runner.max_concurrent >= 2
        # 排名按年化降序(候选3 年化 13% 最高)
        assert [r["name"] for r in out["ranking"]] == ["候选3", "候选2", "候选1", "候选0"]

    def test_single_candidate_still_runs(self):
        """单候选(线程池 max_workers=1)路径正常。"""
        out = run_arena([{"name": "独行侠", "code": _good_strategy("42")}],
                        START, END, runner=StubArenaRunner())
        assert out["ranked_count"] == 1
        assert out["ranking"][0]["annualized_returns"] == 0.42


class TestRunArenaInputGuards:
    def test_duplicate_names_raise_value_error(self):
        """库层防御:候选名重复直接 ValueError(API 层已提前映射 400)。"""
        with pytest.raises(ValueError, match="候选名重复"):
            run_arena(
                [{"name": "同名", "code": _good_strategy("10")},
                 {"name": "同名", "code": _good_strategy("20")}],
                START, END, runner=StubArenaRunner(),
            )

    def test_empty_candidates_raise_value_error(self):
        with pytest.raises(ValueError, match="至少需要 1 个候选"):
            run_arena([], START, END, runner=StubArenaRunner())


class TestRunArenaRealBacktest:
    def test_runner_none_uses_run_backtest(self):
        """runner=None 分支:延迟 import run_backtest 真实回测并排名。

        需要真实数据 bundle(/tmp/aquant-bundle-selftest);不存在则跳过,
        不影响无数据环境(与 runner 注入路径的测试互补)。
        """
        if not SELFTEST_BUNDLE.exists():
            pytest.skip("/tmp/aquant-bundle-selftest 不存在,跳过真实回测分支")
        demo = (Path(__file__).resolve().parents[2] / "strategies" / "demo_ma_cross.py").read_text(
            "utf-8"
        )
        out = run_arena(
            [{"name": "甲", "code": demo}, {"name": "乙", "code": demo}],
            "2023-01-03",
            "2024-06-28",
            bundle_path=str(SELFTEST_BUNDLE),
            runner=None,
        )
        assert out["ranked_count"] == 2
        assert out["start"] == "2023-01-03" and out["end"] == "2024-06-28"
        for name, entry in out["details"].items():
            assert entry["status"] == "ranked"
            assert "annualized_returns" in entry["metrics"]
            assert entry["verdict"]["passed"] is True  # BacktestResult.verdict → dict
        # 两个候选同码同区间:排名并列连续(1、2 名),表格含两名候选
        assert [r["rank"] for r in out["ranking"]] == [1, 2]
        assert all(name in out["table"] for name in ("甲", "乙"))
