"""经验库闭环测试:标签生成、store 落盘/检索/容错、编辑器注入、编排器记录。

覆盖 docs/design.md §1.2 采纳项"成功/失败案例经验库按特征检索注入":
- derive_tags:关键词标签词表(≥6 类)+ 标的代码标签 + ASCII 词边界防误命中;
- ExperienceStore:record 落盘 → search 按重叠数 top-k(ts 新者优先)、损坏文件跳过;
- StrategyEditorAgent:注入 store 后 user 消息含案例段落,系统提示词与重试/防作弊语义不变;
- run_conversation:成功/失败均落盘一条,记录异常只 warn 不阻塞主管线。
"""

import json
from datetime import datetime
from pathlib import Path

import pytest

from aquant.agents.experience import ExperienceStore, derive_tags
from aquant.agents.experience.store import DEFAULT_EXPERIENCE_DIR, MAX_CODE_LINES
from aquant.agents.llm import DeterministicLLM
from aquant.agents.orchestrator import run_conversation
from aquant.agents.prompts import render_prompt
from aquant.agents.strategy_editor import (
    ANTI_CHEAT_CLAUSE,
    CASES_HEADING,
    StrategyEditorAgent,
)
from aquant.validation.validator import validate_strategy
from stubs import BAD_CODE, FENCED_BAD_CODE, GOOD_CODE, ConstantLLM, ScriptedLLM, StubRunner

START, END = "2024-01-02", "2024-06-28"


class TestDeriveTags:
    """标签生成词表覆盖(关键词 ≥6 类 + 标的代码 + 词边界)。"""

    def test_ma_chinese_keyword(self):
        assert derive_tags("写一个双均线金叉策略") == ["均线"]

    def test_ma_english_word(self):
        assert "均线" in derive_tags("MA cross strategy")

    def test_ascii_boundary_blocks_false_positive(self):
        # "markdown"/"macd" 内部的 ma 不命中均线;macd 只给 macd 标签
        assert derive_tags("markdown 策略") == []
        assert derive_tags("MACD 策略") == ["macd"]
        # 中文紧邻的 ASCII 关键词可命中(词边界只看字母数字)
        assert derive_tags("低估值pe选股") == ["价值"]

    def test_macd_with_golden_cross(self):
        tags = derive_tags("MACD 金叉买入")
        assert "macd" in tags and "均线" in tags  # 金叉归入均线族

    def test_rsi_keyword(self):
        assert derive_tags("RSI 超卖反弹") == ["rsi"]

    def test_value_pe_keyword(self):
        assert "价值" in derive_tags("低估值 PE 选股,基本面稳健")

    def test_etf_keyword(self):
        assert derive_tags("ETF 网格轮动") == ["etf"]

    def test_limit_up_keyword(self):
        assert "打板" in derive_tags("打板涨停龙头战法")

    def test_stop_loss_and_position(self):
        assert derive_tags("严格止损") == ["止损"]
        assert "仓位" in derive_tags("目标仓位 60%")

    def test_dividend_keyword(self):
        assert derive_tags("高分红股息策略") == ["分红"]

    def test_instrument_code_extracted_from_code_only(self):
        # 需求不含标的 → 无标的标签;code 中的 order_book_id 提取为标签
        assert derive_tags("写一个双均线策略") == ["均线"]
        tags = derive_tags("写一个双均线策略", 'context.s1 = "000001.XSHE"')
        assert tags == ["均线", "000001.XSHE"]
        tags_two = derive_tags("双标的轮动", 'a = "600519.XSHG"\nb = "000001.XSHE"')
        assert set(tags_two) == {"000001.XSHE", "600519.XSHG"}

    def test_no_match_returns_empty(self):
        assert derive_tags("写一个偷看明天收盘价的策略") == []


class TestExperienceStore:
    """store 落盘、检索打分与容错。"""

    def _entry(self, requirement="双均线策略", tags=("均线",), passed=True, **kw):
        entry = {"requirement": requirement, "code": GOOD_CODE,
                 "passed": passed, "metrics": {"sharpe": 1.1}, "tags": list(tags)}
        entry.update(kw)
        return entry

    def test_record_writes_json_with_all_fields(self, tmp_path):
        store = ExperienceStore(tmp_path)
        path = store.record(self._entry())
        assert path.parent == tmp_path and path.suffix == ".json"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert set(data) >= {"ts", "requirement", "code", "passed", "metrics", "tags"}
        datetime.fromisoformat(data["ts"])  # ts 为合法 ISO 时间
        assert data["requirement"] == "双均线策略"
        assert data["passed"] is True
        assert data["tags"] == ["均线"]

    def test_roundtrip_record_then_search_hit(self, tmp_path):
        store = ExperienceStore(tmp_path)
        store.record(self._entry(requirement="双均线金叉", tags=["均线", "止损"]))
        # 同目录新实例(模拟跨会话)检索命中
        hit = ExperienceStore(tmp_path).search(["均线"])
        assert len(hit) == 1
        assert hit[0]["requirement"] == "双均线金叉"
        assert hit[0]["code"] == GOOD_CODE
        # 无重叠标签不返回;空查询直接返回空
        assert ExperienceStore(tmp_path).search(["rsi"]) == []
        assert ExperienceStore(tmp_path).search([]) == []

    def test_search_orders_by_overlap_then_recency(self, tmp_path):
        store = ExperienceStore(tmp_path)
        store.record(self._entry(requirement="A", tags=["均线", "止损"]))
        store.record(self._entry(requirement="B", tags=["均线"], ts="2026-02-01T00:00:00"))
        store.record(self._entry(requirement="C", tags=["均线"], ts="2026-03-01T00:00:00"))
        hits = store.search(["均线", "止损"])
        # A 重叠数 2 排第一;B/C 同分按 ts 新者优先
        assert [h["requirement"] for h in hits] == ["A", "C", "B"]
        # top_k 截断
        assert [h["requirement"] for h in store.search(["均线"], top_k=2)] == ["A", "C"]

    def test_corrupt_and_non_dict_files_skipped(self, tmp_path):
        store = ExperienceStore(tmp_path)
        store.record(self._entry(requirement="好案例"))
        (tmp_path / "broken.json").write_text("{not-json", encoding="utf-8")
        (tmp_path / "alist.json").write_text("[1, 2, 3]", encoding="utf-8")
        (tmp_path / "note.txt").write_text("ignored", encoding="utf-8")
        hits = store.search(["均线"])
        assert len(hits) == 1 and hits[0]["requirement"] == "好案例"
        # 损坏文件不影响继续落盘
        store.record(self._entry(requirement="第二个案例"))
        assert len(store.search(["均线"])) == 2

    def test_dir_autocreated(self, tmp_path):
        nested = tmp_path / "a" / "b" / "experience"
        ExperienceStore(nested).record(self._entry())
        assert nested.is_dir() and list(nested.glob("*.json"))

    def test_default_dir_constant(self):
        assert DEFAULT_EXPERIENCE_DIR == Path.home() / ".aquant" / "experience"
        assert MAX_CODE_LINES == 40


class TestEditorExperienceInjection:
    """编辑器注入:案例段落进 user 消息,系统提示词与重试/防作弊语义不变。"""

    def _store_with_case(self, tmp_path, code=GOOD_CODE, requirement="双均线金叉策略,止损 8%"):
        store = ExperienceStore(tmp_path)
        store.record({
            "requirement": requirement, "code": code, "passed": True,
            "metrics": {"sharpe": 1.2}, "tags": ["均线", "止损", "000001.XSHE"],
        })
        return store

    def test_user_message_contains_cases_section(self, tmp_path):
        store = self._store_with_case(tmp_path)
        llm = ScriptedLLM([GOOD_CODE])
        editor = StrategyEditorAgent(llm, validate_strategy, experience=store)
        assert editor.generate("写一个双均线策略") == GOOD_CODE.strip()  # 提取器会 strip

        system, user = llm.calls[0]
        assert CASES_HEADING in user
        assert "禁止照抄" in user
        assert "双均线金叉策略,止损 8%" in user          # 历史需求
        assert "000001.XSHE" in user                      # 历史标的
        assert "(成功)" in user
        assert "用户需求:\n写一个双均线策略" in user       # 本轮需求仍在前
        assert user.index("用户需求:") < user.index(CASES_HEADING)

    def test_no_cases_no_section(self, tmp_path):
        llm = ScriptedLLM([GOOD_CODE])
        editor = StrategyEditorAgent(llm, validate_strategy, experience=ExperienceStore(tmp_path))
        editor.generate("写一个双均线策略")
        _, user = llm.calls[0]
        assert CASES_HEADING not in user
        assert user == f"用户需求:\n写一个双均线策略"

    def test_system_prompt_unchanged_and_retry_intact(self, tmp_path):
        store = self._store_with_case(tmp_path)
        llm = ScriptedLLM([FENCED_BAD_CODE, GOOD_CODE])
        editor = StrategyEditorAgent(llm, validate_strategy, experience=store)
        editor.generate("写一个双均线策略")

        assert editor.system == render_prompt("strategy_editor.md")
        assert llm.calls[0][0] == llm.calls[1][0] == editor.system  # 系统提示词不变
        # 重试语义完整:findings + 防作弊声明逐字保留,且案例段落两轮都在
        for _, user in llm.calls:
            assert CASES_HEADING in user
        assert "ast.shift_negative" in llm.calls[1][1]
        assert ANTI_CHEAT_CLAUSE in llm.calls[1][1]

    def test_case_code_truncated_to_40_lines(self, tmp_path):
        long_code = "\n".join(f"line_{i:02d}" for i in range(1, 61))
        store = self._store_with_case(tmp_path, code=long_code)
        llm = ScriptedLLM([GOOD_CODE])
        StrategyEditorAgent(llm, validate_strategy, experience=store).generate("双均线")
        _, user = llm.calls[0]
        assert "line_01" in user and "line_40" in user
        assert "line_41" not in user and "line_60" not in user
        assert f"仅展示前 {MAX_CODE_LINES} 行" in user

    def test_search_failure_does_not_block_generation(self):
        class BadStore:
            def search(self, query_tags, top_k=3):
                raise RuntimeError("检索爆炸")

        llm = ScriptedLLM([GOOD_CODE])
        editor = StrategyEditorAgent(llm, validate_strategy, experience=BadStore())
        with pytest.warns(UserWarning, match="经验库检索失败"):
            assert editor.generate("写一个双均线策略") == GOOD_CODE.strip()
        assert len(llm.calls) == 1


class TestOrchestratorExperience:
    """编排器记录点:成功/失败各落盘一条,记录异常不阻塞。"""

    def test_success_records_one_entry(self, tmp_path):
        store = ExperienceStore(tmp_path / "exp")
        runner = StubRunner()
        text = "写一个双均线策略,目标仓位 80%"
        result = run_conversation(text, START, END, llm=DeterministicLLM(),
                                  runner=runner, experience=store)
        assert result["ok"] is True

        files = list((tmp_path / "exp").glob("*.json"))
        assert len(files) == 1
        entry = json.loads(files[0].read_text(encoding="utf-8"))
        assert entry["requirement"] == text
        assert entry["code"] == result["code"]
        assert entry["passed"] is True
        assert entry["metrics"]["sharpe"] == 1.1
        assert entry["metrics"]["start"] == START
        assert {"均线", "仓位", "000001.XSHE"} <= set(entry["tags"])

    def test_edit_failure_records_failed_entry(self, tmp_path):
        store = ExperienceStore(tmp_path / "exp")
        runner = StubRunner()
        result = run_conversation("写一个偷看明天收盘价的策略", START, END,
                                  llm=ConstantLLM(FENCED_BAD_CODE),
                                  runner=runner, experience=store)
        assert result["rejected"] is True
        assert runner.calls == []  # 失败案例记录绝不影响拦截语义

        files = list((tmp_path / "exp").glob("*.json"))
        assert len(files) == 1
        entry = json.loads(files[0].read_text(encoding="utf-8"))
        assert entry["passed"] is False
        assert entry["code"] == BAD_CODE.strip()  # 最后一次生成的最佳尝试(提取器已 strip)
        assert entry["metrics"] is None
        assert entry["requirement"] == "写一个偷看明天收盘价的策略"

    def test_record_exception_does_not_block_pipeline(self, tmp_path):
        class ExplodingStore:
            def search(self, query_tags, top_k=3):
                return []  # 检索正常,聚焦验证记录异常

            def record(self, entry):
                raise RuntimeError("磁盘爆炸")

        runner = StubRunner()
        with pytest.warns(UserWarning, match="经验库记录失败"):
            result = run_conversation("写一个双均线策略", START, END,
                                      llm=DeterministicLLM(), runner=runner,
                                      experience=ExplodingStore())
        assert result["ok"] is True
        assert [s["step"] for s in result["steps"]] == [
            "edit", "validate", "backtest", "review", "report",
        ]
        assert len(runner.calls) == 1

    def test_none_experience_behaves_as_before(self, tmp_path):
        # 默认 None:不落盘、行为与既有管线一致
        store = ExperienceStore(tmp_path)
        result = run_conversation("写一个双均线策略", START, END,
                                  llm=DeterministicLLM(), runner=StubRunner())
        assert result["ok"] is True
        assert list(store.dir.glob("*.json")) == []
        assert result["code"] == DeterministicLLM().complete("s", "写一个双均线策略").strip()
