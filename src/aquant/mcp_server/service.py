"""MCP direct 模式:本地服务分发(不经 FastAPI/HTTP,进程内直调 aquant 服务)。

设计:实现 ``service_request(method, path, body) -> (status_code, json_dict)``,
复刻 REST 端点的语义(路径、状态码、响应体形状),让 server.py 的各 tool 在
「proxy(薄桥 HTTP)/ direct(本地直调)」两种模式下共享同一套响应解析逻辑:

- proxy:`AQUANT_MCP_MODE=proxy`,沿用 httpx 转发(需先启动 uvicorn 服务);
- direct(默认):进程内直调 —— 不需要启动 REST 服务、也不需要在平台侧配置
  LLM:策略"思考"由外部 agent 框架(Claude Code / ZCode…)自身的大模型完成,
  平台只提供确定性的校验 / 回测 / 诊断 / 数据服务。

direct 模式的实现原则:
- 尽量复用 REST 层同源的领域服务(ValidationAgent / RealRunnerAdapter /
  layered_verification / run_arena / load_kline / SessionStore),保证与 Web
  端行为一致(同一套四层校验与保守参数);
- 需要"平台 LLM"的端点(/api/chat、会话 turn、/api/optimize)在未配置
  AQUANT_LLM_* 时返回 503 + 指引文本:引导 agent 走
  strategy_contract → validate → backtest → diagnose 自主闭环;
- token 闸门:direct 模式同样校验 token(AQUANT_MCP_TOKEN(S)),语义与
  token_gate 一致(fail-closed:未登记路径默认拒绝)。

测试注入点:monkeypatch 本模块的 handler 表,即可脱离真实 bundle/网络单测。
"""

from __future__ import annotations

import json
import os
from typing import Any

from aquant import __version__
from aquant.compat import version_report
from aquant.config.schema import build_run_config, default_bundle_path
from aquant.agents.backtest_agent import BacktestAgent
from aquant.agents.validation_agent import ValidationAgent
from aquant.api.adapter import RealRunnerAdapter
from aquant.validation.findings import Verdict
from aquant.validation.validator import validate_strategy

BUNDLE_GUIDANCE = (
    "回测数据 bundle 不存在,无法执行真实回测。"
    "请先通过 aquant.data 模块构建 RQAlpha bundle(真实价 + ex_cum_factor 复权因子),"
    "或用环境变量 AQUANT_BUNDLE_PATH 指向已有 bundle 目录,构建完成后重试。"
)

# 平台 LLM 三件套是否齐备(每次调用时读,便于运行时配置生效)
def _llm_configured() -> bool:
    return bool(
        os.environ.get("AQUANT_LLM_BASE_URL")
        and os.environ.get("AQUANT_LLM_API_KEY")
        and os.environ.get("AQUANT_LLM_MODEL")
    )


_AGENT_LOOP_GUIDANCE = (
    "平台未配置 LLM(AQUANT_LLM_BASE_URL/AQUANT_LLM_API_KEY/AQUANT_LLM_MODEL 三件套缺一),"
    "该端点的对话生成能力不可用。你(agent 框架内的大模型)就是策略大脑,请改用自主闭环:"
    "platform_strategy_contract 拿契约 → 自己写码 → platform_validate_code 校验 → "
    "platform_run_backtest 回测 → platform_diagnose_strategy 诊断 → 自己改码迭代 → "
    "platform_rank_strategies 排名对比;多轮会话类工具在 platform_session_edit_code + "
    "platform_session_backtest(手动存版本/回测)下同样可用。"
    "若确需平台侧生成,请先配置 AQUANT_LLM_* 并重启服务。"
)


# ---------------------------------------------------------------------------
# token 闸门(direct 模式;语义与 aquant.api.token_gate 对齐)
# ---------------------------------------------------------------------------

def _configured_tokens() -> dict[str, dict[str, Any]]:
    raw = os.environ.get("AQUANT_MCP_TOKENS")
    if raw:
        try:
            parsed = json.loads(raw)
            tokens = {}
            for token, item in parsed.items():
                tokens[token] = item if isinstance(item, dict) else {"scope": str(item)}
            return tokens
        except Exception:
            pass
    single = os.environ.get("AQUANT_MCP_TOKEN")
    if single:
        return {single: {"scope": "readwrite"}}
    return {}


# readonly 档允许的路径前缀(与 token_gate.SCOPE_ROUTES 对齐;health 本就无闸门)
_READONLY_PREFIXES = ("/api/validate", "/api/kline", "/api/health")


def _scope_for(path: str) -> str:
    return "readonly" if any(path.startswith(p) for p in _READONLY_PREFIXES) else "readwrite"


def _token_error(method: str, path: str) -> tuple[int, dict[str, Any]] | None:
    """direct 模式 token 校验:未配置 token = 本机开放;配置了则 fail-closed。"""
    tokens = _configured_tokens()
    if not tokens:
        return None
    outgoing = os.environ.get("AQUANT_MCP_TOKEN", "")
    entry = tokens.get(outgoing)
    if entry is None:
        return 401, {"detail": "token 无效或缺失(direct 模式按 AQUANT_MCP_TOKEN 校验)。"}
    scope = str(entry.get("scope", "readwrite"))
    if scope == "readonly" and _scope_for(path) != "readonly":
        return 403, {"detail": f"token scope=readonly 无权访问 {path}(只读档)。"}
    return None


# ---------------------------------------------------------------------------
# 各端点的本地实现(响应形状与 REST 端点一致)
# ---------------------------------------------------------------------------

def _h_health(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    return 200, {"status": "ok", "version": __version__, "rqalpha": version_report()}


def _h_validate(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    code = str(body.get("code") or "")
    start, end = body.get("start"), body.get("end")
    if not code.strip():
        return 422, {"detail": [{"loc": ["code"], "msg": "code 不能为空"}]}
    run_config = build_run_config(start, end) if (start and end) else None
    agent = ValidationAgent()
    verdict: Verdict = agent.validate(code, run_config)
    payload = verdict.to_dict()
    payload["summary"] = agent.summarize(verdict)
    return 200, payload


def _h_backtest(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    code = str(body.get("code") or "")
    start, end = body.get("start"), body.get("end")
    if not code.strip():
        return 422, {"detail": [{"loc": ["code"], "msg": "code 不能为空"}]}
    benchmark = body.get("benchmark")
    if benchmark is None:
        benchmark = "000001.XSHG"  # 与 REST 端点 _resolve_benchmark 的默认一致
    elif str(benchmark).strip().lower() in ("", "none"):
        benchmark = None if str(benchmark).strip().lower() == "none" else benchmark
    # 基准/标的白名单校验由 adapter 标自适应接管(bundle 缺失时自动在线获取)

    run_config = build_run_config(start, end, cash=body.get("cash"), benchmark=benchmark,
                                  user_mod_overrides=body.get("overrides"))
    agent = BacktestAgent(runner=RealRunnerAdapter())
    exec_kw: dict[str, Any] = {}
    if body.get("overrides"):
        exec_kw["mod_overrides"] = body["overrides"]
    if body.get("cash") is not None:
        exec_kw["cash"] = body["cash"]
    if benchmark:
        exec_kw["benchmark"] = benchmark
    try:
        outcome = agent.execute(code, start, end, run_config=run_config, **exec_kw)
    except FileNotFoundError as exc:
        return 400, {"detail": BUNDLE_GUIDANCE, "reason": str(exc)}

    verdict: Verdict = outcome["verdict"]
    if outcome.get("rejected"):
        payload = verdict.to_dict()
        payload["summary"] = ValidationAgent().summarize(verdict)
        return 422, {"detail": "策略校验未通过,拒绝执行回测。", "verdict": payload}

    result = outcome["result"]
    if isinstance(result, dict) and result.get("execution_error"):
        return 422, {"detail": result["execution_error"][-1200:], "execution_error": result["execution_error"]}

    from aquant.agents.orchestrator import (
        _extract_benchmark_series,
        _extract_daily_holdings,
        _extract_portfolio_series,
        _extract_run_log,
        _extract_trades,
    )

    summary = result.get("summary") if isinstance(result, dict) else getattr(result, "summary", {})
    # trades 可能是 DataFrame(6.5.0 成交明细)或列表;DataFrame 对 `or` 求真值会抛错
    trades_raw = result.get("trades") if isinstance(result, dict) else None
    trades_count = len(trades_raw) if hasattr(trades_raw, "__len__") else 0
    return 200, {
        "passed": True,
        "verdict": verdict.to_dict(),
        "metrics": summary,
        "trades_count": trades_count,
        "portfolio_series": _extract_portfolio_series(result),
        "benchmark_series": _extract_benchmark_series(result),
        "trades": _extract_trades(result),
        "run_log": _extract_run_log(result),
        "daily_holdings": _extract_daily_holdings(result),
        "bundle_extension": result.get("bundle_extension") if isinstance(result, dict) else None,
        "benchmark": benchmark,
        "benchmark_label": _benchmark_label(benchmark),
    }


def _benchmark_label(oid: str | None) -> str:
    if not oid:
        return ""
    from aquant.data.benchmark import DEFAULT_BENCHMARK_LABELS
    from aquant.data.instruments import instrument_symbol

    known = DEFAULT_BENCHMARK_LABELS.get(oid)
    if known:
        return known
    try:
        return instrument_symbol(oid, default_bundle_path()) or oid
    except Exception:
        return oid


def _h_verify(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    from aquant.runner.backtest import BacktestExecutionError
    from aquant.verify.pipeline import layered_verification

    bundle_path = default_bundle_path()
    try:
        from aquant.data.extend import bundle_calendar
        if bundle_calendar(bundle_path) is None:
            return 400, {"detail": BUNDLE_GUIDANCE, "reason": f"数据 bundle 不存在:{bundle_path}"}
    except Exception:
        return 400, {"detail": BUNDLE_GUIDANCE, "reason": str(bundle_path)}
    try:
        lv = layered_verification(
            str(body.get("code") or ""),
            str(body.get("start")),
            str(body.get("end")),
            bundle_path=bundle_path,
            is_ratio=body.get("is_ratio", 0.7),
            cash=body.get("cash"),
            benchmark=body.get("benchmark"),
            mod_overrides=body.get("overrides"),
        )
    except BacktestExecutionError as exc:
        return 500, {"detail": "分层验证中的回测执行失败,请稍后重试或查看服务端日志。", "reason": str(exc)}
    except ValueError as exc:
        return 400, {"detail": f"分层验证参数无法执行:{exc}"}
    comparison = lv.comparison
    return 200, {
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


def _h_arena(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    from aquant.verify.arena import run_arena

    candidates = body.get("candidates") or []
    if not candidates:
        return 422, {"detail": [{"loc": ["candidates"], "msg": "至少需要 1 个候选策略"}]}
    benchmark = body.get("benchmark")
    if benchmark is None:
        benchmark = "000001.XSHG"
    try:
        outcome = run_arena(
            [{"name": c["name"], "code": c["code"]} for c in candidates],
            str(body.get("start")),
            str(body.get("end")),
            cash=body.get("cash"),
            benchmark=None if str(benchmark).lower() == "none" else benchmark,
        )
    except FileNotFoundError as exc:
        return 400, {"detail": BUNDLE_GUIDANCE, "reason": str(exc)}
    except ValueError as exc:
        return 422, {"detail": str(exc)}
    from aquant.api.main import _benchmark_label_of  # 复用 REST 层的名称解析
    outcome["benchmark_label"] = _benchmark_label_of(
        None if str(benchmark).lower() == "none" else benchmark
    )
    return 200, outcome


def _h_kline(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    from aquant.api.kline import KlineError, load_kline

    try:
        candles = load_kline(
            str(body.get("oid") or ""),
            body.get("start"), body.get("end"),
        )
    except FileNotFoundError as exc:
        return 400, {"detail": BUNDLE_GUIDANCE, "reason": str(exc)}
    except KlineError as exc:
        return 400, {"detail": str(exc)}
    return 200, {"oid": body.get("oid"), "start": body.get("start"), "end": body.get("end"),
                 "candles": candles}


# ---------------------------------------------------------------------------
# 会话端点(direct 模式直连 SessionStore;LLM 相关端点按配置门禁)
# ---------------------------------------------------------------------------

_STORE: Any = None


def _store():
    global _STORE
    if _STORE is None:
        from aquant.session.store import SessionStore

        _STORE = SessionStore()
    return _STORE


def _store_guard(body: dict[str, Any]) -> tuple[int, dict[str, Any]] | None:
    """会话类请求的公共体校验:body 为 dict 且包含必要字段由各 handler 自查。"""
    return None


def _session_exists(sid: str) -> dict[str, Any] | None:
    return _store().get(sid)


def _h_session_create(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    session = _store().create(
        start=str(body.get("start")),
        end=str(body.get("end")),
        code=body.get("code"),
        title=body.get("title"),
        cash=body.get("cash"),
        benchmark=body.get("benchmark"),
    )
    return 200, {"session": session}


def _h_session_list(_body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    items = _store().list()
    return 200, {"sessions": items, "count": len(items)}


def _h_session_get(body: dict[str, Any], sid: str) -> tuple[int, dict[str, Any]]:
    session = _session_exists(sid)
    if session is None:
        return 404, {"detail": f"会话不存在:{sid}"}
    return 200, {"session": session}


def _h_session_patch(body: dict[str, Any], sid: str) -> tuple[int, dict[str, Any]]:
    if _session_exists(sid) is None:
        return 404, {"detail": f"会话不存在:{sid}"}
    session = _store().update(
        sid, title=body.get("title"), start=body.get("start"), end=body.get("end"),
        cash=body.get("cash"), benchmark=body.get("benchmark"),
    )
    return 200, {"session": session}


def _h_session_delete(body: dict[str, Any], sid: str, permanent: bool = False) -> tuple[int, dict[str, Any]]:
    store = _store()
    removed = store.purge(sid) if permanent else store.delete(sid)
    if not removed:
        return 404, {"detail": f"会话不存在:{sid}"}
    return 200, {"ok": True, "id": sid, "permanent": permanent}


def _h_session_code_put(body: dict[str, Any], sid: str) -> tuple[int, dict[str, Any]]:
    """手动编辑代码保存为新版本 + 可选回测(与 REST /api/sessions/{sid}/code 同语义)。"""
    store = _store()
    session = _session_exists(sid)
    if session is None:
        return 404, {"detail": f"会话不存在:{sid}"}
    if not str(body.get("code") or "").strip():
        return 422, {"detail": [{"loc": ["code"], "msg": "code 不能为空"}]}
    run_config = build_run_config(session["start"], session["end"], cash=session.get("cash"))
    verdict = validate_strategy(str(body["code"]), run_config)
    if verdict.has_error:
        payload = verdict.to_dict()
        payload["summary"] = ValidationAgent().summarize(verdict)
        return 422, {"message": "策略校验未通过,保存被拒绝", "verdict": payload}

    version = store.add_version(
        sid, code=str(body["code"]), source="user",
        message=str(body.get("message") or "手动保存"),
        verdict=verdict.to_dict(),
    )
    backtest_res = None
    if body.get("backtest"):
        from aquant.api.main import _backtest_version  # 直接复用 REST 层回测逻辑(store 直调)
        from fastapi import HTTPException

        try:
            backtest_res = _backtest_version(
                None, sid, str(body["code"]), store.get(sid),
                store=store, runner=RealRunnerAdapter(),
            )
        except HTTPException as exc:
            detail = exc.detail
            return exc.status_code, detail if isinstance(detail, dict) else {"detail": str(detail)}
    return 200, {"version": version, "backtest": backtest_res, "session": store.get(sid)}


def _h_session_backtest(body: dict[str, Any], sid: str) -> tuple[int, dict[str, Any]]:
    from fastapi import HTTPException
    from aquant.api.main import _backtest_version

    store = _store()
    session = _session_exists(sid)
    if session is None:
        return 404, {"detail": f"会话不存在:{sid}"}
    current = store.current(sid)
    if current is None:
        return 422, {"detail": "会话尚无策略版本,请先保存代码(session code 接口)。"}
    try:
        return 200, _backtest_version(
            None, sid, current["code"], session,
            store=store, runner=RealRunnerAdapter(),
        )
    except HTTPException as exc:
        detail = exc.detail
        return exc.status_code, detail if isinstance(detail, dict) else {"detail": str(detail)}


def _h_session_rollback(body: dict[str, Any], sid: str) -> tuple[int, dict[str, Any]]:
    store = _store()
    session = _session_exists(sid)
    if session is None:
        return 404, {"detail": f"会话不存在:{sid}"}
    version_id = str(body.get("version_id") or "")
    try:
        return 200, {"session": store.rollback(sid, version_id)}
    except KeyError as exc:
        return 404, {"detail": str(exc)}


def _h_session_versions(body: dict[str, Any], sid: str) -> tuple[int, dict[str, Any]]:
    if _session_exists(sid) is None:
        return 404, {"detail": f"会话不存在:{sid}"}
    versions = _store().list_versions(sid)
    return 200, {"versions": versions, "count": len(versions)}


def _h_session_diff(body: dict[str, Any], sid: str) -> tuple[int, dict[str, Any]]:
    from aquant.session.diff import diff_codes

    store = _store()
    if _session_exists(sid) is None:
        return 404, {"detail": f"会话不存在:{sid}"}
    from_v = str(body.get("from_version") or "")
    to_v = str(body.get("to_version") or "")
    from_snap = store.get_version(sid, from_v)
    to_snap = store.get_version(sid, to_v) if to_v else None
    if from_snap is None or (to_v and to_snap is None):
        return 404, {"detail": f"版本不存在:{from_v} / {to_v}"}
    from_code = from_snap["code"]
    to_code = to_snap["code"] if to_snap else (store.current(sid) or {}).get("code", "")
    diff = diff_codes(from_code, to_code)
    return 200, {"from_version": from_v, "to_version": to_v, **diff}


def _h_session_turns(body: dict[str, Any], sid: str) -> tuple[int, dict[str, Any]]:
    if _session_exists(sid) is None:
        return 404, {"detail": f"会话不存在:{sid}"}
    turns = _store().get_turns(sid)
    return 200, {"turns": turns, "count": len(turns)}


def _h_session_turn_add(body: dict[str, Any], sid: str) -> tuple[int, dict[str, Any]]:
    if _session_exists(sid) is None:
        return 404, {"detail": f"会话不存在:{sid}"}
    role = str(body.get("role") or "")
    if role not in ("user", "agent", "note"):
        return 422, {"detail": [{"loc": ["role"], "msg": "role 必须是 user/agent/note"}]}
    turn = _store().add_turn(sid, role=role, text=str(body.get("text") or ""),
                             version_id=body.get("version_id"))
    return 200, {"turn": turn}


def _h_session_turn_update(body: dict[str, Any], sid: str, tid: str) -> tuple[int, dict[str, Any]]:
    store = _store()
    if _session_exists(sid) is None:
        return 404, {"detail": f"会话不存在:{sid}"}
    try:
        turn = store.update_turn(sid, tid, text=body.get("text"), role=body.get("role"),
                                 version_id=body.get("version_id"))
        return 200, {"turn": turn}
    except KeyError as exc:
        return 404, {"detail": str(exc)}


def _h_session_turn_delete(body: dict[str, Any], sid: str, tid: str) -> tuple[int, dict[str, Any]]:
    store = _store()
    if _session_exists(sid) is None:
        return 404, {"detail": f"会话不存在:{sid}"}
    try:
        store.delete_turn(sid, tid)
        return 200, {"ok": True, "id": tid}
    except KeyError as exc:
        return 404, {"detail": str(exc)}


def _h_session_version_patch(body: dict[str, Any], sid: str, vid: str) -> tuple[int, dict[str, Any]]:
    store = _store()
    if _session_exists(sid) is None:
        return 404, {"detail": f"会话不存在:{sid}"}
    try:
        version = store.update_version_meta(sid, vid, message=body.get("message"))
        return 200, {"version": version}
    except KeyError as exc:
        return 404, {"detail": str(exc)}


def _h_session_version_delete(body: dict[str, Any], sid: str, vid: str) -> tuple[int, dict[str, Any]]:
    store = _store()
    if _session_exists(sid) is None:
        return 404, {"detail": f"会话不存在:{sid}"}
    try:
        store.delete_version(sid, vid)
        return 200, {"ok": True, "id": vid}
    except KeyError as exc:
        return 404, {"detail": str(exc)}


# 需要"平台 LLM"的端点:direct 模式下按配置门禁
def _h_needs_llm(endpoint: str):
    def handler(body: dict[str, Any], *rest: str) -> tuple[int, dict[str, Any]]:
        if not _llm_configured():
            return 503, {"detail": _AGENT_LOOP_GUIDANCE}
        # 已配置 LLM:引导走 REST 服务(重 orchestration 场景请启动 uvicorn,proxy 模式)
        return 501, {
            "detail": (
                f"direct 模式不支持 {endpoint} 的完整编排(需要 FastAPI 请求上下文)。"
                "两种选择:1)继续用 agent 自主闭环(见 platform_strategy_contract / "
                "platform_workflow_guide);2)AQUANT_MCP_MODE=proxy 并启动 uvicorn 服务后重试。"
            ),
        }
    return handler


# ---------------------------------------------------------------------------
# 路由表与分发入口
# ---------------------------------------------------------------------------

# (method, path, path_params) → handler;body 为解析后的 JSON dict
_ROUTES: dict[tuple[str, str], Any] = {
    ("GET", "/api/health"): _h_health,
    ("POST", "/api/validate"): _h_validate,
    ("POST", "/api/backtest"): _h_backtest,
    ("POST", "/api/verify"): _h_verify,
    ("POST", "/api/arena"): _h_arena,
    ("GET", "/api/kline"): _h_kline,
    ("POST", "/api/sessions"): _h_session_create,
    ("GET", "/api/sessions"): _h_session_list,
    ("POST", "/api/chat"): _h_needs_llm("/api/chat"),
    ("POST", "/api/optimize"): _h_needs_llm("/api/optimize"),
}

# 带路径参数的路由(前缀匹配,按注册顺序)
_PARAM_ROUTES: list[tuple[str, str, Any]] = [
    ("POST", "/api/sessions/{sid}/code", _h_session_code_put),
    ("POST", "/api/sessions/{sid}/backtest", _h_session_backtest),
    ("POST", "/api/sessions/{sid}/rollback", _h_session_rollback),
    ("GET", "/api/sessions/{sid}/versions", _h_session_versions),
    ("GET", "/api/sessions/{sid}/diff", _h_session_diff),
    ("GET", "/api/sessions/{sid}/turns", _h_session_turns),
    ("POST", "/api/sessions/{sid}/turns", _h_needs_llm("session turn")),
    ("GET", "/api/sessions/{sid}", _h_session_get),
    ("PATCH", "/api/sessions/{sid}", _h_session_patch),
    ("DELETE", "/api/sessions/{sid}", _h_session_delete),
    ("POST", "/api/sessions/{sid}/turns/message", _h_session_turn_add),
    ("PATCH", "/api/sessions/{sid}/turns/{tid}", _h_session_turn_update),
    ("DELETE", "/api/sessions/{sid}/turns/{tid}", _h_session_turn_delete),
    ("PATCH", "/api/sessions/{sid}/versions/{vid}", _h_session_version_patch),
    ("DELETE", "/api/sessions/{sid}/versions/{vid}", _h_session_version_delete),
]


def service_request(method: str, path: str, body: dict[str, Any] | None = None,
                    *, query: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """direct 模式统一入口:复刻 REST 端点语义,返回 (status_code, json)。

    :param path: 如 /api/backtest 或 /api/sessions/{sid}/backtest(路径参数已代入)
    """
    body = dict(body or {})
    if query:
        body = {**{k: v for k, v in query.items()}, **body}

    token_err = _token_error(method, path)
    if token_err is not None:
        return token_err

    handler = _ROUTES.get((method, path))
    if handler is None:
        for route_method, prefix, route_handler in _PARAM_ROUTES:
            if method != route_method:
                continue
            pattern_parts = prefix.strip("/").split("/")
            path_parts = path.strip("/").split("/")
            if len(pattern_parts) != len(path_parts):
                continue
            params: dict[str, str] = {}
            matched = True
            for pp, part in zip(pattern_parts, path_parts):
                if pp.startswith("{") and pp.endswith("}"):
                    params[pp[1:-1]] = part
                elif pp != part:
                    matched = False
                    break
            if matched:
                import inspect

                sig = inspect.signature(route_handler)
                kwargs: dict[str, Any] = {"body": body}
                kwargs.update(params)
                # query 参数合并进 body(diff 的 from_version/to_version 走 query)
                if route_handler is _h_session_diff:
                    if query:
                        body.setdefault("from_version", query.get("from_version"))
                        body.setdefault("to_version", query.get("to_version"))
                kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}
                return route_handler(**kwargs)
        return 404, {"detail": f"未知端点:{method} {path}(direct 模式未实现,可用 proxy 模式)"}
    return handler(body)
