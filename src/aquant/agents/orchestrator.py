"""编排器 Orchestrator:edit → validate → backtest → review → 汇总报告。

完整管线(docs/design.md 第 3 节):
1. edit    StrategyEditorAgent:LLM 生成 + 校验-重试(最多 3 次,防作弊约束);
2. validate ValidationAgent:带 run_config 的完整 Layer 1 校验;
3. backtest BacktestAgent:校验不过绝不回测(拦截则整条管线终止);
4. review  ReviewAgent:规则 findings + LLM 人工复核合并去重;
5. 汇总    用 result_reporter.md 模板生成面向用户的中文报告。

经验库闭环(docs/design.md §1.2):注入可选 ExperienceStore 时,本次对话的结果
在 report 步骤后 record 入库(成功案例);edit 3 次未过的失败案例也记录
(passed=False,code 取最后一次生成尝试)。记录异常只 warn 不阻塞主管线。

解耦约定(ADR 5):本模块不 import aquant.runner;runner 参数由调用方注入
(测试注入 stub,api 层注入 RealRunnerAdapter),为 None 时跳过回测步骤。
LLM 默认按环境变量构造(make_llm),无 Key 时回落 DeterministicLLM。
"""

from __future__ import annotations

import warnings
from typing import Any

from aquant.agents.backtest_agent import BacktestAgent, Runner
from aquant.agents.experience import ExperienceStore, derive_tags
from aquant.agents.llm import LLMClient, make_llm
from aquant.agents.prompts import render_prompt
from aquant.agents.review_agent import ReviewAgent
from aquant.agents.strategy_editor import StrategyEditorAgent, StrategyGenerationError
from aquant.agents.validation_agent import ValidationAgent
from aquant.config.schema import build_run_config, load_defaults
from aquant.validation.findings import Verdict
from aquant.validation.validator import validate_strategy

# 报告中按百分比展示的指标键
_PERCENT_KEYS = {"total_returns", "annualized_returns", "benchmark_total_returns",
                 "max_drawdown", "volatility", "alpha", "beta"}


def run_conversation(
    user_text: str,
    start: str,
    end: str,
    *,
    llm: LLMClient | None = None,
    runner: Runner | None = None,
    experience: ExperienceStore | None = None,
    cash: float | None = None,
    benchmark: str | None = None,
) -> dict[str, Any]:
    """执行一次完整对话编排,返回 JSON 友好的结果 dict。

    :param user_text: 用户的自然语言策略需求;
    :param start/end: 回测区间 'YYYY-MM-DD';
    :param llm: LLM 客户端(缺省按 AQUANT_LLM_* 环境变量构造,无配置用 DeterministicLLM);
    :param runner: 回测执行器(缺省 None 时跳过回测步骤;测试注入 stub,api 层注入真实适配器);
    :param experience: 经验库(可选)。提供时:生成前按需求标签检索相似案例注入编辑器,
        report 后把本次结果 record 入库;edit 失败也记录失败案例;记录异常不阻塞管线。
    :param benchmark: 基准指数 oid(如 000001.XSHG);None 时回测不设基准。
    :returns: {"ok", "rejected", "steps", "code", "verdict", "metrics", "portfolio_series",
        "benchmark_series", "review", "report", "error"}
    """
    llm = llm if llm is not None else make_llm()
    validation_agent = ValidationAgent()
    steps: list[dict[str, Any]] = []

    # ---- 1~3. 生成-校验-执行自愈闭环(含经验库案例注入) ----------------------
    editor = StrategyEditorAgent(llm, validate_strategy, experience=experience)

    # 静态校验无法覆盖 API 误用类运行时错误(真实 LLM 实测:把 history_bars 的
    # ndarray 当 DataFrame 用会崩在撮合阶段)。崩溃时把错误回馈生成器自愈重试,
    # 即"自己复盘/自己反思/自己改正优化"循环(设计来源:PA_Agent 校验-重试-防作弊
    # 三件套 + 博主 Agent 自我优化工作流,见 docs/design.md §1.2/§1.4)。
    # 静态校验无法覆盖 API 误用类运行时错误(真实 LLM 实测:把 history_bars 的
    # ndarray 当 DataFrame 用会崩在撮合阶段)。崩溃时把错误回馈生成器自愈重试,
    # 即"自己复盘/自己反思/自己改正优化"循环(设计来源:PA_Agent 校验-重试-防作弊
    # 三件套 + 博主 Agent 自我优化工作流,见 docs/design.md §1.2/§1.4)。
    MAX_RUNTIME_RETRIES = 2
    runtime_error: str | None = None
    runtime_attempt = 0

    while True:
        try:
            code = editor.generate(user_text, runtime_error=runtime_error)
        except StrategyGenerationError as exc:
            steps.append({
                "step": "edit",
                "ok": False,
                "attempts": exc.attempts,
                "summary": f"连续 {exc.attempts} 次生成未通过 Layer 1 校验,管线终止,未进入回测。",
            })
            # 失败案例也入库:code 取最后一次生成的最佳尝试(可能为 None)
            _record_experience(experience, user_text=user_text,
                               code=getattr(exc, "last_code", None), rejected=True, metrics=None)
            return {
                "ok": False,
                "rejected": True,
                "steps": steps,
                "code": None,
                "verdict": exc.verdict.to_dict(),
                "metrics": None,
                "portfolio_series": None,
                "benchmark_series": None,
                "trades": None,
                "review": [],
                "report": None,
                "error": str(exc),
            }
        steps.append({"step": "edit", "ok": True, "attempts": editor.attempts,
                      "summary": f"第 {editor.attempts} 次生成通过 Layer 1 代码校验。"})

        # ---- 2. validate:带 run_config 的完整校验 -------------------------------
        run_config = build_run_config(start, end)
        verdict = validation_agent.validate(code, run_config)
        steps.append({"step": "validate", "passed": not verdict.has_error,
                      "summary": validation_agent.summarize(verdict)})
        if verdict.has_error:
            # 防御性分支:编辑阶段已保证代码级无 ERROR,正常不会走到(配置来自平台默认)
            return _terminated(steps, code=code, verdict=verdict,
                               error="完整校验未通过(含配置),管线终止。")

        if runner is None:
            steps.append({"step": "backtest", "skipped": True, "rejected": False,
                          "summary": "未注入 runner,跳过回测(测试可注入 stub,api 层注入 RealRunnerAdapter)。"})
            metrics = None
            portfolio_series: list | None = []
            benchmark_series: list | None = []
            trades_list: list | None = []
            break

        exec_kw: dict[str, Any] = {}
        if cash is not None:
            exec_kw["cash"] = cash
        if benchmark:
            exec_kw["benchmark"] = benchmark
        outcome = BacktestAgent(runner, validation_agent).execute(
            code, start, end, run_config=run_config, **exec_kw
        )
        if outcome["rejected"]:
            steps.append({"step": "backtest", "skipped": False, "rejected": True,
                          "summary": outcome["reason"]})
            return _terminated(steps, code=code, verdict=outcome["verdict"],
                               error=outcome["reason"])
        result_obj = outcome["result"]
        exec_error = _extract_execution_error(result_obj)
        if exec_error and runtime_attempt < MAX_RUNTIME_RETRIES:
            runtime_attempt += 1
            # 清掉本轮的 edit/validate 步骤,避免步骤条重复;保留自愈标记
            steps = [s for s in steps if s["step"] not in ("edit", "validate")]
            steps.append({
                "step": "backtest", "skipped": False, "rejected": False, "ok": False,
                "summary": (f"第 {runtime_attempt} 次执行崩溃:"
                            f"{exec_error.strip().splitlines()[-1][:120]} —— 错误已回馈生成器自愈。"),
            })
            runtime_error = exec_error
            continue
        if exec_error:
            # 自愈预算耗尽:如实终止,失败案例入库
            steps.append({"step": "backtest", "skipped": False, "rejected": True,
                          "summary": f"运行时错误自愈 {MAX_RUNTIME_RETRIES} 次后仍失败:"
                                     f"{exec_error.strip().splitlines()[-1][:150]}"})
            _record_experience(experience, user_text=user_text, code=code,
                               rejected=True, metrics=None)
            return _terminated(steps, code=code, verdict=verdict,
                               error=f"回测运行时错误(自愈 {MAX_RUNTIME_RETRIES} 次后仍失败):{exec_error}")
        metrics = _extract_metrics(result_obj, start, end)
        portfolio_series = _extract_portfolio_series(result_obj)
        benchmark_series = _extract_benchmark_series(result_obj)
        trades_list = _extract_trades(result_obj)
        steps.append({"step": "backtest", "skipped": False, "rejected": False,
                      "summary": "回测执行完成,指标已汇总。" if metrics else "回测已执行,未取到 summary 指标。"})
        break

    # ---- 4. review:规则结论 + LLM 复核 --------------------------------------
    reviewer = ReviewAgent(llm, verdict.findings)
    merged = reviewer.review(code)
    degraded = any(f["rule"] == "llm.review_parse_failed" for f in merged)
    steps.append({"step": "review", "finding_count": len(merged),
                  "llm_degraded": degraded,
                  "summary": "评审完成。" if not degraded else "LLM 评审输出解析失败,已降级为规则结论。"})

    # ---- 5. report:面向用户的中文报告 ---------------------------------------
    report = _build_report(code, verdict, metrics, merged, validation_agent)
    steps.append({"step": "report", "ok": True, "summary": "报告已生成。"})

    # ---- 6. 经验库记录:成功案例入库(失败案例在 edit 失败分支记录) ----------
    _record_experience(experience, user_text=user_text, code=code,
                       rejected=False, metrics=metrics)

    return {
        "ok": True,
        "rejected": False,
        "steps": steps,
        "code": code,
        "verdict": verdict.to_dict(),
        "metrics": metrics,
        "portfolio_series": portfolio_series,
        "benchmark_series": benchmark_series,
        "trades": trades_list,
        "bundle_extension": _extract_bundle_extension(result_obj),
        "review": merged,
        "report": report,
        "error": None,
    }


def _record_experience(experience: ExperienceStore | None, *, user_text: str,
                       code: str | None, rejected: bool,
                       metrics: dict[str, Any] | None) -> None:
    """把本次对话结果写入经验库(成功/失败案例)。

    经验库未注入时为空操作;记录过程中的任何异常只发 warning,绝不阻塞主管线
    (经验库是旁路增强,不是关键路径)。
    """
    if experience is None:
        return
    try:
        experience.record({
            "requirement": user_text,
            "code": code,
            "passed": not rejected,
            "metrics": metrics,
            "tags": derive_tags(user_text, code),
        })
    except Exception as exc:  # noqa: BLE001 任何落盘/序列化异常都不影响主流程
        warnings.warn(f"经验库记录失败(已忽略,不影响主管线):{exc}", stacklevel=3)


def _terminated(steps: list[dict[str, Any]], *, code: str | None, verdict: Verdict,
                error: str) -> dict[str, Any]:
    """管线中途终止的统一返回结构(rejected=True,无指标)。"""
    return {
        "ok": False,
        "rejected": True,
        "steps": steps,
        "code": code,
        "verdict": verdict.to_dict(),
        "metrics": None,
        "portfolio_series": None,
        "benchmark_series": None,
        "trades": None,
        "review": [],
        "report": None,
        "error": error,
    }


def _extract_execution_error(result: Any) -> str | None:
    """从 Runner 返回中提取运行时崩溃文本(RealRunnerAdapter 归一化的 execution_error)。

    runner 崩溃不抛异常而是返回该键(见 api/adapter.py),使 agents 层
    无需 import aquant.runner 即可感知运行时失败(ADR-5)。
    """
    err = result.get("execution_error") if isinstance(result, dict) else getattr(result, "execution_error", None)
    return str(err) if err else None


def _extract_metrics(result: Any, start: str, end: str) -> dict[str, Any] | None:
    """从 Runner 返回中提取 summary dict,统一转为 JSON 可序列化标量。"""
    summary: Any = None
    if isinstance(result, dict):
        summary = result.get("summary")
    elif hasattr(result, "summary"):
        summary = result.summary
    if summary is None:
        return None
    metrics: dict[str, Any] = {"start": start, "end": end}
    for key, value in dict(summary).items():
        metrics[key] = _to_jsonable(value)
    return metrics


def _extract_portfolio_series(result: Any, max_points: int = 400) -> list[dict[str, Any]]:
    """从 Runner 返回中提取净值序列([{date, net_value}]),供前端绘制净值曲线。

    net_value = total_value / 首日 total_value(归一化到 1.0);
    超过 max_points 等距抽样以控制响应体积;任何异常返回空列表(图表非关键路径)。
    """
    import pandas as pd

    portfolio = result.get("portfolio") if isinstance(result, dict) else getattr(result, "portfolio", None)
    if portfolio is None:
        return []
    try:
        df = portfolio if isinstance(portfolio, pd.DataFrame) else pd.DataFrame(portfolio)
        if df.empty or "total_value" not in df.columns:
            return []
        tv = df["total_value"].astype(float)
        base = float(tv.iloc[0]) or 1.0
        net = tv / base
        step = max(1, len(net) // max_points)
        picked = list(range(0, len(net), step))
        if (len(net) - 1) % step != 0:
            picked.append(len(net) - 1)

        def _point(i: int) -> dict[str, Any]:
            return {
                "date": pd.Timestamp(df.index[i]).strftime("%Y-%m-%d"),
                "net_value": round(float(net.iloc[i]), 6),
            }

        return [_point(i) for i in picked]
    except Exception:
        return []


def _extract_benchmark_series(result: Any, max_points: int = 400) -> list[dict[str, Any]]:
    """从 Runner 返回中提取基准净值序列([{date, net_value}]),供前端叠加基准曲线。

    数据来源:sys_analyser 配置 base.benchmark 时输出的 benchmark_portfolio
    (index=date, column=unit_net_value);与策略序列同样归一化首日 = 1.0 并
    等距抽样。任何异常/未配置基准返回空列表(图表非关键路径)。
    """
    import pandas as pd

    bm = result.get("benchmark_portfolio") if isinstance(result, dict) \
        else getattr(result, "benchmark_portfolio", None)
    if bm is None:
        return []
    try:
        df = bm if isinstance(bm, pd.DataFrame) else pd.DataFrame(bm)
        if df.empty:
            return []
        # rqalpha 6.5.0 benchmark_portfolio 列名 unit_net_value;容错其它常见列
        col = next((c for c in ("unit_net_value", "total_value", "benchmark_unit_net_value")
                    if c in df.columns), None)
        if col is None:
            return []
        nv = df[col].astype(float)
        base = float(nv.iloc[0]) or 1.0
        net = nv / base
        step = max(1, len(net) // max_points)
        picked = list(range(0, len(net), step))
        if (len(net) - 1) % step != 0:
            picked.append(len(net) - 1)

        def _point(i: int) -> dict[str, Any]:
            return {
                "date": pd.Timestamp(df.index[i]).strftime("%Y-%m-%d"),
                "net_value": round(float(net.iloc[i]), 6),
            }

        return [_point(i) for i in picked]
    except Exception:
        return []


def _extract_trades(result: Any, max_trades: int = 300) -> list[dict[str, Any]]:
    """从 Runner 返回中提取成交明细([{date, side, price, quantity}]),供前端图上标记。

    rqalpha 6.5.0 trades 为 DataFrame(datetime/side/last_price/last_quantity),
    旧版本兼容 Trade 对象列表。超过 max_trades 截断(标记是辅助信息,图表非关键
    路径);任何异常返回空列表。
    """
    import pandas as pd

    trades = result.get("trades", []) if isinstance(result, dict) else getattr(result, "trades", [])
    try:
        rows: list[dict[str, Any]] = []
        if isinstance(trades, pd.DataFrame) and not trades.empty:
            for _, t in trades.iterrows():
                rows.append({
                    "date": pd.Timestamp(t["datetime"]).strftime("%Y-%m-%d"),
                    "side": str(t["side"]),
                    "price": float(t["last_price"]),
                    "quantity": float(t["last_quantity"]),
                })
        else:
            for t in trades or []:
                dt = getattr(t, "datetime", None)
                rows.append({
                    "date": pd.Timestamp(dt).strftime("%Y-%m-%d") if dt is not None else "",
                    "side": str(getattr(t, "side", "")),
                    "price": float(getattr(t, "last_price", 0) or 0),
                    "quantity": float(getattr(t, "last_quantity", 0) or 0),
                })
        return rows[:max_trades]
    except Exception:
        return []


def _extract_bundle_extension(result: Any) -> dict[str, Any] | None:
    """提取 bundle 自动扩展信息(RealRunnerAdapter 挂 ensure_coverage 后写入)。"""
    ext = result.get("bundle_extension") if isinstance(result, dict) \
        else getattr(result, "bundle_extension", None)
    return ext if isinstance(ext, dict) else None


def _to_jsonable(value: Any) -> Any:
    """numpy 标量等转 float;bool/None/str 原样;其余失败转 str。"""
    if isinstance(value, (bool, str)) or value is None:
        return value
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return str(value)


def _metrics_table(metrics: dict[str, Any] | None) -> str:
    """把 metrics 渲染为 markdown 表格;收益/回撤类以百分比展示。"""
    if not metrics:
        return "本次未执行回测,无指标可展示。"
    lines = ["| 指标 | 数值 |", "| --- | --- |"]
    for key, value in metrics.items():
        if isinstance(value, float) and key in _PERCENT_KEYS:
            lines.append(f"| {key} | {value * 100:.2f}% |")
        else:
            lines.append(f"| {key} | {value} |")
    return "\n".join(lines)


def _build_report(code: str, verdict: Verdict, metrics: dict[str, Any] | None,
                  review: list[dict[str, Any]], validation_agent: ValidationAgent) -> str:
    """用 result_reporter.md 模板生成面向用户的中文报告。"""
    layer1 = (
        "通过,未发现 ERROR 级未来函数/不可复现/配置违规。"
        if not verdict.has_error
        else "存在 ERROR 级问题,详见校验结论。"
    )
    _dfts = load_defaults()
    layer3 = (
        f"平台已强制注入 {float(_dfts['slippage']['value']):.1%} 比例滑点、"
        f"单K线成交量占比 ≤{float(_dfts['volume_limit']['percent']):.0%} 限制、"
        "T+1 与真实费率(见 INFO 项)。"
    )
    layer4 = "本次为单段回测;建议按样本内/样本外两段复跑并对照买入持有基准后再评估。"
    if metrics is None:
        layer4 += "本次未执行回测。"
    review_lines = [
        f"- [{f['severity']}] {f['rule']}: {f['message']}" for f in review
    ] or ["- 评审无补充发现。"]
    return render_prompt(
        "result_reporter.md",
        TITLE=f"# 策略回测报告(区间 {metrics['start']} ~ {metrics['end']})" if metrics else "# 策略校验报告(未执行回测)",
        VERDICT_SUMMARY=validation_agent.summarize(verdict),
        METRICS_TABLE=_metrics_table(metrics),
        LAYER1_STATUS=layer1,
        LAYER3_STATUS=layer3,
        LAYER4_STATUS=layer4,
        REVIEW_SECTION="\n".join(review_lines),
        DISCLAIMER="本报告由 AQ-Platform 自动生成,仅供研究,不构成任何投资建议。",
    )
