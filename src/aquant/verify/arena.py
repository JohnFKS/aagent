"""Layer 4:多策略竞技场——多候选并行回测 + 指标对比排名。

设计来源:docs/design.md §1.4 采纳的"多 Agent 养蛊式大逃杀"思路
(蒸馏多个 Agent 同场竞技、按表现动态排名)的平台化落地:
一次提交多个候选策略,统一过 Layer 1 校验门禁 → 通过者并行回测 →
复用 rank_candidates/render_ranking 按(年化收益, 夏普)降序排名。

职责边界:
- 校验阶段**串行**(便宜:纯 AST/配置 lint),含 error 的候选直接淘汰
  (status="rejected"),绝不进入回测;
- 回测阶段**并行**(ThreadPoolExecutor,max_workers=min(4, 待跑候选数)):
  回测重活本就在 run_backtest 的子进程里,线程只负责并发发起与收集,
  不放大 GIL 压力;
- 失败隔离:单个候选执行失败(BacktestExecutionError/FileNotFoundError 等)
  只淘汰自己(status="failed"),不中断全场竞技。
"""

from __future__ import annotations

import concurrent.futures
from pathlib import Path
from typing import Any, Protocol

from aquant.config.schema import build_run_config
from aquant.validation.validator import validate_strategy
from aquant.verify.metrics import rank_candidates, render_ranking

# 并行回测的线程上限:线程只负责发起/等待子进程,4 路并发足以覆盖
# 竞技场 6 候选上限,同时避免无谓放大本机负载
MAX_WORKERS = 4

# 失败候选错误摘要的最大长度(stderr 尾部可能很长,报告页只需要点)
_ERROR_SUMMARY_MAX = 300


class ArenaRunner(Protocol):
    """竞技场 Runner 协议(与 agents 层 Runner 同构,dict 形态)。

    run(code, start, end, **kw) 返回回测结果:dict(含 "summary"/"verdict"
    键,api 层 RealRunnerAdapter 已归一化)或 BacktestResult(取
    .summary/.verdict 属性)。两种形态 run_arena 都能消化。
    """

    def run(self, code: str, start: str, end: str, **kw: Any) -> Any: ...


def _verdict_to_dict(verdict: Any) -> Any:
    """verdict 统一为 dict:Verdict 对象 → to_dict(),dict 原样,None 保持 None。"""
    if verdict is None or isinstance(verdict, dict):
        return verdict
    to_dict = getattr(verdict, "to_dict", None)
    return to_dict() if callable(to_dict) else verdict


def _extract_summary_verdict(result: Any) -> tuple[dict[str, Any], Any]:
    """兼容两种回测结果形态:dict(adapter 归一化)与 BacktestResult(属性)。"""
    if isinstance(result, dict):
        return result.get("summary") or {}, result.get("verdict")
    return getattr(result, "summary", None) or {}, getattr(result, "verdict", None)


def _error_summary(exc: BaseException) -> str:
    """失败候选的错误摘要(压成单行、截断,便于 API 响应与报告直接展示)。"""
    text = " ".join(str(exc).split())
    if len(text) > _ERROR_SUMMARY_MAX:
        text = text[: _ERROR_SUMMARY_MAX - 3] + "..."
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def run_arena(
    candidates: list[dict[str, str]],
    start: str,
    end: str,
    bundle_path: str | Path | None = None,
    *,
    cash: float | None = None,
    runner: ArenaRunner | None = None,
) -> dict[str, Any]:
    """多策略候选并行回测 + 指标对比排名("多 Agent 养蛊式"竞技场)。

    流程:
    1. 串行 Layer 1 校验:validate_strategy(code, build_run_config(start, end,
       cash=cash));含 error 的候选 status="rejected"(附 verdict dict),不执行;
    2. 通过校验的候选经 runner 并行回测(ThreadPoolExecutor);
       runner=None 时延迟 import run_backtest 直连真实回测(需 bundle_path);
    3. 执行失败(BacktestExecutionError/FileNotFoundError 等)→
       status="failed"(附 error 摘要),不中断其余候选;
    4. 成功候选的 summary 进 rank_candidates 排名,render_ranking 渲染表格。

    :param candidates: [{"name": 候选名, "code": 策略源码}];至少 1 个且名不重复
    :param bundle_path: 数据 bundle 目录(仅 runner=None 直连 run_backtest 时
        使用;传入 runner 时由 runner 自行解析,如 RealRunnerAdapter 走
        default_bundle_path(),此参数不透传给 runner)
    :param cash: 初始资金(校验配置与 runner=None 路径的回测共用;runner 路径
        仅在显式提供时透传)
    :returns: {"ranking": 排名列表, "table": 文本排名表, "details": 每候选
        {"status": "rejected"/"failed"/"ranked", "verdict"?/"error"?/"metrics"?},
        "start", "end", "count", "ranked_count"}
    """
    if not candidates:
        raise ValueError("竞技场至少需要 1 个候选策略")
    names = [str(c["name"]) for c in candidates]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise ValueError(f"候选名重复:{', '.join(duplicates)}(同场竞技的候选名必须唯一)")

    run_config = build_run_config(start, end, cash=cash)
    details: dict[str, dict[str, Any]] = {}
    summaries: dict[str, dict[str, Any]] = {}
    to_run: list[tuple[str, str]] = []

    # ---- 阶段 1:校验门禁(串行,便宜;被拒者绝不进入回测)------------------
    for cand in candidates:
        name, code = str(cand["name"]), cand["code"]
        verdict = validate_strategy(code, run_config)
        if verdict.has_error:
            details[name] = {"status": "rejected", "verdict": verdict.to_dict()}
        else:
            to_run.append((name, code))

    # ---- 阶段 2:并行回测(重活在外部子进程,线程只发起/收集)--------------
    def _execute(code: str) -> Any:
        if runner is not None:
            kw: dict[str, Any] = {}
            if cash is not None:
                kw["cash"] = cash
            # bundle 由 runner 自行处理(RealRunnerAdapter 内部走默认路径),
            # 因此不向 runner 透传 bundle_path
            return runner.run(code, start, end, **kw)
        from aquant.runner.backtest import run_backtest  # 延迟导入,保持层间解耦

        return run_backtest(code, start, end, bundle_path=bundle_path, cash=cash)

    if to_run:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(MAX_WORKERS, len(to_run))
        ) as pool:
            futures = {pool.submit(_execute, code): name for name, code in to_run}
            for future, name in futures.items():
                try:
                    result = future.result()
                except Exception as exc:  # 单候选失败不中断全场(含 BaseException 之外的一切)
                    details[name] = {"status": "failed", "error": _error_summary(exc)}
                    continue
                if isinstance(result, dict):
                    exec_error = result.get("execution_error")
                else:
                    exec_error = getattr(result, "execution_error", None)
                if exec_error:
                    details[name] = {
                        "status": "failed",
                        "error": _error_summary(RuntimeError(str(exec_error))),
                    }
                    continue
                summary, verdict = _extract_summary_verdict(result)
                if not summary:
                    details[name] = {"status": "failed", "error": "回测未返回有效指标,不参与排名"}
                    continue
                summaries[name] = summary
                details[name] = {
                    "status": "ranked",
                    "metrics": summary,
                    "verdict": _verdict_to_dict(verdict),
                }

    # ---- 阶段 3:排名与渲染(复用 verify/metrics)--------------------------
    ranked = rank_candidates(summaries)
    return {
        "ranking": ranked,
        "table": render_ranking(ranked),
        "details": details,
        "start": start,
        "end": end,
        "count": len(candidates),
        "ranked_count": len(ranked),
    }
