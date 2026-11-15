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

mcp = _MCPServer(
    name="aquant-platform",
    title="AQ-Platform 量化回测平台",
    description=(
        "AQ-Platform(A股回测正确性平台)的 MCP 桥。"
        "通过本地 FastAPI 完成:健康检查、策略代码校验、真实回测。"
    ),
    instructions=(
        "本 server 是本地量化平台的薄桥。典型用法:"
        "先 platform_health 确认后端可用;写/改策略代码后用 platform_validate_code "
        "做 Layer 1 校验(返回 findings 可指导修改);校验通过后用 platform_run_backtest "
        "执行真实回测。若返回文本中包含错误或校验 findings,请按提示修正参数/代码后重试,"
        "不要向用户原样抛出堆栈。"
    ),
)

# 测试注入点:默认 None(真实网络);测试可赋值为 httpx.MockTransport(handler)
_TRANSPORT: httpx.BaseTransport | None = None


def _api_base() -> str:
    """后端地址:每次请求时读环境变量,便于测试与部署覆盖。"""
    return os.environ.get("AQUANT_API_BASE", DEFAULT_API_BASE).rstrip("/")


def _build_client() -> httpx.Client:
    """构造 httpx 客户端;_TRANSPORT 非 None 时(测试)走 MockTransport。"""
    return httpx.Client(transport=_TRANSPORT, timeout=REQUEST_TIMEOUT_SECONDS)


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
) -> httpx.Response:
    """向后端发一次 HTTP 请求;配置了 token 时附带 X-MCP-Token 头。"""
    headers: dict[str, str] = {}
    token = _outgoing_token()
    if token:
        headers["X-MCP-Token"] = token
    with _build_client() as client:
        return client.request(
            method,
            _api_base() + path,
            json=json_payload,
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


def main() -> None:
    """以 stdio transport 启动 MCP server(python -m aquant.mcp_server.server 入口)。"""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
