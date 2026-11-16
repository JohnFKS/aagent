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
from aquant.compat import version_report
from aquant.agents.backtest_agent import BacktestAgent
from aquant.agents.conversation import run_edit_turn
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
def health() -> dict[str, Any]:
    """健康检查:附带当前/支持的 RQAlpha 版本,升级前后一眼可核对。"""
    return {"status": "ok", "version": __version__, "rqalpha": version_report()}


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
        # 执行配置:cash 与 overrides 都需透传给 runner(run_config 仅用于校验,
        # BacktestAgent.execute 不会自动从中提取 cash)。
        exec_kw: dict[str, Any] = {}
        if req.overrides:
            exec_kw["mod_overrides"] = req.overrides
        if req.cash is not None:
            exec_kw["cash"] = req.cash
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
    # 运行时崩溃透出:静态校验通过但撮合阶段崩溃(如 history_bars 单字段请求返回一维
    # ndarray 还按字段名取列)。把错误文本返回给调用方(外部 agent 据此自纠),
    # 否则 metrics=null 而外部 agent 无从得知原因。
    exec_error = result.get("execution_error") if isinstance(result, dict) else getattr(result, "execution_error", None)
    if exec_error:
        return JSONResponse(
            status_code=422,
            content={
                "detail": (
                    "策略通过静态校验,但回测执行阶段崩溃。"
                    "常见原因:history_bars 单字段请求(如 fields='close')返回一维 ndarray,"
                    "直接参与运算即可,不要再按字段名取列(多字段请求才返回结构化数组);"
                    "下单前判断数组长度;检查标的当日是否停牌。"
                ),
                "execution_error": str(exec_error)[-2000:],
            },
        )
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


# ---------------------------------------------------------------------------
# AI 自动优化闭环(用户需求:"agent 可以根据代码以及回测结果自动调整策略,
# 包括但不限于超参数、策略代码和策略逻辑,从而达到更好的效果"):POST /api/optimize
# 用户传入已就绪策略代码,StrategyOptimizerAgent 按轮次执行"回测 → 诊断 →
# LLM 改码 → 校验(重试)→ 回测(自愈)→ 对比保留",返回 baseline/best/轮次历史。
# 轮内失败(LLM/校验/执行)不抛:记入 rounds[].error 继续下一轮,早停由 agent 控制。
# 注意:必须注册在前端静态托管(_mount_frontend 的 SPA catch-all)之前,
# 路由按注册顺序匹配,否则 /api/optimize 会被 index.html fallback 遮蔽。
from aquant.agents.optimizer import StrategyOptimizerAgent
from aquant.validation.validator import validate_strategy


class OptimizeRequest(BaseModel):
    """/api/optimize 请求体:已就绪策略代码 + 回测区间 + 轮次预算。"""

    code: str = Field(min_length=1, description="待优化的 RQAlpha 策略源码")
    start: str = Field(description="回测起始日 YYYY-MM-DD")
    end: str = Field(description="回测结束日 YYYY-MM-DD")
    rounds: int = Field(
        default=2, ge=1, le=5,
        description="优化轮次预算(1~5,默认 2);连续 2 轮无改进自动早停",
    )
    cash: float | None = Field(default=None, description="初始资金,缺省用平台默认")
    overrides: dict | None = Field(
        default=None,
        description=(
            "用户自选回测参数覆盖(dict[str, dict]):更保守的值放行,"
            "激进值(如零滑点)被 Layer 1 校验拒绝(422)"
        ),
    )


@app.post("/api/optimize", dependencies=[Depends(require_mcp_token(scope="readwrite"))])
def optimize(req: OptimizeRequest, request: Request) -> dict[str, Any]:
    """AI 自动优化闭环:回测-诊断-LLM 改码-对比保留,200 返回 OptimizationResult。

    耗时预期:每轮 = 1 次 LLM 生成(约 30~120s)+ 2 次回测(每次数秒);
    rounds 上限 5,整体响应可能需要数分钟,调用方请设置足够长的读超时。
    错误语义与既有端点对齐:
    - 传入代码含 ERROR(validate_strategy 与 editor 同款代码级校验)→ 422 + verdict;
    - 数据 bundle 缺失(FileNotFoundError)→ 400 + BUNDLE_GUIDANCE 构建指引;
    - LLM 服务不可用(LLMAPIError 逃逸时兜底)→ 502 + 中文指引。
    经验库:与 /api/chat 同款 app.state 惰性单例,优化结束 record 最终 best。
    """
    verdict = validate_strategy(req.code)
    if verdict.has_error:
        payload = verdict.to_dict()
        payload["summary"] = ValidationAgent().summarize(verdict)
        return JSONResponse(
            status_code=422,
            content={"detail": "策略校验未通过,拒绝进入优化。", "verdict": payload},
        )
    runner = _get_runner(request)
    if isinstance(runner, RealRunnerAdapter):
        # 真实适配器在执行时从 default_bundle_path() 解析 bundle;缺失属于用户可
        # 修复的环境问题,预检直接给 400 + 指引(与 /api/arena 同型)
        bundle_path = default_bundle_path()
        if not bundle_path.exists():
            return JSONResponse(
                status_code=400,
                content={"detail": BUNDLE_GUIDANCE, "reason": f"数据 bundle 不存在:{bundle_path}"},
            )
    store = getattr(request.app.state, "experience", None)
    if store is None:
        from aquant.agents.experience import ExperienceStore

        store = ExperienceStore()
        request.app.state.experience = store
    agent = StrategyOptimizerAgent(_get_llm(request), validate_strategy)
    try:
        return agent.optimize(
            req.code,
            req.start,
            req.end,
            runner=runner,
            rounds=req.rounds,
            cash=req.cash,
            mod_overrides=req.overrides,
            experience=store,
        )
    except FileNotFoundError as exc:
        # bundle 缺失属于用户可修复的环境问题,400 + 指引而非 500
        return JSONResponse(status_code=400, content={"detail": BUNDLE_GUIDANCE, "reason": str(exc)})
    except LLMAPIError as exc:
        # LLM 网络故障(超时/断连/限流)是外部依赖问题,502 + 中文指引而非 500
        return JSONResponse(status_code=502, content={"detail": "LLM 服务暂时不可用,请稍后重试。", "reason": str(exc)})


# ---- 多轮对话会话:策略全程可见/可编辑/可回退/可对比 ------------------------
# 与单轮 /api/chat 并存:会话把每轮对话与每次策略变更都落为版本快照,
# 回退只移动 current 指针,历史永不删除;diff 由后端 difflib 统一计算。
class SessionCreateRequest(BaseModel):
    start: str = Field(description="回测起始日 YYYY-MM-DD")
    end: str = Field(description="回测结束日 YYYY-MM-DD")
    code: str | None = Field(default=None, description="初始策略代码(空则首轮由 Agent 生成)")
    title: str | None = Field(default=None, description="会话标题")
    cash: float | None = Field(default=None, description="初始资金,缺省用平台默认")


class SessionUpdateRequest(BaseModel):
    title: str | None = None
    start: str | None = None
    end: str | None = None
    cash: float | None = None


class SessionTurnRequest(BaseModel):
    text: str = Field(min_length=1, description="本轮对话要求(自然语言)")
    backtest: bool = Field(default=True, description="修改后是否立即回测")


class SessionCodeRequest(BaseModel):
    code: str = Field(min_length=1, description="手动编辑后的完整策略源码")
    message: str | None = Field(default=None, description="本次修改说明")
    backtest: bool = Field(default=False, description="保存后是否立即回测")


class SessionRollbackRequest(BaseModel):
    version_id: str = Field(description="目标历史版本 id(如 v2)")


class SessionTurnUpdateRequest(BaseModel):
    text: str | None = Field(default=None, description="修改后的对话文本")
    role: str | None = Field(default=None, description="角色 user/agent(可选)")
    version_id: str | None = Field(default=None, description="关联版本 id(可选)")
    clear_version_id: bool = Field(default=False, description="True 则解除与版本的关联")


class SessionTurnCreateRequest(BaseModel):
    role: str = Field(description="消息角色:user / agent / note(笔记)")
    text: str = Field(min_length=1, description="消息文本")
    version_id: str | None = Field(default=None, description="可选关联版本 id")


class SessionVersionUpdateRequest(BaseModel):
    message: str | None = Field(default=None, description="修改后的版本说明")
    analysis: str | None = Field(default=None, description="修改后的分析说明")
    source: str | None = Field(default=None, description="版本来源(initial/agent/user/rollback)")


def _get_store(request: Request):
    """会话仓库依赖:app.state.sessions 可注入临时目录(测试),缺省 ~/.aquant/sessions。"""
    if getattr(request.app.state, "sessions", None) is None:
        from aquant.session import SessionStore

        request.app.state.sessions = SessionStore()
    return request.app.state.sessions


def _require_session(request: Request, sid: str) -> dict[str, Any]:
    session = _get_store(request).get(sid)
    if session is None:
        raise HTTPException(status_code=404, detail=f"会话不存在:{sid}")
    return session


def _session_metrics_summary(metrics: dict[str, Any] | None) -> str:
    if not metrics:
        return "未取得回测指标。"
    parts = []
    for key, label in (("annualized_returns", "年化"), ("sharpe", "夏普"), ("max_drawdown", "回撤")):
        value = metrics.get(key)
        if isinstance(value, (int, float)):
            parts.append(f"{label} {value:.4f}")
    return " / ".join(parts) or "回测已完成。"


def _backtest_version(
    request: Request, sid: str, code: str, session: dict[str, Any]
) -> dict[str, Any]:
    """回测指定代码并把指标写回会话当前版本;校验不过抛 422 结构化错误。"""
    from aquant.agents.orchestrator import _extract_metrics, _extract_portfolio_series

    start, end = session["start"], session["end"]
    run_config = build_run_config(start, end, cash=session.get("cash"))
    verifier = ValidationAgent()
    verdict = verifier.validate(code, run_config)
    if verdict.has_error:
        payload = verdict.to_dict()
        payload["summary"] = verifier.summarize(verdict)
        raise HTTPException(status_code=422, detail={"message": "策略校验未通过,拒绝回测", "verdict": payload})
    try:
        exec_kw: dict[str, Any] = {}
        if session.get("cash") is not None:
            exec_kw["cash"] = session["cash"]
        outcome = BacktestAgent(_get_runner(request)).execute(
            code, start, end, run_config=run_config, **exec_kw
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=f"{BUNDLE_GUIDANCE} 原始错误:{exc}") from exc
    if outcome["rejected"]:
        payload = outcome["verdict"].to_dict()
        payload["summary"] = verifier.summarize(outcome["verdict"])
        raise HTTPException(status_code=422, detail={"message": outcome["reason"], "verdict": payload})
    metrics = _extract_metrics(outcome["result"], start, end)
    series = _extract_portfolio_series(outcome["result"])
    current_id = str(session.get("current_version") or "")
    if current_id:
        _get_store(request).update_version(
            sid, current_id,
            metrics=metrics, verdict=verdict.to_dict(),
            backtest={"portfolio_series": series, "metrics": metrics},
        )
    return {"metrics": metrics, "portfolio_series": series, "verdict": verdict.to_dict()}


@app.get("/api/sessions", dependencies=[Depends(require_mcp_token(scope="readwrite"))])
def list_sessions(request: Request) -> dict[str, Any]:
    items = _get_store(request).list()
    return {"sessions": items, "count": len(items)}


@app.post("/api/sessions", dependencies=[Depends(require_mcp_token(scope="readwrite"))])
def create_session(req: SessionCreateRequest, request: Request) -> dict[str, Any]:
    session = _get_store(request).create(
        start=req.start, end=req.end, code=req.code, title=req.title, cash=req.cash
    )
    return {"session": session}


@app.get("/api/sessions/{sid}", dependencies=[Depends(require_mcp_token(scope="readwrite"))])
def get_session(sid: str, request: Request) -> dict[str, Any]:
    return {"session": _require_session(request, sid)}


@app.patch("/api/sessions/{sid}", dependencies=[Depends(require_mcp_token(scope="readwrite"))])
def update_session(sid: str, req: SessionUpdateRequest, request: Request) -> dict[str, Any]:
    _require_session(request, sid)
    session = _get_store(request).update(
        sid, title=req.title, start=req.start, end=req.end, cash=req.cash
    )
    return {"session": session}


@app.delete("/api/sessions/{sid}", dependencies=[Depends(require_mcp_token(scope="readwrite"))])
def delete_session(request: Request, sid: str, permanent: bool = False) -> dict[str, Any]:
    """删除会话:默认软删除(移入 .trash/ 可归档);permanent=true 永久抹除不可恢复。"""
    store = _get_store(request)
    removed = store.purge(sid) if permanent else store.delete(sid)
    if not removed:
        raise HTTPException(status_code=404, detail=f"会话不存在:{sid}")
    return {"ok": True, "id": sid, "permanent": permanent}


@app.post("/api/sessions/{sid}/turns", dependencies=[Depends(require_mcp_token(scope="readwrite"))])
def session_turn(sid: str, req: SessionTurnRequest, request: Request) -> dict[str, Any]:
    """一轮对话:记录用户消息 → Agent 修改(首轮生成)→ 校验 → 回测 → 落版本与 Agent 消息。"""
    store = _get_store(request)
    session = _require_session(request, sid)
    current = store.current(sid)
    history = store.get_turns(sid)
    store.add_turn(sid, role="user", text=req.text)

    llm = _get_llm(request)
    runner = _get_runner(request) if req.backtest else None
    experience = getattr(request.app.state, "experience", None)
    if experience is None:
        from aquant.agents.experience import ExperienceStore

        experience = ExperienceStore()
        request.app.state.experience = experience

    try:
        if current is None:
            # 首轮无既有策略:走既有从零生成管线(自带 review)。
            outcome = run_conversation(
                req.text, session["start"], session["end"],
                llm=llm, runner=runner, experience=experience,
                cash=session.get("cash"),
            )
            outcome.setdefault("analysis", "首轮由 Agent 根据需求生成初始策略。")
        else:
            outcome = run_edit_turn(
                req.text, current["code"], history, session["start"], session["end"],
                llm=llm, runner=runner, experience=experience,
                metrics=current.get("metrics"), cash=session.get("cash"),
                backtest=req.backtest,
            )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=f"{BUNDLE_GUIDANCE} 原始错误:{exc}") from exc
    except LLMAPIError as exc:
        raise HTTPException(status_code=502, detail=f"LLM 服务暂时不可用:{exc}") from exc

    version_id = None
    if outcome.get("ok") and outcome.get("code"):
        version = store.add_version(
            sid,
            code=outcome["code"],
            source="agent",
            message=req.text,
            analysis=outcome.get("analysis") or "",
            metrics=outcome.get("metrics"),
            verdict=outcome.get("verdict"),
            backtest={
                "steps": outcome.get("steps"),
                "portfolio_series": outcome.get("portfolio_series"),
                "report": outcome.get("report"),
            },
        )
        version_id = version["id"]
        reply = outcome.get("analysis") or "已按需求修改策略。"
        reply = f"{reply}\n{_session_metrics_summary(outcome.get('metrics'))}"
    else:
        reply = f"本轮未采纳修改:{outcome.get('error') or '校验未通过'}"
    store.add_turn(
        sid, role="agent", text=reply, version_id=version_id,
        result={
            "ok": outcome.get("ok"), "rejected": outcome.get("rejected"),
            "steps": outcome.get("steps"), "metrics": outcome.get("metrics"),
            "verdict": outcome.get("verdict"), "error": outcome.get("error"),
        },
    )
    outcome["version_id"] = version_id
    outcome["session"] = store.get(sid)
    return outcome


# ---- 对话消息 CRUD(历史对话可增删改查) -----------------------------------
@app.get("/api/sessions/{sid}/turns", dependencies=[Depends(require_mcp_token(scope="readwrite"))])
def list_turns(sid: str, request: Request) -> dict[str, Any]:
    _require_session(request, sid)
    turns = _get_store(request).get_turns(sid)
    return {"turns": turns, "count": len(turns)}


@app.get("/api/sessions/{sid}/turns/{tid}", dependencies=[Depends(require_mcp_token(scope="readwrite"))])
def get_turn(sid: str, tid: str, request: Request) -> dict[str, Any]:
    _require_session(request, sid)
    turn = _get_store(request).get_turn(sid, tid)
    if turn is None:
        raise HTTPException(status_code=404, detail=f"对话消息不存在:{tid}")
    return {"turn": turn}


@app.post("/api/sessions/{sid}/turns/message", dependencies=[Depends(require_mcp_token(scope="readwrite"))])
def create_turn_message(sid: str, req: SessionTurnCreateRequest, request: Request) -> dict[str, Any]:
    """手工追加一条对话消息(role=user/agent/note),不触发 Agent 与回测。"""
    _require_session(request, sid)
    store = _get_store(request)
    if req.version_id and store.get_version(sid, req.version_id) is None:
        raise HTTPException(status_code=404, detail=f"关联版本不存在:{req.version_id}")
    try:
        turn = store.add_turn(
            sid, role=req.role, text=req.text, version_id=req.version_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"turn": turn, "session": store.get(sid)}


@app.patch("/api/sessions/{sid}/turns/{tid}", dependencies=[Depends(require_mcp_token(scope="readwrite"))])
def update_turn(sid: str, tid: str, req: SessionTurnUpdateRequest, request: Request) -> dict[str, Any]:
    _require_session(request, sid)
    store = _get_store(request)
    if req.version_id and store.get_version(sid, req.version_id) is None:
        raise HTTPException(status_code=404, detail=f"关联版本不存在:{req.version_id}")
    try:
        turn = store.update_turn(
            sid, tid, text=req.text, role=req.role,
            version_id=req.version_id, clear_version_id=req.clear_version_id,
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=404 if isinstance(exc, KeyError) else 422,
                            detail=str(exc)) from exc
    return {"turn": turn, "session": store.get(sid)}


@app.delete("/api/sessions/{sid}/turns/{tid}", dependencies=[Depends(require_mcp_token(scope="readwrite"))])
def delete_turn(sid: str, tid: str, request: Request) -> dict[str, Any]:
    _require_session(request, sid)
    store = _get_store(request)
    try:
        removed = store.delete_turn(sid, tid)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True, "turn": removed, "session": store.get(sid)}


@app.put("/api/sessions/{sid}/code", dependencies=[Depends(require_mcp_token(scope="readwrite"))])
def edit_session_code(sid: str, req: SessionCodeRequest, request: Request) -> dict[str, Any]:
    """手动编辑当前策略:校验通过后落为新版本(source=user),可选择立即回测。"""
    from aquant.validation.validator import validate_strategy

    store = _get_store(request)
    session = _require_session(request, sid)
    verdict = validate_strategy(req.code)
    if verdict.has_error:
        payload = verdict.to_dict()
        payload["summary"] = ValidationAgent().summarize(verdict)
        raise HTTPException(
            status_code=422,
            detail={"message": "手动编辑的代码未通过 Layer 1 校验", "verdict": payload},
        )
    version = store.add_version(
        sid, code=req.code, source="user",
        message=req.message or "手动编辑策略代码",
        analysis=req.message or "",
    )
    backtest_result = None
    if req.backtest:
        backtest_result = _backtest_version(request, sid, req.code, store.get(sid))
    store.add_turn(
        sid, role="user", text=f"[手动编辑] {req.message or '更新策略代码'}",
        version_id=version["id"],
    )
    return {"version": version, "backtest": backtest_result, "session": store.get(sid)}


@app.post("/api/sessions/{sid}/backtest", dependencies=[Depends(require_mcp_token(scope="readwrite"))])
def backtest_session(sid: str, request: Request) -> dict[str, Any]:
    """回测会话当前版本(原地更新指标,不新增版本),便于编辑后快速验证。"""
    store = _get_store(request)
    session = _require_session(request, sid)
    current = store.current(sid)
    if current is None:
        raise HTTPException(status_code=400, detail="会话尚无策略代码,请先发起对话或手动编辑。")
    result = _backtest_version(request, sid, current["code"], session)
    store.add_turn(
        sid, role="agent",
        text=f"[手动回测] {_session_metrics_summary(result['metrics'])}",
        version_id=current["id"], result=result,
    )
    return {**result, "session": store.get(sid)}


@app.post("/api/sessions/{sid}/rollback", dependencies=[Depends(require_mcp_token(scope="readwrite"))])
def rollback_session(sid: str, req: SessionRollbackRequest, request: Request) -> dict[str, Any]:
    """回退到历史版本:不删除任何快照,current 指针可再次前进。"""
    store = _get_store(request)
    _require_session(request, sid)
    try:
        target = store.rollback(sid, req.version_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    store.add_turn(
        sid, role="agent", text=f"[回退] 已切换到版本 {req.version_id}",
        version_id=req.version_id,
    )
    return {"version": target, "session": store.get(sid)}


@app.get("/api/sessions/{sid}/versions", dependencies=[Depends(require_mcp_token(scope="readwrite"))])
def list_versions(sid: str, request: Request) -> dict[str, Any]:
    _require_session(request, sid)
    versions = _get_store(request).list_versions(sid)
    return {"versions": versions, "count": len(versions)}


@app.get("/api/sessions/{sid}/versions/{vid}", dependencies=[Depends(require_mcp_token(scope="readwrite"))])
def get_version(sid: str, vid: str, request: Request) -> dict[str, Any]:
    _require_session(request, sid)
    version = _get_store(request).get_version(sid, vid)
    if version is None:
        raise HTTPException(status_code=404, detail=f"版本不存在:{vid}")
    return {"version": version}


@app.patch("/api/sessions/{sid}/versions/{vid}", dependencies=[Depends(require_mcp_token(scope="readwrite"))])
def update_version(sid: str, vid: str, req: SessionVersionUpdateRequest, request: Request) -> dict[str, Any]:
    """修改版本说明/分析/来源(代码快照不可原地改,改代码请用 PUT /code 新建版本)。"""
    _require_session(request, sid)
    store = _get_store(request)
    try:
        version = store.update_version_meta(
            sid, vid, message=req.message, analysis=req.analysis, source=req.source,
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=404 if isinstance(exc, KeyError) else 422,
                            detail=str(exc)) from exc
    return {"version": version, "session": store.get(sid)}


@app.delete("/api/sessions/{sid}/versions/{vid}", dependencies=[Depends(require_mcp_token(scope="readwrite"))])
def delete_version(sid: str, vid: str, request: Request) -> dict[str, Any]:
    """永久删除单个策略版本;若为当前版本则 current 回落到父版本。"""
    _require_session(request, sid)
    store = _get_store(request)
    try:
        removed = store.delete_version(sid, vid)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True, "version": removed, "session": store.get(sid)}


@app.get("/api/sessions/{sid}/diff", dependencies=[Depends(require_mcp_token(scope="readwrite"))])
def diff_versions(
    request: Request,
    sid: str,
    from_version: str,
    to_version: str | None = None,
) -> dict[str, Any]:
    """两个版本的代码对比;to_version 缺省为当前版本。"""
    from aquant.session import diff_codes

    _require_session(request, sid)
    store = _get_store(request)
    to_version = to_version or str(store.get(sid).get("current_version") or "")
    old = store.get_version(sid, from_version)
    new = store.get_version(sid, to_version)
    if old is None or new is None:
        raise HTTPException(
            status_code=404,
            detail=f"版本不存在:from={from_version}, to={to_version}",
        )
    return diff_codes(
        old["code"], new["code"],
        from_label=f"{from_version} {old.get('message', '')}"[:60],
        to_label=f"{to_version} {new.get('message', '')}"[:60],
    )


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
