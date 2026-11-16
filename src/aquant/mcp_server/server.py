"""AQ-Platform MCP server(薄桥模式,stdio)。

设计取自 microsoft 生态调研中 tick-stock-panel 的 mcp-server 先例:
- 本模块只做「MCP tool ↔ 本地 FastAPI」的 HTTP 转发,tools 内不 import
  aquant.runner / aquant.agents 等重依赖(纯薄桥,回测逻辑全部留在后端 API);
- 非 2xx 与连接失败一律返回「可读错误文本」而不抛异常,让 LLM 依据文本自行纠错
  (例如校验被拦截时把 findings 摘要拼进返回文本,LLM 可据此修改策略代码重试);
- 后端地址默认 http://127.0.0.1:8000,可用环境变量 AQUANT_API_BASE 覆盖;
- 可选 token:AQUANT_MCP_TOKEN 非空时,每个请求自动附加 X-MCP-Token 头;
  未设置但 AQUANT_MCP_TOKENS 配置了多 token 时,默认取第一个 readwrite token
  (与后端 aquant.api.token_gate 的多 token 闸门配对);都没有则不带头
  (本机单用户模式)。

运行方式(stdio):
    .venv/bin/python -m aquant.mcp_server.server

测试注入点:monkeypatch 本模块的 ``_TRANSPORT`` 为 httpx.MockTransport,
即可在不启动真实后端的情况下覆盖全部 tool 的行为。
"""

import json
import os
from typing import Any

import httpx

# mcp SDK:2.x 将 FastMCP 更名为 MCPServer(mcp.server.mcpserver);
# 1.x 仍为 FastMCP。这里做一次兼容导入,两代 SDK 均可用。
try:  # mcp >= 2.x
    from mcp.server.mcpserver import MCPServer as _MCPServer
except ImportError:  # pragma: no cover - mcp 1.x 兼容
    from mcp.server.fastmcp import FastMCP as _MCPServer  # type: ignore[no-redef]

DEFAULT_API_BASE = "http://127.0.0.1:8000"
REQUEST_TIMEOUT_SECONDS = 60.0
# 长耗时端点的读超时:/api/verify(两次回测+随机基准,5~20s)、/api/arena
# (并行回测,10~60s)、/api/optimize(每轮 = LLM 生成 + 两次回测,2 轮常见
# 3~8 分钟,读超时必须 ≥900s)。
VERIFY_TIMEOUT_SECONDS = 120.0
ARENA_TIMEOUT_SECONDS = 180.0
OPTIMIZE_TIMEOUT_SECONDS = 900.0

mcp = _MCPServer(
    name="aquant-platform",
    title="AQ-Platform 量化回测平台",
    description=(
        "AQ-Platform(A股回测正确性平台)的 MCP 桥。"
        "通过本地 FastAPI 完成:健康检查、策略代码校验、真实回测、AI 优化、"
        "分层验证(防过拟合)、多策略竞技场、K 线行情查询。"
    ),
    instructions=(
        "本 server 是本地量化平台的薄桥。典型用法:"
        "先 platform_health 确认后端可用;写/改策略代码后用 platform_validate_code "
        "做 Layer 1 校验(返回 findings 可指导修改);校验通过后用 platform_run_backtest "
        "执行真实回测;platform_optimize 做 AI 优化、platform_verify_layered 做分层验证"
        "(防过拟合必做)、platform_arena 做多策略排名、platform_get_kline 查行情。"
        "不确定如何开始时先调用 platform_workflow_guide 获取七步工作流指南。"
        "若返回文本中包含错误或校验 findings,请按提示修正参数/代码后重试,"
        "不要向用户原样抛出堆栈。"
    ),
)

# 测试注入点:默认 None(真实网络);测试可赋值为 httpx.MockTransport(handler)
_TRANSPORT: httpx.BaseTransport | None = None


def _api_base() -> str:
    """后端地址:每次请求时读环境变量,便于测试与部署覆盖。"""
    return os.environ.get("AQUANT_API_BASE", DEFAULT_API_BASE).rstrip("/")


def _build_client(timeout: float = REQUEST_TIMEOUT_SECONDS) -> httpx.Client:
    """构造 httpx 客户端;_TRANSPORT 非 None 时(测试)走 MockTransport。"""
    return httpx.Client(transport=_TRANSPORT, timeout=timeout)


def _outgoing_token() -> str:
    """MCP 出站 token(每请求读取,与后端 token_gate 闸门配对)。

    优先级:AQUANT_MCP_TOKEN(单 token 兼容)> AQUANT_MCP_TOKENS 中第一个
    readwrite token(多 token 配置时,本机桥默认用读写档,否则退而取第一个)>
    空串(不附加 X-MCP-Token 头,本机开放模式)。
    """
    single = os.environ.get("AQUANT_MCP_TOKEN", "").strip()
    if single:
        return single
    multi = os.environ.get("AQUANT_MCP_TOKENS", "").strip()
    if not multi:
        return ""
    try:
        raw = json.loads(multi)
    except json.JSONDecodeError:
        return ""
    if not isinstance(raw, dict) or not raw:
        return ""
    for key, value in raw.items():
        scope = value.get("scope", "readwrite") if isinstance(value, dict) else value
        if str(scope).strip() == "readwrite":
            return str(key).strip()
    return str(next(iter(raw))).strip()


def _json_text(data: Any) -> str:
    """JSON 转可读文本(中文不转义)。"""
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def _request(
    method: str,
    path: str,
    *,
    json_payload: dict[str, Any] | None = None,
    params: dict[str, str] | None = None,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
) -> httpx.Response:
    """向后端发一次 HTTP 请求;配置了 token 时附带 X-MCP-Token 头。

    timeout 按端点耗时选择(默认 60s;optimize 用 900s 长读超时);
    params 为 GET/POST 共用的 query 参数。
    """
    headers: dict[str, str] = {}
    token = _outgoing_token()
    if token:
        headers["X-MCP-Token"] = token
    with _build_client(timeout) as client:
        return client.request(
            method,
            _api_base() + path,
            json=json_payload,
            params=params,
            headers=headers,
        )


def _findings_summary(data: dict[str, Any]) -> str:
    """从 verdict/payload 中提炼 findings 一行摘要,供 LLM 自纠。"""
    parts: list[str] = []
    error_count = data.get("error_count")
    warning_count = data.get("warning_count")
    if error_count is not None:
        parts.append(f"{error_count} 个错误")
    if warning_count is not None:
        parts.append(f"{warning_count} 个警告")
    rules = [
        str(f.get("rule"))
        for f in data.get("findings", [])
        if isinstance(f, dict) and f.get("rule")
    ]
    line = "校验结果:" + (", ".join(parts) if parts else "未提供计数")
    if rules:
        line += ";触发规则:" + ", ".join(rules)
    return line


def _detail_text(data: Any) -> str:
    """把响应体里的 detail 字段(fastapi 的 str 或 pydantic 的 list)转成可读文本。"""
    if data is None:
        return "(响应体不是 JSON)"
    if isinstance(data, str):
        return data
    return _json_text(data)


def _error_text(
    tool: str,
    method: str,
    path: str,
    resp: httpx.Response,
    hint: str = "",
) -> str:
    """非 2xx → 可读错误文本(不抛异常),LLM 依据文本自纠。"""
    lines = [
        f"[{tool}] 平台请求失败:{method} {path} 返回 HTTP {resp.status_code}。"
    ]
    try:
        data: Any = resp.json()
    except Exception:  # noqa: BLE001 - 任何解析失败都降级为纯文本
        data = None
    if isinstance(data, dict):
        if "detail" in data:
            lines.append(f"detail:{_detail_text(data['detail'])}")
        if "reason" in data:
            lines.append(f"reason:{_detail_text(data['reason'])}")
        verdict = data.get("verdict")
        if isinstance(verdict, dict):
            lines.append(_findings_summary(verdict))
        elif data.get("findings") or data.get("passed") is False:
            lines.append(_findings_summary(data))
    if hint:
        lines.append(f"建议:{hint}")
    return "\n".join(lines)


def _transport_error_text(tool: str, exc: Exception) -> str:
    """连接失败/超时 → 可读文本,指引 LLM 或用户先检查后端。"""
    return (
        f"[{tool}] 无法连接量化平台 API({_api_base()})。"
        f"请确认后端已启动(例如:uvicorn aquant.api.main:app --port 8000),"
        f"或用环境变量 AQUANT_API_BASE 指向正确地址后再重试。"
        f"原始错误:{type(exc).__name__}: {exc}"
    )


VALIDATE_HINT = (
    "请检查参数:code 必须为非空策略源码字符串,start/end 需为 YYYY-MM-DD 日期,修正后重试;"
    "若 detail 中是校验 findings,请据此修改策略代码(常见问题:T+1、未来函数 shift(-1)、"
    "涨跌停/手续费缺失、零滑点等)后重新调用。"
)
BACKTEST_HINT = (
    "请先调用 platform_validate_code 确认代码可过 Layer 1 校验,再修正问题后重试;"
    "若 detail 提示 bundle 缺失,请先构建 RQAlpha 数据 bundle 或设置 AQUANT_BUNDLE_PATH。"
)
TOKEN_HINT = (
    "若 detail 为 401/鉴权失败,说明平台启用了 token 闸门"
    "(AQUANT_MCP_TOKEN 或 AQUANT_MCP_TOKENS 配置),"
    "需要正确的 X-MCP-Token 后才能调用。"
)
OPTIMIZE_HINT = (
    "HTTP 422 为校验拦截,请先调用 platform_validate_code 并按 findings 修改代码后重试;"
    "detail 提示 bundle 缺失时,请先构建 RQAlpha 数据 bundle 或设置 AQUANT_BUNDLE_PATH;"
    "HTTP 502 为 LLM 服务故障(外部依赖),请稍后重试或检查服务端 LLM 配置;"
    "401/403/429 为 token 闸门(鉴权/权限/限流),需正确 X-MCP-Token 或稍后再试。"
)
VERIFY_HINT = (
    "HTTP 422 为校验拦截,请先调用 platform_validate_code 并按 findings 修改代码后重试;"
    "detail 提示 bundle 缺失时,请先构建 RQAlpha 数据 bundle 或设置 AQUANT_BUNDLE_PATH;"
    "detail 提示区间不足时,请扩大 start/end(分层验证要求区间 ≥40 个交易日,"
    "is_ratio 限 0.5~0.9);401/403/429 为 token 闸门,需正确 X-MCP-Token 或稍后再试。"
)
KLINE_HINT = (
    "oid 需为 bundle 内合约的 order_book_id(形如 000001.XSHE / 600000.XSHG),"
    "start/end 需为 YYYY-MM-DD 日期;detail 提示 bundle 缺失时,请先构建 RQAlpha "
    "数据 bundle 或设置 AQUANT_BUNDLE_PATH 后重试。"
)
ARENA_HINT = (
    "candidates 需为 1~6 个 {name, code} 对象且候选名唯一;"
    "候选代码建议先过 platform_validate_code 提高入围率;"
    "detail 提示 bundle 缺失时,请先构建 RQAlpha 数据 bundle 或设置 AQUANT_BUNDLE_PATH;"
    "401/403/429 为 token 闸门,需正确 X-MCP-Token 或稍后再试。"
)


def _validation_rejected_text(
    tool: str,
    resp: httpx.Response,
    action: str,
    hint: str,
) -> str:
    """422 校验拦截 → verdict/findings 摘要拼进可读文本(不抛异常)。

    与 platform_run_backtest 的 422 分支同语义:verdict 的 findings 摘要
    指导 LLM 改码自纠,完整 verdict JSON 仍在文本中可继续解析。
    """
    try:
        data: Any = resp.json()
    except Exception:  # noqa: BLE001 - 任何解析失败都降级为纯文本
        data = None
    lines = [f"[{tool}] {action}被校验拦截(HTTP 422):策略校验未通过。"]
    if isinstance(data, dict):
        verdict = data.get("verdict")
        if isinstance(verdict, dict):
            lines.append(_findings_summary(verdict))
            lines.append("verdict JSON:\n" + _json_text(verdict))
        elif "detail" in data:
            lines.append(f"detail:{_detail_text(data['detail'])}")
    lines.append(f"建议:{hint}")
    return "\n".join(lines)


@mcp.tool(
    description=(
        "健康检查:查询量化平台后端是否可用。"
        "无参数;转发 GET /api/health,返回 {\"status\", \"version\"} JSON 文本。"
        "在调用校验/回测前建议先用本工具确认后端已启动。"
    )
)
def platform_health() -> str:
    """转发 GET /api/health;失败时返回可读错误文本。"""
    try:
        resp = _request("GET", "/api/health")
    except Exception as exc:  # noqa: BLE001 - 薄桥不向 LLM 抛异常
        return _transport_error_text("platform_health", exc)
    if resp.is_success:
        try:
            return _json_text(resp.json())
        except Exception:  # noqa: BLE001
            return f"平台返回了非 JSON 响应(HTTP {resp.status_code}):{resp.text}"
    return _error_text(
        "platform_health", "GET", "/api/health", resp, hint=TOKEN_HINT
    )


@mcp.tool(
    description=(
        "校验策略代码(Layer 1 静态检查,不回测)。"
        "参数:code(必填,RQAlpha 策略源码字符串);"
        "start/end(可选,YYYY-MM-DD,两者同时提供时做含回测配置的完整校验)。"
        "转发 POST /api/validate,返回 {passed, error_count, warning_count, findings, summary} "
        "JSON 文本;校验未通过时 findings/summary 会指出具体问题(如未来函数、T+1 违规),"
        "请按提示修改代码后重试。请求异常时返回可读错误文本。"
    )
)
def platform_validate_code(code: str, start: str | None = None, end: str | None = None) -> str:
    """转发 POST /api/validate;2xx 返回 verdict JSON 文本,非 2xx 返回可读错误文本。"""
    payload: dict[str, Any] = {"code": code}
    if start is not None:
        payload["start"] = start
    if end is not None:
        payload["end"] = end
    try:
        resp = _request("POST", "/api/validate", json_payload=payload)
    except Exception as exc:  # noqa: BLE001
        return _transport_error_text("platform_validate_code", exc)
    if resp.is_success:
        try:
            data: Any = resp.json()
        except Exception:  # noqa: BLE001
            return f"平台返回了非 JSON 响应(HTTP {resp.status_code}):{resp.text}"
        if isinstance(data, dict) and data.get("passed") is False:
            # 校验拦截(200 内的 passed=False):把 findings 摘要拼进返回文本
            return f"校验未通过,{_findings_summary(data)}。\n完整结果 JSON:\n{_json_text(data)}"
        return _json_text(data)
    return _error_text(
        "platform_validate_code", "POST", "/api/validate", resp, hint=VALIDATE_HINT
    )


@mcp.tool(
    description=(
        "执行真实回测(代码需已通过 Layer 1 校验,后端会再做一次硬门禁)。"
        "参数:code(必填,RQAlpha 策略源码);start/end(必填,YYYY-MM-DD 回测区间);"
        "cash(可选,初始资金,缺省用平台默认)。"
        "转发 POST /api/backtest,成功返回 {passed, verdict, metrics, trades_count} JSON 文本;"
        "HTTP 422 表示策略校验未通过(返回文本含 findings 摘要与 verdict),"
        "HTTP 400 表示数据 bundle 缺失(返回文本含构建指引)。"
        "请依据返回文本修正代码或环境后重试。"
    )
)
def platform_run_backtest(code: str, start: str, end: str, cash: float | None = None) -> str:
    """转发 POST /api/backtest;200 返回 metrics JSON,422/400 返回结构化可读文本。"""
    payload: dict[str, Any] = {"code": code, "start": start, "end": end}
    if cash is not None:
        payload["cash"] = cash
    try:
        resp = _request("POST", "/api/backtest", json_payload=payload)
    except Exception as exc:  # noqa: BLE001
        return _transport_error_text("platform_run_backtest", exc)
    if resp.is_success:
        try:
            return _json_text(resp.json())
        except Exception:  # noqa: BLE001
            return f"平台返回了非 JSON 响应(HTTP {resp.status_code}):{resp.text}"
    if resp.status_code == 422:
        # 校验拦截:422 + verdict(findings 摘要拼进文本,LLM 改代码后重试)
        try:
            data: Any = resp.json()
        except Exception:  # noqa: BLE001
            data = None
        lines = ["回测被校验拦截(HTTP 422):策略校验未通过,拒绝执行回测。"]
        if isinstance(data, dict):
            verdict = data.get("verdict")
            if isinstance(verdict, dict):
                lines.append(_findings_summary(verdict))
                lines.append("verdict JSON:\n" + _json_text(verdict))
            elif "detail" in data:
                lines.append(f"detail:{_detail_text(data['detail'])}")
        lines.append(f"建议:{BACKTEST_HINT}")
        return "\n".join(lines)
    return _error_text(
        "platform_run_backtest", "POST", "/api/backtest", resp, hint=BACKTEST_HINT
    )


@mcp.tool(
    description=(
        "AI 自动优化策略(闭环:回测→诊断→LLM 改码→校验→回测→对比保留)。"
        "参数:code(必填,已通过 Layer 1 校验的策略源码);start/end(必填,YYYY-MM-DD 回测区间);"
        "rounds(可选,优化轮次预算 1~5,默认 2,连续 2 轮无改进自动早停);"
        "cash(可选,初始资金);overrides(可选,回测参数覆盖 dict)。"
        "转发 POST /api/optimize,返回 {improved, best:{code,summary}, baseline, rounds[], "
        "elapsed_seconds} JSON 文本:best.code 为历史最优代码,rounds[] 为每轮演进史,"
        "improved=False 表示无改进(保留原代码)。"
        "耗时预期:每轮 = 1 次 LLM 生成 + 2 次回测,2 轮常见 3~8 分钟"
        "(读超时已放宽到 900s),期间请耐心等待,勿中断重试。"
        "错误语义:HTTP 422 校验拦截(返回文本含 findings 摘要,请先改代码);"
        "HTTP 400 bundle 缺失(含构建指引);HTTP 502 LLM 服务故障(稍后重试)。"
        "配合:优化完成后建议继续 platform_verify_layered 验证 best.code 防过拟合;"
        "多个候选版本横向对比可用 platform_arena。"
    )
)
def platform_optimize(
    code: str,
    start: str,
    end: str,
    rounds: int = 2,
    cash: float | None = None,
    overrides: dict | None = None,
) -> str:
    """转发 POST /api/optimize;200 返回优化结果 JSON,422/400/502 返回可读错误文本。"""
    payload: dict[str, Any] = {"code": code, "start": start, "end": end, "rounds": rounds}
    if cash is not None:
        payload["cash"] = cash
    if overrides is not None:
        payload["overrides"] = overrides
    try:
        resp = _request(
            "POST", "/api/optimize", json_payload=payload, timeout=OPTIMIZE_TIMEOUT_SECONDS
        )
    except Exception as exc:  # noqa: BLE001 - 薄桥不向 LLM 抛异常
        return _transport_error_text("platform_optimize", exc)
    if resp.is_success:
        try:
            return _json_text(resp.json())
        except Exception:  # noqa: BLE001
            return f"平台返回了非 JSON 响应(HTTP {resp.status_code}):{resp.text}"
    if resp.status_code == 422:
        # 校验拦截:422 + verdict(findings 摘要拼进文本,LLM 改码后重试)
        return _validation_rejected_text("platform_optimize", resp, "优化", OPTIMIZE_HINT)
    return _error_text(
        "platform_optimize", "POST", "/api/optimize", resp, hint=OPTIMIZE_HINT
    )


@mcp.tool(
    description=(
        "分层验证(防过拟合必做):样本内/样本外切分两次回测 + 随机基准对照 + 判定"
        "+ 模拟盘引导清单。参数:code(必填,RQAlpha 策略源码);start/end(必填,"
        "YYYY-MM-DD,整个区间需 ≥40 个交易日);is_ratio(可选,样本内占比 0.5~0.9,"
        "默认 0.7);overrides(可选,回测参数覆盖 dict)。"
        "转发 POST /api/verify,返回 {status, comparison, checks, report, "
        "random_baseline_returns, paper_checklist, ...} JSON 文本:"
        "status=pass 表示样本外仍优于随机基准才可信,status=reject 请回读 checks/report 修正策略。"
        "耗时预期:两次回测 + 随机基准对照,约 5~20 秒。"
        "错误语义:HTTP 422 校验拦截(返回文本含 findings 摘要);"
        "HTTP 400 bundle 缺失或区间过短(含指引)。"
        "配合:platform_optimize 产出的 best.code 应再用本工具验证一次;"
        "验证通过后的多策略对比可用 platform_arena。"
    )
)
def platform_verify_layered(
    code: str,
    start: str,
    end: str,
    is_ratio: float = 0.7,
    overrides: dict | None = None,
) -> str:
    """转发 POST /api/verify;200 返回分层验证结果 JSON,422/400 返回可读错误文本。"""
    payload: dict[str, Any] = {"code": code, "start": start, "end": end, "is_ratio": is_ratio}
    if overrides is not None:
        payload["overrides"] = overrides
    try:
        resp = _request(
            "POST", "/api/verify", json_payload=payload, timeout=VERIFY_TIMEOUT_SECONDS
        )
    except Exception as exc:  # noqa: BLE001 - 薄桥不向 LLM 抛异常
        return _transport_error_text("platform_verify_layered", exc)
    if resp.is_success:
        try:
            return _json_text(resp.json())
        except Exception:  # noqa: BLE001
            return f"平台返回了非 JSON 响应(HTTP {resp.status_code}):{resp.text}"
    if resp.status_code == 422:
        return _validation_rejected_text(
            "platform_verify_layered", resp, "分层验证", VERIFY_HINT
        )
    return _error_text(
        "platform_verify_layered", "POST", "/api/verify", resp, hint=VERIFY_HINT
    )


@mcp.tool(
    description=(
        "查询真实价日线 K 线(行情观察用,不执行回测)。"
        "参数:oid(必填,合约 order_book_id,形如 000001.XSHE / 600000.XSHG);"
        "start/end(必填,YYYY-MM-DD,用于缩小日期范围)。"
        "转发 GET /api/kline,返回 {oid, start, end, candles:[{date, open, close, "
        "high, low, volume}]} JSON 文本;最多 500 根,超出时等距抽样并保留首末两根。"
        "耗时预期:直接读本地数据 bundle,通常 <1 秒。"
        "错误语义:HTTP 400 表示 oid 不在 bundle 或 bundle 缺失(返回文本含指引)。"
        "配合:写策略/挑标的时先用本工具观察量价形态;回测结果异常时也可对照行情核查;"
        "策略验证仍需走 platform_validate_code + platform_run_backtest 流程。"
    )
)
def platform_get_kline(oid: str, start: str, end: str) -> str:
    """转发 GET /api/kline(query 参数);200 返回 candles JSON,400 返回可读错误文本。"""
    try:
        resp = _request("GET", "/api/kline", params={"oid": oid, "start": start, "end": end})
    except Exception as exc:  # noqa: BLE001 - 薄桥不向 LLM 抛异常
        return _transport_error_text("platform_get_kline", exc)
    if resp.is_success:
        try:
            return _json_text(resp.json())
        except Exception:  # noqa: BLE001
            return f"平台返回了非 JSON 响应(HTTP {resp.status_code}):{resp.text}"
    return _error_text("platform_get_kline", "GET", "/api/kline", resp, hint=KLINE_HINT)


@mcp.tool(
    description=(
        "多策略竞技场:1~6 个候选策略并行回测并按(年化收益, 夏普)降序排名"
        "(养蛊式大逃杀)。参数:candidates(必填,列表,元素为 {name: 候选名, "
        "code: 策略源码},1~6 个且候选名唯一);start/end(必填,YYYY-MM-DD 回测区间)。"
        "转发 POST /api/arena,返回 {ranking, table, details, ...} JSON 文本。"
        "校验不过的候选 status=rejected、执行失败的 status=failed,只淘汰自己、不中断全场。"
        "耗时预期:并行回测,约 10~60 秒(候选越多越久)。"
        "错误语义:HTTP 422 参数不合法(候选数量/字段);HTTP 400 候选名重复或 bundle 缺失"
        "(返回文本含指引)。"
        "配合:提交前先用 platform_validate_code 预检各候选可提高入围率;"
        "冠军代码建议再做 platform_verify_layered 防过拟合。"
    )
)
def platform_arena(candidates: list[dict[str, str]], start: str, end: str) -> str:
    """转发 POST /api/arena;200 返回排名结果 JSON,422/400 返回可读错误文本。"""
    payload: dict[str, Any] = {"candidates": candidates, "start": start, "end": end}
    try:
        resp = _request(
            "POST", "/api/arena", json_payload=payload, timeout=ARENA_TIMEOUT_SECONDS
        )
    except Exception as exc:  # noqa: BLE001 - 薄桥不向 LLM 抛异常
        return _transport_error_text("platform_arena", exc)
    if resp.is_success:
        try:
            return _json_text(resp.json())
        except Exception:  # noqa: BLE001
            return f"平台返回了非 JSON 响应(HTTP {resp.status_code}):{resp.text}"
    return _error_text("platform_arena", "POST", "/api/arena", resp, hint=ARENA_HINT)


WORKFLOW_GUIDE = """\
AQ-Platform 外部 agent 工作流指南(七步全流程:
策略编辑 → 校验 → 回测 → 优化 → 分层验证 → 竞技场 → 归档)

本平台通过 MCP 工具(platform_*)驱动本地 FastAPI 后端。不确定如何开始时按下面
七步执行;每一步的返回文本若含错误或校验 findings,按提示修正后重试,不要向用户
原样抛出堆栈。

第 1 步 建项目目录
  为本次任务创建项目目录,统一结构:
    <项目目录>/strategies/   # 策略源码 .py 文件
    <项目目录>/reports/      # 回测/验证/竞技场结果(JSON 或文本)

第 2 步 写策略代码(保存到 strategies/)
  rqalpha 最小模板要点:init(context) 中设置目标标的与参数(如均线窗口),
  handle_bar(context, bar_dict) 中用 history_bars 取行情、order_shares/order_value
  下单、用 logger 记录信号。
  平台 API 约束(违反会被 Layer 1 校验拦截):
  - history_bars(...) 返回 numpy ndarray(不是 DataFrame),按 [行, 列] 索引,
    如 [..., 0] 取最新收盘价序列;
  - get_position(order_book_id) 需配合 POSITION_DIRECTION.LONG / POSITION_DIRECTION.SHORT
    指明方向;
  - 禁止外部数据源(网络请求、读文件)与墙钟(datetime.now()/time.time()),
    只允许使用 bundle 行情与回测时钟。

第 3 步 校验到 0 error:platform_validate_code(code, start, end)
  Layer 1 静态检查;error_count=0 且无 ERROR 级 findings 才算通过,
  否则按 findings 修改代码后重试。

第 4 步 回测:platform_run_backtest(code, start, end, cash?)
  校验通过后执行真实回测,读取 metrics(年化收益/夏普/最大回撤等),把结果存入 reports/。

第 5 步 优化:platform_optimize(code, start, end, rounds=2)
  AI 闭环"回测→诊断→LLM 改码→对比保留",耗时数分钟;采用返回的 best.code 作为
  新策略代码(baseline 为原版,rounds[] 为演进史;improved=False 表示无改进,
  保留原代码)。

第 6 步 分层验证(防过拟合必做):platform_verify_layered(best.code, start, end, is_ratio=0.7)
  样本内/样本外对比 + 随机基准对照 + 模拟盘清单;优化后的策略应再做一次,
  status=pass 才可信,status=reject 时回读 checks/report 修正策略。

第 7 步 竞技与归档
  多策略对比:platform_arena(candidates=[{name, code}], start, end) 并行回测排名
  (1~6 个候选,校验不过的候选 rejected 只淘汰自己);需要观察行情时用
  platform_get_kline(oid, start, end)。
  把最终采用的策略代码存入 strategies/,把回测/验证/竞技场结果存入 reports/,
  并向用户汇报最终结论。

多轮对话模式(策略全程可见可编辑可回退,适合持续迭代同一份策略):
  1) platform_session_create(start, end, code?) 建会话(拿到 sid;
     code 留空则首轮由 Agent 生成);
  2) platform_session_turn(sid, text) 逐轮用自然语言要求修改策略,每轮自动
     校验+回测+落版本;用 platform_session_get(sid) 查看全部对话与版本;
  3) platform_session_edit_code(sid, code) 手动改写代码也落版本;
     platform_session_backtest(sid) 可随时回测当前版本;
  4) platform_session_diff(sid, from_version, to_version) 对比任意两版变更;
     platform_session_rollback(sid, version_id) 回退(历史永不删除,可再次前进)。
  对话修正以用户要求为准,指标好坏由回测与版本对比判断,不强行采纳"指标更优"版本。

历史对话与版本的增删改查(永久删除需谨慎):
  - 对话:platform_session_turns 查列表;platform_session_turn_add 手工追加消息;
    platform_session_turn_update 改消息文本/角色/关联版本;
    platform_session_turn_delete 永久删除单条消息。
  - 策略版本:platform_session_versions/vid 查;platform_session_version_update 改说明;
    platform_session_version_delete 永久删除单个版本(当前版本回落父版本)。
  - 整个会话:前端「删除」是软删除(移入 .trash/ 可归档);
    platform_session_purge 是不可恢复的永久删除(连同策略与全部对话)。
"""


@mcp.tool(
    description=(
        "返回 AQ-Platform 外部 agent 工作流指南(七步全流程);不确定如何开始时先调用本工具。"
    )
)
def platform_workflow_guide() -> str:
    """返回静态七步工作流文本(不转发后端,无网络请求)。"""
    return WORKFLOW_GUIDE


# ---------------------------------------------------------------------------
# 多轮对话会话工具:策略全程可见/可编辑/可回退/可对比
# ---------------------------------------------------------------------------
SESSION_HINT = (
    "400 表示会话尚无策略代码或参数问题(先 platform_session_turn 生成,或 edit_code 写入);"
    "404 表示会话/版本不存在(先 platform_session_create 或核对 id);"
    "422 表示策略校验未通过,请按 verdict findings 修正后重试;"
    "502 表示 LLM 服务故障,稍后重试;401/403/429 为 token 闸门。"
)


def _session_text(
    tool: str,
    method: str,
    path: str,
    *,
    json_payload: dict[str, Any] | None = None,
    params: dict[str, str] | None = None,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
) -> str:
    """会话族工具统一转发:非 2xx / 连接失败均返回可读中文文本,不抛异常。"""
    try:
        resp = _request(method, path, json_payload=json_payload, params=params, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - 薄桥契约
        return _transport_error_text(tool, exc)
    if resp.is_success:
        try:
            return _json_text(resp.json())
        except Exception:  # noqa: BLE001
            return f"平台返回了非 JSON 响应(HTTP {resp.status_code}):{resp.text}"
    return _error_text(tool, method, path, resp, hint=SESSION_HINT)


@mcp.tool(
    description=(
        "新建多轮对话会话,用于持续迭代同一份策略。参数:start/end(必填,YYYY-MM-DD);"
        "code(可选,初始策略代码;留空则首轮由 Agent 生成);title(可选);cash(可选)。"
        "返回 session(含 id、current_version)。后续用 platform_session_turn 继续对话。"
    )
)
def platform_session_create(
    start: str,
    end: str,
    code: str | None = None,
    title: str | None = None,
    cash: float | None = None,
) -> str:
    payload: dict[str, Any] = {"start": start, "end": end}
    if code is not None:
        payload["code"] = code
    if title is not None:
        payload["title"] = title
    if cash is not None:
        payload["cash"] = cash
    return _session_text("platform_session_create", "POST", "/api/sessions", json_payload=payload)


@mcp.tool(
    description=(
        "列出全部会话摘要(id/标题/区间/版本数/消息数/更新时间),按更新时间倒序。"
        "无参数。拿到 sid 后用 platform_session_get 查看完整对话与版本历史。"
    )
)
def platform_session_list() -> str:
    return _session_text("platform_session_list", "GET", "/api/sessions")


@mcp.tool(
    description=(
        "查看会话全量内容:对话消息(turns)、全部策略版本快照(versions,含完整代码/指标/分析)、"
        "当前版本 current_version。参数:sid(必填,会话 id)。"
    )
)
def platform_session_get(sid: str) -> str:
    return _session_text("platform_session_get", "GET", f"/api/sessions/{sid}")


@mcp.tool(
    description=(
        "在会话中发起一轮对话:Agent 根据文字要求修改当前策略 → Layer 1 校验 → 回测 → "
        "落为新版本并记录对话。参数:sid(必填),text(必填,自然语言修改要求),"
        "backtest(可选,默认 True;False 则只改代码不回测)。"
        "耗时预期:每轮 = 1 次 LLM 生成 + 1 次回测,常见 30~180 秒"
        "(读超时 900s)。返回 ok/steps/analysis/metrics/version_id/session。"
        "校验不过时 ok=False 且 error 可读;历史版本不会丢失,可随时 platform_session_rollback。"
    )
)
def platform_session_turn(sid: str, text: str, backtest: bool = True) -> str:
    return _session_text(
        "platform_session_turn", "POST", f"/api/sessions/{sid}/turns",
        json_payload={"text": text, "backtest": backtest},
        timeout=OPTIMIZE_TIMEOUT_SECONDS,
    )


@mcp.tool(
    description=(
        "手动编辑会话当前策略(全程可见可编辑):校验通过后落为 source=user 新版本。"
        "参数:sid(必填),code(必填,完整策略源码),message(可选,修改说明),"
        "backtest(可选,默认 False;True 则保存后立即回测)。"
        "返回 version/backtest/session。"
    )
)
def platform_session_edit_code(
    sid: str,
    code: str,
    message: str | None = None,
    backtest: bool = False,
) -> str:
    payload: dict[str, Any] = {"code": code, "backtest": backtest}
    if message is not None:
        payload["message"] = message
    return _session_text(
        "platform_session_edit_code", "PUT", f"/api/sessions/{sid}/code",
        json_payload=payload, timeout=VERIFY_TIMEOUT_SECONDS,
    )


@mcp.tool(
    description=(
        "对会话当前版本执行回测(原地更新指标,不新增版本)。参数:sid(必填)。"
        "耗时预期:一次真实回测约数秒~20 秒。返回 metrics/portfolio_series/session。"
    )
)
def platform_session_backtest(sid: str) -> str:
    return _session_text(
        "platform_session_backtest", "POST", f"/api/sessions/{sid}/backtest",
        timeout=VERIFY_TIMEOUT_SECONDS,
    )


@mcp.tool(
    description=(
        "回退会话到历史版本:不删除任何快照,current_version 指针可再次前进。"
        "参数:sid(必填),version_id(必填,如 v2)。返回 version/session。"
        "回退后可用 platform_session_diff 对比任意两个版本。"
    )
)
def platform_session_rollback(sid: str, version_id: str) -> str:
    return _session_text(
        "platform_session_rollback", "POST", f"/api/sessions/{sid}/rollback",
        json_payload={"version_id": version_id},
    )


@mcp.tool(
    description=(
        "列出会话的全部策略版本摘要(含来源/说明/指标/行数,不含完整代码)。"
        "参数:sid(必填)。配合 platform_session_diff 做变更对比。"
    )
)
def platform_session_versions(sid: str) -> str:
    return _session_text("platform_session_versions", "GET", f"/api/sessions/{sid}/versions")


@mcp.tool(
    description=(
        "对比会话中两个版本的策略代码,返回结构化行级 diff(rows: context/add/remove)、"
        "unified diff 文本与增删统计。参数:sid(必填),from_version(必填),"
        "to_version(可选,缺省为当前版本)。"
    )
)
def platform_session_diff(sid: str, from_version: str, to_version: str | None = None) -> str:
    params = {"from_version": from_version}
    if to_version is not None:
        params["to_version"] = to_version
    return _session_text(
        "platform_session_diff", "GET", f"/api/sessions/{sid}/diff", params=params
    )


@mcp.tool(
    description=(
        "永久删除会话(不可恢复):抹除会话 JSON 与其归档,策略和对话一并删除。"
        "参数:sid(必填)。与软删除(平台前端的'删除'按钮)不同,此操作不可撤销。"
    )
)
def platform_session_purge(sid: str) -> str:
    return _session_text(
        "platform_session_purge", "DELETE", f"/api/sessions/{sid}",
        params={"permanent": "true"},
    )


@mcp.tool(
    description=(
        "列出会话的全部对话消息(增删改查之'查')。参数:sid(必填)。"
        "返回 turns(id/role/text/version_id/created_at)与 count。"
    )
)
def platform_session_turns(sid: str) -> str:
    return _session_text("platform_session_turns", "GET", f"/api/sessions/{sid}/turns")


@mcp.tool(
    description=(
        "手工追加一条对话消息(不触发 Agent 与回测)。参数:sid(必填),"
        "role(必填,user/agent/note),text(必填),version_id(可选,关联版本)。"
    )
)
def platform_session_turn_add(sid: str, role: str, text: str, version_id: str | None = None) -> str:
    payload: dict[str, Any] = {"role": role, "text": text}
    if version_id is not None:
        payload["version_id"] = version_id
    return _session_text(
        "platform_session_turn_add", "POST", f"/api/sessions/{sid}/turns/message",
        json_payload=payload,
    )


@mcp.tool(
    description=(
        "修改历史对话消息(增删改查之'改')。参数:sid(必填),tid(必填,消息 id 如 t1);"
        "可选 text(新文本)、role(user/agent/note)、version_id、clear_version_id(True 解除关联)。"
    )
)
def platform_session_turn_update(
    sid: str,
    tid: str,
    text: str | None = None,
    role: str | None = None,
    version_id: str | None = None,
    clear_version_id: bool = False,
) -> str:
    payload: dict[str, Any] = {"clear_version_id": clear_version_id}
    for key, value in (("text", text), ("role", role), ("version_id", version_id)):
        if value is not None:
            payload[key] = value
    return _session_text(
        "platform_session_turn_update", "PATCH", f"/api/sessions/{sid}/turns/{tid}",
        json_payload=payload,
    )


@mcp.tool(
    description=(
        "永久删除单条对话消息。参数:sid(必填),tid(必填,消息 id)。不可恢复。"
    )
)
def platform_session_turn_delete(sid: str, tid: str) -> str:
    return _session_text(
        "platform_session_turn_delete", "DELETE", f"/api/sessions/{sid}/turns/{tid}"
    )


@mcp.tool(
    description=(
        "修改策略版本的说明/分析/来源(代码不可原地改,改代码用 platform_session_edit_code 新建版本)。"
        "参数:sid(必填),vid(必填,版本 id 如 v2),可选 message/analysis/source。"
    )
)
def platform_session_version_update(
    sid: str,
    vid: str,
    message: str | None = None,
    analysis: str | None = None,
    source: str | None = None,
) -> str:
    payload: dict[str, Any] = {}
    for key, value in (("message", message), ("analysis", analysis), ("source", source)):
        if value is not None:
            payload[key] = value
    return _session_text(
        "platform_session_version_update", "PATCH", f"/api/sessions/{sid}/versions/{vid}",
        json_payload=payload,
    )


@mcp.tool(
    description=(
        "永久删除单个策略版本(不可恢复)。参数:sid(必填),vid(必填,版本 id)。"
        "若删的是当前版本,current 自动回落到父版本;历史 diff 会随版本删除而消失。"
    )
)
def platform_session_version_delete(sid: str, vid: str) -> str:
    return _session_text(
        "platform_session_version_delete", "DELETE", f"/api/sessions/{sid}/versions/{vid}"
    )


def main() -> None:
    """以 stdio transport 启动 MCP server(python -m aquant.mcp_server.server 入口)。"""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
