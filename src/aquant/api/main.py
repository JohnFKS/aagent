"""AQ-Platform FastAPI 入口。

端点(docs/design.md 第 3 节"Agent 网页平台"):
- GET  /api/health   服务健康与版本;
- POST /api/validate 策略代码(可选区间)→ Layer 1 Verdict;
- POST /api/chat     对话式编排:edit → validate → backtest → review → report。

依赖注入约定(便于测试替换):
- app.state.llm    为 None 时按环境变量构造(make_llm),测试可直接注入 DeterministicLLM/FakeLLM;
- app.state.runner 为 None 时默认 RealRunnerAdapter(真实回测,需要 bundle 存在),
  测试可注入 stub。bundle 缺失时 /api/chat 返回 400 与构建指引,而不是 500。

aquant.runner 的 StrategyValidationError 在此层映射为 422 + verdict
(延迟导入注册,保持模块加载时不与 aquant.runner 耦合)。
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from aquant import __version__
from aquant.agents.backtest_agent import BacktestAgent
from aquant.agents.llm import LLMAPIError, LLMClient, make_llm
from aquant.agents.orchestrator import run_conversation
from aquant.agents.validation_agent import ValidationAgent
from aquant.api.adapter import RealRunnerAdapter
from aquant.api.token_gate import require_mcp_token
from aquant.config.schema import build_run_config
# 追加式导入(不改动既有行):default_bundle_path 供 /api/arena 的 bundle 预检
from aquant.config.schema import default_bundle_path
from aquant.validation.findings import Verdict
from aquant.verify.arena import run_arena

app = FastAPI(title="AQ-Platform API", version=__version__)

# 依赖注入挂载点:测试可整体替换;为 None 时按需惰性构造
# (llm: LLMClient;runner: agents.backtest_agent.Runner)
app.state.llm = None
app.state.runner = None

BUNDLE_GUIDANCE = (
    "回测数据 bundle 不存在,无法执行真实回测。"
    "请先通过 aquant.data 模块构建 RQAlpha bundle(真实价 + ex_cum_factor 复权因子),"
    "或用环境变量 AQUANT_BUNDLE_PATH 指向已有 bundle 目录(默认 ~/.aquant/bundle),"
    "构建完成后重新调用 /api/chat。"
)


def _get_llm(request: Request) -> LLMClient:
    """LLM 依赖:优先取 app.state.llm(测试注入),否则按环境变量惰性构造。"""
    if getattr(request.app.state, "llm", None) is None:
        request.app.state.llm = make_llm()
    return request.app.state.llm


def _get_runner(request: Request) -> Any:
    """Runner 依赖:优先取 app.state.runner(测试注入),否则用真实适配器。"""
    if getattr(request.app.state, "runner", None) is None:
        request.app.state.runner = RealRunnerAdapter()
    return request.app.state.runner


# ---------------------------------------------------------------------------
# 可选 MCP token 闸门已抽至 aquant.api.token_gate(docs/design.md §1.3
# tick-stock-panel scope 门禁采纳项的完整版):
# - 多 token:AQUANT_MCP_TOKENS(JSON,scope/name/rpm 按 token 配置)优先,
#   AQUANT_MCP_TOKEN 单 token 向后兼容(=readwrite);都未设置 → 闸门关闭
#   (本机开放模式,现状行为);
# - scope 规则表 SCOPE_ROUTES 两档:readonly 仅 /api/validate、readwrite 全部;
# - 每 token 滑动窗口限流(RPM,默认 60,可按 token 覆盖),超限 429。
# 各端点按需声明所需最低档位:validate 只读即可,写操作端点 readwrite。
# ---------------------------------------------------------------------------


class ValidateRequest(BaseModel):
    """/api/validate 请求体:code 必填;start/end 同时提供时做含配置的完整校验。"""

    code: str = Field(min_length=1, description="RQAlpha 策略源码")
    start: str | None = Field(default=None, description="回测起始日 YYYY-MM-DD")
    end: str | None = Field(default=None, description="回测结束日 YYYY-MM-DD")


class ChatRequest(BaseModel):
    """/api/chat 请求体:自然语言需求 + 回测区间。"""

    text: str = Field(min_length=1, description="策略需求描述")
    start: str = Field(description="回测起始日 YYYY-MM-DD")
    end: str = Field(description="回测结束日 YYYY-MM-DD")


class BacktestRequest(BaseModel):
    """/api/backtest 请求体:已就绪的策略代码 + 回测区间。"""

    code: str = Field(min_length=1, description="RQAlpha 策略源码")
    start: str = Field(description="回测起始日 YYYY-MM-DD")
    end: str = Field(description="回测结束日 YYYY-MM-DD")
    cash: float | None = Field(default=None, description="初始资金,缺省用平台默认")
    overrides: dict | None = Field(
        default=None,
        description=(
            "用户自选回测参数覆盖(dict[str, dict],形如 "
            '{"sys_simulation": {"slippage": 0.005}}):'
            "更保守的值放行,激进值(如零滑点)被 Layer 1 校验拒绝(422)"
        ),
    )


@app.get("/api/health")
def health() -> dict[str, str]:
    """健康检查。"""
    return {"status": "ok", "version": __version__}


@app.post("/api/validate", dependencies=[Depends(require_mcp_token(scope="readonly"))])
def validate(req: ValidateRequest) -> dict[str, Any]:
    """Layer 1 校验:返回 Verdict dict + 中文摘要(校验不通过也是 200,passed=False)。"""
    run_config = None
    if req.start and req.end:
        run_config = build_run_config(req.start, req.end)
    agent = ValidationAgent()
    verdict = agent.validate(req.code, run_config)
    payload = verdict.to_dict()
    payload["summary"] = agent.summarize(verdict)
    return payload


@app.post("/api/chat", dependencies=[Depends(require_mcp_token(scope="readwrite"))])
def chat(req: ChatRequest, request: Request) -> dict[str, Any]:
    """对话式编排管线;生成/校验被拒为 200 内的结构化结果,bundle 缺失返回 400 指引。

    经验库:每次对话(成功或生成失败)自动记录;生成时检索相似案例注入
    prompt(复盘-反思-优化闭环,见 docs/design.md §1.2)。store 挂在
    app.state 上可被测试整体替换。
    """
    store = getattr(request.app.state, "experience", None)
    if store is None:
        from aquant.agents.experience import ExperienceStore

        store = ExperienceStore()
        request.app.state.experience = store
    try:
        return run_conversation(
            req.text,
            req.start,
            req.end,
            llm=_get_llm(request),
            runner=_get_runner(request),
            experience=store,
        )
    except FileNotFoundError as exc:
        # 真实回测需要 bundle;缺失属于用户可修复的环境问题,给 400 + 指引而非 500
        return JSONResponse(status_code=400, content={"detail": BUNDLE_GUIDANCE, "reason": str(exc)})
    except LLMAPIError as exc:
        # LLM 网络故障(超时/断连/限流)是外部依赖问题,502 + 中文指引而非 500
        return JSONResponse(status_code=502, content={"detail": "LLM 服务暂时不可用,请稍后重试。", "reason": str(exc)})


@app.post("/api/backtest", dependencies=[Depends(require_mcp_token(scope="readwrite"))])
def backtest(req: BacktestRequest, request: Request) -> dict[str, Any]:
    """直接回测已就绪的策略代码:Layer 1 校验是硬门禁,不过即 422;bundle 缺失 400。

    手动模式(用户自选参数):可选 overrides 原样并入 run 配置交给既有 Layer 1
    把关——更保守的值(如更高滑点)放行,激进值(如零滑点)422 拒绝,零新逻辑。
    """
    # 校验配置:overrides 并入后交 Layer 1 lint(激进值在此被拦)
    run_config = build_run_config(
        req.start, req.end, cash=req.cash, user_mod_overrides=req.overrides
    )
    agent = BacktestAgent(runner=_get_runner(request))
    try:
        # 执行配置:overrides 提供时经 runner 透传给 run_backtest(mod_overrides);
        # 未提供时不附加 kw,与既有调用完全一致
        exec_kw: dict[str, Any] = {"mod_overrides": req.overrides} if req.overrides else {}
        outcome = agent.execute(req.code, req.start, req.end, run_config=run_config, **exec_kw)
    except FileNotFoundError as exc:
        return JSONResponse(status_code=400, content={"detail": BUNDLE_GUIDANCE, "reason": str(exc)})
    verdict: Verdict = outcome["verdict"]
    if outcome.get("rejected"):
        payload = verdict.to_dict()
        payload["summary"] = ValidationAgent().summarize(verdict)
        return JSONResponse(
            status_code=422,
            content={"detail": "策略校验未通过,拒绝执行回测。", "verdict": payload},
        )
    result = outcome["result"]
    summary = result["summary"] if isinstance(result, dict) else getattr(result, "summary", {})
    trades = result["trades"] if isinstance(result, dict) else getattr(result, "trades", [])
    # trades 可能是 DataFrame(6.5.0 成交明细)或列表;DataFrame 对 `or` 求真值会抛错
    trades_count = len(trades) if hasattr(trades, "__len__") else 0
    return {
        "passed": True,
        "verdict": verdict.to_dict(),
        "metrics": summary,
        "trades_count": trades_count,
    }


def _register_runner_exception_handler() -> None:
    """把 aquant.runner 的 StrategyValidationError 映射为 422 + verdict dict。

    import 语句写在函数体内:runner 依赖(pandas/rqalpha 相关)不可用时降级为
    不注册该映射,API 仍可加载;注册动作本身在模块加载时执行。
    """
    try:
        from aquant.runner.backtest import StrategyValidationError
    except Exception:  # pragma: no cover - runner 依赖不可用时的降级
        return

    @app.exception_handler(StrategyValidationError)
    async def _handle_strategy_validation_error(
        request: Request, exc: StrategyValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "detail": "策略校验未通过,拒绝执行回测。",
                "verdict": exc.verdict.to_dict(),
            },
        )


_register_runner_exception_handler()


# ---------------------------------------------------------------------------
# 多策略竞技场(docs/design.md §1.4:"多 Agent 养蛊式大逃杀"思路的平台化落地):
# 一次提交 1~6 个候选策略,统一过 Layer 1 校验门禁,通过者并行回测,
# 按(年化收益, 夏普)降序排名(复用 verify/metrics 的 rank_candidates/render_ranking)。
# 校验被拒/执行失败的候选只淘汰自己,不中断全场竞技。

class ArenaCandidate(BaseModel):
    """竞技场单个候选:候选名 + 策略源码。"""

    name: str = Field(min_length=1, description="候选名(同场不可重复)")
    code: str = Field(min_length=1, description="RQAlpha 策略源码")


class ArenaRequest(BaseModel):
    """/api/arena 请求体:多候选同场竞技 + 回测区间。"""

    candidates: list[ArenaCandidate] = Field(
        min_length=1, max_length=6, description="策略候选(1~6 个)"
    )
    start: str = Field(description="回测起始日 YYYY-MM-DD")
    end: str = Field(description="回测结束日 YYYY-MM-DD")
    cash: float | None = Field(default=None, description="初始资金,缺省用平台默认")


@app.post("/api/arena", dependencies=[Depends(require_mcp_token(scope="readwrite"))])
def arena(req: ArenaRequest, request: Request) -> dict[str, Any]:
    """多策略竞技场:多 Agent 养蛊式排名(设计来源:docs/design.md §1.4 采纳的
    "多 Agent 养蛊式大逃杀"——多个候选策略同场竞技、按表现动态排名)。

    每个候选先过 Layer 1 校验(含 error → status="rejected",不进入回测);
    通过者经 runner 并行回测(重活在外部子进程,线程只并发发起与收集),
    失败候选 status="failed" 只淘汰自己;成功者按 (年化收益, 夏普) 降序排名。
    bundle 缺失(真实 runner)→ 400 + 构建指引;候选名重复 → 400。
    """
    names = [c.name for c in req.candidates]
    if len(set(names)) != len(names):
        raise HTTPException(status_code=400, detail="候选名重复:同场竞技的候选名必须唯一。")

    runner = _get_runner(request)
    # 真实适配器在执行时从 default_bundle_path() 解析 bundle;缺失属于用户可修复
    # 的环境问题,预检直接给 400 + 指引(而不是等所有候选都 failed 后返回 200)
    if isinstance(runner, RealRunnerAdapter):
        bundle_path = default_bundle_path()
        if not bundle_path.exists():
            return JSONResponse(
                status_code=400,
                content={"detail": BUNDLE_GUIDANCE, "reason": f"数据 bundle 不存在:{bundle_path}"},
            )
    return run_arena(
        [{"name": c.name, "code": c.code} for c in req.candidates],
        req.start,
        req.end,
        cash=req.cash,
        runner=runner,
    )


# ---------------------------------------------------------------------------
# K 线行情(docs/design.md §6"K 线图"路线项的后端数据端点):直接读 bundle
# stocks.h5 的真实价日线(不复权,与回测撮合口径一致,ADR #1),供前端
# lightweight-charts 蜡烛图消费。GET + query 参数,行情查询是只读操作 →
# scope 只需 readonly(token_gate.SCOPE_ROUTES 已登记 /api/kline)。
# 注意:必须注册在前端静态托管(_mount_frontend 的 SPA catch-all)之前,
# 路由按注册顺序匹配,否则 /api/kline 会被 index.html fallback 遮蔽。
from aquant.api.kline import KlineError, load_kline
from fastapi import Query


@app.get("/api/kline", dependencies=[Depends(require_mcp_token(scope="readonly"))])
def kline(
    oid: str = Query(default="", description="合约 order_book_id,如 000001.XSHE"),
    start: str | None = Query(default=None, description="起始日 YYYY-MM-DD(可选)"),
    end: str | None = Query(default=None, description="结束日 YYYY-MM-DD(可选)"),
) -> dict[str, Any]:
    """K 线行情:bundle stocks.h5 真实价日线(最多 500 根,超出等距抽样保留首末)。

    容错与 /api/chat 同型:bundle 缺失 → 400 + BUNDLE_GUIDANCE;oid 不在
    bundle / h5 读取失败 / 日期格式非法 → 400 中文提示(KlineError)。
    """
    oid_val = (oid or "").strip()
    try:
        candles = load_kline(oid_val, start, end)
    except FileNotFoundError as exc:
        return JSONResponse(status_code=400, content={"detail": BUNDLE_GUIDANCE, "reason": str(exc)})
    except KlineError as exc:
        return JSONResponse(status_code=400, content={"detail": str(exc)})
    return {"oid": oid_val, "start": start, "end": end, "candles": candles}


# ---------------------------------------------------------------------------
# 手动模式:分层验证(docs/design.md Layer 4 的端点化,与 CLI `aquant verify`
# 同管线)——用户自备策略代码、自选样本内/样本外切分与回测参数,平台直接执行
# aquant.verify.pipeline.layered_verification(IS→OOS 两次回测 + 随机基准对照
# + 对比判定 + 模拟盘引导清单)。与 /api/chat 的"生成-校验-回测-复盘"自动编排
# 互补:这里不做任何生成,策略代码原样进管线;激进参数同样由 Layer 1 校验拒绝。
# 注意:必须注册在前端静态托管(_mount_frontend 的 SPA catch-all)之前,
# 路由按注册顺序匹配,否则 /api/verify 会被 index.html fallback 遮蔽。

class VerifyRequest(BaseModel):
    """/api/verify 请求体:用户自备策略代码 + 分层验证区间与可选参数。"""

    code: str = Field(min_length=1, description="RQAlpha 策略源码")
    start: str = Field(description="回测起始日 YYYY-MM-DD")
    end: str = Field(description="回测结束日 YYYY-MM-DD")
    is_ratio: float = Field(
        default=0.7, ge=0.5, le=0.9, description="样本内占比,限 0.5~0.9,越界 422"
    )
    cash: float | None = Field(default=None, description="初始资金,缺省用平台默认")
    benchmark: str | None = Field(
        default=None, description="基准指数 order_book_id(如 000300.XSHG),缺省不做基准对照"
    )
    overrides: dict | None = Field(
        default=None,
        description=(
            "用户自选回测参数覆盖(dict[str, dict]):更保守的值放行,"
            "激进值(如零滑点)被 Layer 1 校验拒绝(422)"
        ),
    )


@app.post("/api/verify", dependencies=[Depends(require_mcp_token(scope="readwrite"))])
def verify(req: VerifyRequest) -> dict[str, Any]:
    """分层验证:样本内/样本外两次回测 + 随机基准对照 + 对比判定 + 模拟盘清单。

    与 CLI ``aquant verify`` 同管线(aquant.verify.pipeline.layered_verification)。
    错误语义与既有端点对齐:
    - 策略/参数校验未通过(StrategyValidationError)→ 422 + verdict dict
      (由模块级 runner 异常映射统一处理,与 /api/backtest 同语义);
    - 数据 bundle 缺失(FileNotFoundError)→ 400 + BUNDLE_GUIDANCE 构建指引;
    - 区间不足 40 个交易日等参数问题(ValueError)→ 400 中文提示;
    - 回测执行失败(BacktestExecutionError)→ 500 + 中文 reason(服务端问题)。
    """
    bundle_path = default_bundle_path()
    if not bundle_path.exists():
        # 与 /api/arena 同型的预检:bundle 缺失属于用户可修复的环境问题,
        # 400 + 构建指引,而不是 500
        return JSONResponse(
            status_code=400,
            content={"detail": BUNDLE_GUIDANCE, "reason": f"数据 bundle 不存在:{bundle_path}"},
        )
    # 延迟导入:verify 管线(进而 runner/pandas 相关)依赖不可用时保持 API 可
    # 加载,与 _register_runner_exception_handler 的降级策略一致
    from aquant.runner.backtest import BacktestExecutionError
    from aquant.verify.pipeline import layered_verification

    try:
        lv = layered_verification(
            req.code,
            req.start,
            req.end,
            bundle_path=bundle_path,
            is_ratio=req.is_ratio,
            cash=req.cash,
            benchmark=req.benchmark,
            mod_overrides=req.overrides,
        )
    except FileNotFoundError as exc:
        return JSONResponse(status_code=400, content={"detail": BUNDLE_GUIDANCE, "reason": str(exc)})
    except BacktestExecutionError as exc:
        # 单次回测子进程失败是服务端执行问题:500 + 中文 reason(简单直接)
        return JSONResponse(
            status_code=500,
            content={
                "detail": "分层验证中的回测执行失败,请稍后重试或查看服务端日志。",
                "reason": str(exc),
            },
        )
    except ValueError as exc:
        # 区间 <40 个交易日等参数问题:用户可修复 → 400
        return JSONResponse(status_code=400, content={"detail": f"分层验证参数无法执行:{exc}"})
    comparison = lv.comparison
    return {
        "status": comparison["status"],
        "comparison": comparison,
        "is_metrics": comparison["is_metrics"],
        "oos_metrics": comparison["oos_metrics"],
        "checks": comparison["checks"],
        "report": lv.to_report(),
        "random_baseline_returns": lv.random_baseline_returns,
        "paper_checklist": lv.paper_checklist,
        "is_split": lv.is_split,
        "start": lv.start,
        "end": lv.end,
    }


# ---- 前端静态托管(单容器部署模式,来源:tick-stock-panel 调研结论)-----------
# frontend/dist 由 Vite 构建产出;存在时 FastAPI 直接托管,/api/* 路由优先于
# SPA fallback。dist 不存在时 API 独立运行(纯后端模式)。
def _mount_frontend() -> None:
    from pathlib import Path

    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    dist = Path(__file__).resolve().parents[3] / "frontend" / "dist"
    assets = dist / "assets"
    if not (dist / "index.html").exists():
        return
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

    @app.get("/", include_in_schema=False)
    def _index() -> FileResponse:
        return FileResponse(dist / "index.html")

    # SPA fallback:非 /api 路径全部回落到 index.html(前端路由预留)
    @app.get("/{full_path:path}", include_in_schema=False)
    def _spa(full_path: str) -> FileResponse:
        candidate = dist / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(dist / "index.html")


_mount_frontend()
