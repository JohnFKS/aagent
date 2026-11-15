"""Layer 4 指标排名与 NULL 基线辅助测试。"""

from aquant.verify.metrics import rank_candidates, render_ranking
from aquant.verify.pipeline import PAPER_CHECKLIST, _first_cs_oid, split_is_oos
import numpy as np
import pickle


def _summary(ann, sharpe, dd=-0.1):
    return {
        "annualized_returns": ann,
        "sharpe": sharpe,
        "max_drawdown": dd,
        "total_returns": ann * 1.2,
        "turnover": 1.5,
    }


class TestRankCandidates:
    def test_sorted_by_return_then_sharpe(self):
        ranked = rank_candidates({
            "甲": _summary(0.10, 1.0),
            "乙": _summary(0.25, 1.5),
            "丙": _summary(0.10, 1.8),
            "丁": _summary(-0.05, -0.2),
        })
        assert [r["name"] for r in ranked] == ["乙", "丙", "甲", "丁"]
        assert ranked[0]["rank"] == 1
        assert ranked[3]["rank"] == 4

    def test_none_values_rank_last(self):
        ranked = rank_candidates({"无指标": {}, "正常": _summary(0.1, 1.0)})
        assert ranked[0]["name"] == "正常"
        assert ranked[0]["annualized_returns"] == 0.1

    def test_render_table(self):
        ranked = rank_candidates({"乙": _summary(0.25, 1.5), "甲": _summary(0.10, 1.0)})
        text = render_ranking(ranked)
        assert "排名" in text and "乙" in text and "25.00%" in text


class TestPaperChecklist:
    def test_checklist_moved_to_pipeline_and_complete(self):
        """模拟盘清单归 pipeline(PAPER_CHECKLIST 职责归位),内容完整。"""
        assert len(PAPER_CHECKLIST) >= 6
        assert any("模拟盘" in x for x in PAPER_CHECKLIST)


class TestRandomBaselineHelpers:
    def test_first_cs_oid(self, tmp_path):
        instruments = [
            {"order_book_id": "IDX00001.XSHG", "type": "INDX"},
            {"order_book_id": "SYN00001.XSHG", "type": "CS"},
        ]
        with open(tmp_path / "instruments.pk", "wb") as f:
            pickle.dump(instruments, f)
        assert _first_cs_oid(tmp_path) == "SYN00001.XSHG"

    def test_split_needs_enough_days(self, tmp_path):
        dates = np.arange(20230103, 20230110, dtype=np.int64)  # 只有 5 天
        np.save(tmp_path / "trading_dates.npy", dates)
        import pytest

        with pytest.raises(ValueError):
            split_is_oos(tmp_path, "2023-01-03", "2023-01-09")
