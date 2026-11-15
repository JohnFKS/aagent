"""StrategyOptimizerAgent:AI 自动优化闭环(回测 → 诊断 → LLM 改码 → 对比保留)。

用户需求:"agent 可以根据代码以及回测结果自动调整策略,包括但不限于超参数、
策略代码和策略逻辑,从而达到更好的效果"。

每轮循环(prompt 工程与校验-重试-防作弊三件套同 strategy_editor,见 docs/design.md):
1. 回测当前最优代码(Runner Protocol,dict 形态与 RealRunnerAdapter 归一化一致,
   含 "summary"/"trades"/"execution_error";runner=None 时延迟 import run_backtest
   直接驱动真实回测——模块加载不 import aquant.runner,保持 ADR-5 解耦);
2. _diagnose 按规则自动生成改进线索(无成交/换手过高/成本占比/回撤/胜率/平稳微调);
3. 构造优化 prompt(硬约束模板 optimizer.md):当前代码 + 最新指标(含诊断)+
   已尝试轮次摘要(避免重复);LLM 输出 = 先 `## 优化分析` 后单个 ```python 块;
4. extract_python_block 提取 → validate_strategy(注入 self.validate)校验;
   ERROR 把 findings 文本拼回重试(轮内重试 ≤2 次,复用 ANTI_CHEAT_CLAUSE),
   仍错则本轮记 failed(rejected=True);
5. 回测新代码 → execution_error 把错误回馈重试(轮内 ≤1 次)→ 仍崩本轮记 failed;
6. 对比保留:_score 元组 (sharpe, annualized_returns, -|max_drawdown|),sharpe 为主;
   新分严格大于当前最优分才采用(best 更新,kept=True),否则丢弃
   (kept=False,error="未超越当前最优");
7. 轮次历史 rounds:{"round","analysis","kept","summary","rejected","error"}。

终止:rounds 用完,或连续 2 轮无改进(早停)。LLM=None 抛 ValueError(优化必须有
LLM);LLMAPIError 记 failed 继续下一轮,全部轮次 LLM 失败 → improved=False 且
error 汇总返回(不抛)。结束时注入 experience 则 record 最终 best(复用
orchestrator._record_experience,agents 内部复用,异常只 warn 不阻塞)。
"""

from __future__ import annotations

import time
from typing import Any, Callable, TypedDict

from aquant.agents.backtest_agent import Runner
from aquant.agents.experience import ExperienceStore
from aquant.agents.llm import LLMAPIError, LLMClient
from aquant.agents.orchestrator import _record_experience
from aquant.agents.prompts import render_prompt
from aquant.agents.strategy_editor import ANTI_CHEAT_CLAUSE, extract_python_block
from aquant.validation.findings import Finding, RuleId, Severity, Verdict
from aquant.validation.validator import validate_strategy

# 轮内重试预算(与 strategy_editor 的 MAX_ATTEMPTS=3 语义对齐:1 次首发 + 2 次重试)
MAX_VALIDATE_RETRIES = 2
MAX_EXEC_RETRIES = 1
# 早停:连续 2 轮无改进(采纳失败或未超越)即终止
EARLY_STOP_ROUNDS = 2

# 诊断阈值(经验值,docs/design.md 自我优化工作流的落地规则)
TURNOVER_HIGH = 20.0
COST_RATIO_HIGH = 0.02
DRAWDOWN_HIGH = 0.3
WIN_RATE_LOW = 0.35

# 诊断线索文案(逐字写入 prompt 与测试断言)
NO_TRADE_CLUE = "策略从未成交:检查信号条件是否过严、标的区间停牌、仓位计算是否正确"
TURNOVER_CLUE = "换手过高,成本侵蚀收益:考虑加信号过滤降低频率"
COST_CLUE = "交易成本占比过高"
DRAWDOWN_CLUE = "回撤偏大:考虑仓位控制、止损或分散"
WIN_RATE_CLUE = "胜率偏低:检查出场逻辑与信号质量"
STABLE_CLUE = "指标平稳:尝试超参数微调(幅度每次 ≤50%)或入场过滤"

# 未超越时的弃用文案(逐字写入 rounds[].error,测试断言)
REJECT_NOT_BETTER = "未超越当前最优"


class OptimizationResult(TypedDict):
    """optimize() 的返回结构(JSON 友好;error 仅在全部轮次 LLM 失败时非 None)。"""

    baseline: dict[str, Any]  # {"code": 传入源码, "summary": 首轮回测指标}
    best: dict[str, Any]      # {"code": 最优源码, "summary": 最优指标}
    improved: bool
    rounds: list[dict[str, Any]]
    elapsed_seconds: float
    error: str | None


def _num(x: Any) -> float | None:
    """宽松取数:None/非数值/NaN → None(与 verify/metrics 同口径)。"""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


def _score(summary: dict[str, Any] | None) -> tuple[float, float, float]:
    """优化目标评分元组:(sharpe, annualized_returns, -|max_drawdown|),字典序比较。

    回撤项取 -abs():rqalpha 6.5.0 的 max_drawdown 为负数(如 -0.08),字面取负号
    会变成"回撤越深分越高"的方向错误;统一先取绝对值再取负,与符号约定无关地
    实现"回撤越小越好"。指标缺失按 -inf 参与(任何有指标的候选都优于无指标)。
    """
    if not summary:
        return (float("-inf"), float("-inf"), float("-inf"))
    sharpe = _num(summary.get("sharpe"))
    ann = _num(summary.get("annualized_returns"))
    dd = _num(summary.get("max_drawdown"))
    return (
        sharpe if sharpe is not None else float("-inf"),
        ann if ann is not None else float("-inf"),
        -abs(dd) if dd is not None else float("-inf"),
    )


def _diagnose(summary: dict[str, Any] | None, trades_count: int, cash: float | None) -> list[str]:
    """按指标规则自动生成改进线索(诊断式 prompt 的素材;命中多条可并存)。

    规则:无成交 / 换手率>20 / 佣金占初始资金>2% / 回撤>0.3 / 胜率<0.35(且有成交);
    全不命中给"平稳微调"兜底线索,保证 prompt 每轮都有可执行的优化方向。
    """
    clues: list[str] = []
    if trades_count <= 0:
        clues.append(NO_TRADE_CLUE)
    if summary:
        turnover = _num(summary.get("turnover"))
        if turnover is not None and turnover > TURNOVER_HIGH:
            clues.append(TURNOVER_CLUE)
        commission = _num(summary.get("total_commission"))
        base_cash = cash if cash is not None else _default_cash()
        if commission is not None and base_cash and commission / base_cash > COST_RATIO_HIGH:
            clues.append(COST_CLUE)
        dd = _num(summary.get("max_drawdown"))
        if dd is not None and abs(dd) > DRAWDOWN_HIGH:
            clues.append(DRAWDOWN_CLUE)
        win_rate = _num(summary.get("win_rate"))
        if trades_count > 0 and win_rate is not None and win_rate < WIN_RATE_LOW:
            clues.append(WIN_RATE_CLUE)
    if not clues:
        clues.append(STABLE_CLUE)
    return clues


def _default_cash() -> float:
    """平台默认初始资金(Layer 3 defaults 唯一事实来源;读取失败回落 100 万)。"""
    try:
        from aquant.config.schema import load_defaults

        return float(load_defaults()["account"]["default_cash"])
    except Exception:  # pragma: no cover - 配置缺失的兜底
        return 1_000_000.0


def _summary_of(result: dict[str, Any]) -> dict[str, Any] | None:
    s = result.get("summary")
    return dict(s) if isinstance(s, dict) else None


def _trades_count(result: dict[str, Any]) -> int:
    trades = result.get("trades")
    # trades 可能是 DataFrame(6.5.0 成交明细)或列表;DataFrame 对 `or` 求真值会抛错
    return len(trades) if hasattr(trades, "__len__") else 0


def _exec_error_of(result: dict[str, Any]) -> str | None:
    err = result.get("execution_error")
    return str(err) if err else None


def _tail(text: str, limit: int = 2500) -> str:
    """取错误文本尾部(与编辑器自愈回馈同款截断,避免 prompt 膨胀)。"""
    return "\n".join(text.strip().splitlines()[-30:])[-limit:]


def _format_feedback(verdict: Verdict, attempt: int) -> str:
    """把上一轮的 ERROR findings 文本化,拼装第 attempt 次重试反馈(含防作弊声明)。"""
    lines = [
        f"## 第 {attempt} 次优化反馈:上一轮代码未通过平台 Layer 1 校验",
        "以下 ERROR 必须全部修复:",
    ]
    lines.extend(f"- {msg}" for msg in verdict.error_messages())
    lines.append(ANTI_CHEAT_CLAUSE)
    return "\n".join(lines)


def _format_exec_feedback(exec_error: str) -> str:
    """执行崩溃回馈(与编辑器"生成-执行自愈闭环"同风格)。"""
    return (
        "## 上一轮优化代码在回测执行阶段崩溃(运行时错误)\n"
        "以下错误发生在真实撮合执行时,请修复代码使回测能完整运行:\n"
        "```\n"
        f"{_tail(exec_error)}\n"
        "```\n"
        "常见原因:history_bars 返回 numpy 结构化数组,取列为 bars['close'] 得到 ndarray,"
        "直接参与运算即可;下单前判断数组长度防越界;检查标的当日是否停牌。\n"
    )


def _extract_analysis(raw: str) -> str:
    """提取 LLM 输出中 ```python 代码块之前的 `## 优化分析` 段落(无代码块时取全文)。"""
    fence = raw.find("```")
    head = raw[:fence] if fence != -1 else raw
    return head.strip()


class StrategyOptimizerAgent:
    """策略优化子 agent:回测-诊断-改码-对比保留的自动优化闭环。"""

    def __init__(
        self,
        llm: LLMClient | None,
        validator: Callable[[str], Verdict] = validate_strategy,
    ) -> None:
        """llm:LLMClient 协议实现(优化必须有 LLM,None 直接 ValueError);
        validator:代码级校验函数(validate_strategy),与编辑器同款注入。
        """
        if llm is None:
            raise ValueError(
                "优化必须有 LLM:请注入 LLMClient(api 层经 _get_llm 惰性构造;"
                "无环境变量时可用 DeterministicLLM 兜底)。"
            )
        self.llm = llm
        self.validator = validator
        self.system = render_prompt("optimizer.md")

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def optimize(
        self,
        code: str,
        start: str,
        end: str,
        *,
        runner: Runner | None = None,
        rounds: int = 2,
        cash: float | None = None,
        mod_overrides: dict[str, dict] | None = None,
        experience: ExperienceStore | None = None,
    ) -> OptimizationResult:
        """执行自动优化闭环,返回 JSON 友好的 OptimizationResult。

        :param code: 待优化策略源码(调用方应先用 validate_strategy 把关,api 层 422);
        :param runner: 回测执行器(缺省 None 时延迟 import run_backtest 直接驱动);
        :param rounds: 轮次预算(每轮 = 1 次 LLM 生成 + 至多 2 次回测);
        :param cash / mod_overrides: 透传给 runner 的回测参数;
        :param experience: 经验库(可选)。提供时结束时 record 最终 best,
            记录异常只 warn 不阻塞(复用 orchestrator._record_experience)。
        """
        if rounds < 1:
            raise ValueError(f"rounds 必须 ≥ 1,收到 {rounds}。")
        t0 = time.time()
        rounds_hist: list[dict[str, Any]] = []
        llm_failed_rounds = 0
        consecutive_no_improve = 0

        best_code = code
        best_summary: dict[str, Any] | None = None
        best_score: tuple[float, float, float] | None = None
        baseline_summary: dict[str, Any] | None = None
        improved = False

        for n in range(1, rounds + 1):
            # ---- 1. 回测当前最优代码(首轮 step1 即 baseline 回测) ----------
            result = self._run_backtest(runner, best_code, start, end, cash, mod_overrides)
            summary = _summary_of(result)
            trades_count = _trades_count(result)
            if n == 1:
                baseline_summary = summary
                best_summary = summary
                best_score = _score(summary)

            # ---- 2. 诊断(当前代码自身若崩溃,把错误一并作为线索) ----------
            clues = _diagnose(summary, trades_count, cash)
            crash = _exec_error_of(result)
            if crash:
                clues.append(f"当前代码回测执行崩溃:{_tail(crash, 300)}")

            # ---- 3~5. LLM 改码 + 校验重试 + 执行自愈 ------------------------
            entry: dict[str, Any] = {
                "round": n, "analysis": "", "kept": False,
                "summary": None, "rejected": False, "error": None,
            }
            candidate_code: str | None = None
            candidate_summary: dict[str, Any] | None = None

            user = self._build_user_message(best_code, summary, trades_count, clues,
                                            rounds_hist, start, end)
            feedback = ""
            validate_retries = 0
            exec_retries = 0
            while True:
                prompt = f"{user}\n\n{feedback}" if feedback else user
                try:
                    raw = self.llm.complete(self.system, prompt)
                except LLMAPIError as exc:
                    entry["rejected"] = True
                    entry["error"] = f"LLM 调用失败:{exc}"
                    llm_failed_rounds += 1
                    break

                entry["analysis"] = _extract_analysis(raw)
                new_code = extract_python_block(raw)
                if new_code is None:
                    # 输出格式违规视同 ERROR,进入重试(与编辑器同语义)
                    verdict = Verdict((Finding(
                        RuleId.AST_SYNTAX, Severity.ERROR,
                        "输出未包含 ```python 代码块,无法提取策略源码。",
                    ),))
                else:
                    verdict = self.validator(new_code)

                if verdict.has_error:
                    if validate_retries < MAX_VALIDATE_RETRIES:
                        validate_retries += 1
                        feedback = _format_feedback(verdict, validate_retries)
                        continue
                    entry["rejected"] = True
                    entry["error"] = "连续校验未通过:" + "; ".join(verdict.error_messages())
                    break

                # 校验通过 → 回测新代码(execution_error 则回馈自愈)
                new_result = self._run_backtest(runner, new_code, start, end, cash, mod_overrides)
                exec_error = _exec_error_of(new_result)
                if exec_error:
                    if exec_retries < MAX_EXEC_RETRIES:
                        exec_retries += 1
                        feedback = _format_exec_feedback(exec_error)
                        continue
                    entry["rejected"] = True
                    entry["error"] = f"回测执行崩溃:{_tail(exec_error, 500)}"
                    break

                candidate_summary = _summary_of(new_result)
                entry["summary"] = candidate_summary
                candidate_code = new_code
                break

            # ---- 6. 对比保留:新分严格大于当前最优分才采用 -------------------
            if candidate_code is not None:
                new_score = _score(candidate_summary)
                if best_score is None or new_score > best_score:
                    best_code, best_summary, best_score = candidate_code, candidate_summary, new_score
                    entry["kept"] = True
                    improved = True
                    consecutive_no_improve = 0
                else:
                    entry["error"] = REJECT_NOT_BETTER
                    consecutive_no_improve += 1
            else:
                consecutive_no_improve += 1  # 校验/执行/LLM 失败同样计为"无改进"

            rounds_hist.append(entry)
            # ---- 终止:连续 2 轮无改进早停 -----------------------------------
            if consecutive_no_improve >= EARLY_STOP_ROUNDS:
                break

        error_summary: str | None = None
        if rounds_hist and llm_failed_rounds == len(rounds_hist):
            error_summary = (
                f"全部 {len(rounds_hist)} 轮的 LLM 调用均失败,未产生任何优化:"
                + "; ".join(str(r.get("error")) for r in rounds_hist)
            )
        result: OptimizationResult = {
            "baseline": {"code": code, "summary": baseline_summary},
            "best": {"code": best_code, "summary": best_summary},
            "improved": improved,
            "rounds": rounds_hist,
            "elapsed_seconds": round(time.time() - t0, 3),
            "error": error_summary,
        }
        if experience is not None:
            adopted = sum(1 for r in rounds_hist if r["kept"])
            _record_experience(
                experience,
                user_text=f"AI 自动优化({rounds} 轮预算,采纳 {adopted} 次)",
                code=best_code,
                rejected=False,
                metrics=best_summary,
            )
        return result

    # ------------------------------------------------------------------
    # 内部:回测与 prompt 构造
    # ------------------------------------------------------------------
    def _run_backtest(
        self,
        runner: Runner | None,
        code: str,
        start: str,
        end: str,
        cash: float | None,
        mod_overrides: dict[str, dict] | None,
    ) -> dict[str, Any]:
        """执行一次回测并归一化为 dict 形态(summary/trades/execution_error)。

        runner 未注入时延迟 import run_backtest 直接驱动真实回测(与
        RealRunnerAdapter 同款延迟导入与异常归一化,保持模块加载期解耦)。
        """
        kw: dict[str, Any] = {}
        if cash is not None:
            kw["cash"] = cash
        if mod_overrides:
            kw["mod_overrides"] = mod_overrides
        if runner is not None:
            raw = runner.run(code, start, end, **kw)
            if isinstance(raw, dict):
                return raw
            # 对象形态(BacktestResult 等)归一化为 dict(Runner 协议不强约束返回)
            return {
                "summary": getattr(raw, "summary", None),
                "trades": getattr(raw, "trades", []) or [],
                "execution_error": None,
            }
        from aquant.runner.backtest import BacktestExecutionError, key_metrics, run_backtest

        try:
            raw = run_backtest(code, start, end, **kw)
        except BacktestExecutionError as exc:
            return {"summary": None, "trades": [], "execution_error": str(exc)}
        return {
            "summary": key_metrics(raw),
            "trades": getattr(raw, "trades", []) or [],
            "execution_error": None,
        }

    def _build_user_message(
        self,
        code: str,
        summary: dict[str, Any] | None,
        trades_count: int,
        clues: list[str],
        rounds_hist: list[dict[str, Any]],
        start: str,
        end: str,
    ) -> str:
        """user 消息:当前代码 + 最新指标 + 诊断线索 + 已尝试轮次摘要(防重复)。"""
        parts = [
            "## 优化任务",
            "请根据当前策略代码与最近一次回测表现,输出一版改进后的策略代码。",
            "",
            "## 当前策略代码",
            "```python",
            code,
            "```",
            "",
            f"## 最近一次回测指标(区间 {start} ~ {end},成交 {trades_count} 笔)",
        ]
        if summary:
            parts.extend(f"- {key}: {value}" for key, value in summary.items())
        else:
            parts.append("- (无有效指标)")
        parts += ["", "## 诊断线索(自动生成,优化应优先回应这些线索)"]
        parts.extend(f"- {clue}" for clue in clues)
        parts += ["", _format_rounds_history(rounds_hist)]
        return "\n".join(parts)


def _format_rounds_history(rounds_hist: list[dict[str, Any]]) -> str:
    """已尝试轮次摘要(仅含已结束的轮次;避免 LLM 重复被拒改动)。"""
    if not rounds_hist:
        return "## 已尝试轮次摘要\n- 尚无已尝试轮次(本轮为第 1 轮)。"
    lines = ["## 已尝试轮次摘要(避免重复相同的改动)"]
    for r in rounds_hist:
        if r["kept"]:
            status = "已采纳"
        elif r["rejected"]:
            status = "失败"
        else:
            status = REJECT_NOT_BETTER
        first_line = next(
            (ln.strip() for ln in str(r.get("analysis") or "").splitlines() if ln.strip()),
            "(无分析)",
        )[:120]
        lines.append(f"- 第 {r['round']} 轮[{status}]:{first_line}")
        if r.get("error"):
            lines.append(f"  - 结果:{r['error']}")
    return "\n".join(lines)
