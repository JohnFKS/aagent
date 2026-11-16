"""MCP/API token 闸门:多 token + scope 规则表 + 每 token 限流。

docs/design.md §1.3 tick-stock-panel 调研采纳项(scope 门禁)的完整版,
替代 main.py 中「单一共享 token」的简化闸门。单机单用户产品的务实多 token 支持:

token 存储(优先级从高到低;每请求惰性读取环境变量,便于测试 setenv):
1. ``AQUANT_MCP_TOKENS``:JSON 字符串,多 token 表,如::

       '{"tkn_xxx": {"scope": "readwrite", "name": "mcp默认", "rpm": 60}}'

   value 也可简写为 scope 字符串(如 ``'{"tkn_yyy": "readonly"}'``)。
   该变量非空时即为权威 token 表(其余来源忽略);JSON 非法/顶层不是对象时
   按 fail-closed 处理(所有请求 401,stderr 告警)——配置坏了绝不能静默放开。
2. ``AQUANT_MCP_TOKEN``:单 token 向后兼容,等价于 scope=readwrite 的表项。
3. 都未设置 → 闸门关闭(本机开放模式,现状行为:完全放行且不限流)。

scope 两档(规则表见 ``SCOPE_ROUTES``):
- readonly:仅 /api/validate(/api/health 本就无闸门,列在表中仅为规则完整,
  因此 readonly token 实际能调用的只有 validate);
- readwrite:全部端点。
扩展方式见 ``SCOPE_ROUTES`` / ``_SCOPE_LEVEL`` 处注释。

每 token 滑动窗口限流:默认 RPM=60(可按 token 覆盖),内存 deque 实现,
超限返回 429 + 中文 body;被闸门拒绝的请求不计入额度;闸门关闭时不限流。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import Header, HTTPException, Request

# ---------------------------------------------------------------------------
# Scope 规则表(tick-stock-panel scope 门禁的务实版:单机两档)
# ---------------------------------------------------------------------------
# SCOPE_ROUTES:scope → 该档 token 允许访问的端点路径集合。
# - readonly:只读档。/api/health 本就无闸门(不在 dependency 链上),列在这里
#   仅为规则表完整性;readonly token 实际能新调用的只有 /api/validate;
# - readwrite:读写档,允许全部端点。
#
# 扩展方式:
# 1) 新增 scope 档位:在 _SCOPE_LEVEL 登记序数(数字越大权限越高),再在
#    SCOPE_ROUTES 声明该档可访问的端点集合;
# 2) 新增受保护端点:在 SCOPE_ROUTES 对应档位补上路径,并在 main.py 用
#    require_mcp_token(scope="...") 声明该端点所需最低档位。
#
# 生效方式是「双重检查」(见 _scope_allows):token 档位序数 ≥ 端点要求的最低
# 档位序数,且请求路径确实出现在 token 档位的 SCOPE_ROUTES 集合内——后者保证
# 「档位声明」与「实际可访问路径」永远一致,新增端点忘登记者默认拒绝。
_SCOPE_LEVEL: dict[str, int] = {"readonly": 1, "readwrite": 2}

SCOPE_ROUTES: dict[str, tuple[str, ...]] = {
    # /api/kline(K 线行情,GET 只读)为 readonly 新登记:行情查询无需写权限
    "readonly": ("/api/validate", "/api/health", "/api/kline"),
    "readwrite": (
        "/api/validate",
        "/api/health",
        "/api/backtest",
        "/api/chat",
        "/api/arena",
        "/api/kline",
        "/api/verify",
        "/api/optimize",
        # 多轮对话会话族:以 "/*" 结尾的规则按路径前缀匹配(见 _path_allowed),
        # 覆盖 /api/sessions/{sid}/turns 等动态子路径,新增子端点无需再逐条登记。
        "/api/sessions",
        "/api/sessions/*",
    ),
}

# ---------------------------------------------------------------------------
# 统一错误文案(中文,供 LLM/前端直接展示)
# ---------------------------------------------------------------------------
MSG_UNAUTHORIZED = "无效或缺失的 API Token"
MSG_FORBIDDEN = "Token 权限不足(scope={scope} 无法访问该端点)"
MSG_RATE_LIMITED = "请求过于频繁(RPM={rpm}),请稍后再试"

# ---------------------------------------------------------------------------
# 每 token 滑动窗口限流(进程内存态,重启即清零)
# ---------------------------------------------------------------------------
DEFAULT_RPM = 60
RATE_WINDOW_SECONDS = 60.0

# token → 窗口内请求时间戳(monotonic 秒)
_RATE_BUCKETS: dict[str, deque[float]] = {}
_RATE_LOCK = threading.Lock()  # FastAPI 同步端点在线程池并发,复合操作需持锁


@dataclass(frozen=True)
class TokenRecord:
    """单个 token 的配置:scope 档位 / 展示名 / 每分钟请求数上限。"""

    scope: str = "readwrite"
    name: str = ""
    rpm: int = DEFAULT_RPM


def _warn(message: str) -> None:
    """配置类告警统一走 stderr(不污染 API 响应,便于部署侧排查)。"""
    print(f"[aquant.api.token_gate] {message}", file=sys.stderr)


def _parse_token_entry(key: str, value: Any) -> TokenRecord | None:
    """解析 AQUANT_MCP_TOKENS 的单个表项;非法表项返回 None(跳过并告警)。"""
    if isinstance(value, str):  # 简写:{"tkn": "readonly"}
        record = TokenRecord(scope=value.strip())
    elif isinstance(value, dict):
        try:
            rpm = int(value.get("rpm", DEFAULT_RPM))
        except (TypeError, ValueError):
            rpm = DEFAULT_RPM
        record = TokenRecord(
            scope=str(value.get("scope", "readwrite")).strip() or "readwrite",
            name=str(value.get("name", "")),
            rpm=rpm,
        )
    else:
        _warn(f"token {key!r} 的配置既不是对象也不是 scope 字符串,已忽略")
        return None
    if record.scope not in _SCOPE_LEVEL:
        _warn(f"token {key!r} 的 scope={record.scope!r} 未在 _SCOPE_LEVEL 登记,已忽略")
        return None
    if record.rpm <= 0:  # 非法 RPM 回落到默认值,避免配置出「永不放行」的 token
        record = TokenRecord(scope=record.scope, name=record.name, rpm=DEFAULT_RPM)
    return record


def _load_tokens() -> dict[str, TokenRecord] | None:
    """读取当前生效的 token 表(每请求调用,惰性;便于测试 setenv)。

    :returns: None 表示闸门关闭(开放模式);dict(可为空)表示闸门开启。
    """
    multi = os.environ.get("AQUANT_MCP_TOKENS", "").strip()
    if multi:
        try:
            raw: Any = json.loads(multi)
        except json.JSONDecodeError as exc:
            _warn(f"AQUANT_MCP_TOKENS 不是合法 JSON({exc}),fail-closed:所有请求 401")
            return {}
        if not isinstance(raw, dict):
            _warn("AQUANT_MCP_TOKENS 顶层必须是 JSON 对象,fail-closed:所有请求 401")
            return {}
        tokens: dict[str, TokenRecord] = {}
        for key, value in raw.items():
            token = str(key).strip()
            if not token:
                continue
            record = _parse_token_entry(token, value)
            if record is not None:
                tokens[token] = record
        return tokens
    single = os.environ.get("AQUANT_MCP_TOKEN", "").strip()
    if single:
        # 向后兼容:单 token 等价于 scope=readwrite、默认 RPM 的表项
        return {single: TokenRecord(scope="readwrite", name="mcp默认")}
    return None  # 闸门关闭:本机开放模式


def _allow_request(token: str, rpm: int) -> bool:
    """滑动窗口限流:最近 RATE_WINDOW_SECONDS 秒内该 token 请求数 < rpm 才放行。

    放行时记录本次时间戳;超限时不记录(被拒请求不占额度)。
    按 token 分别计数(窗口按 token 计)。
    """
    now = time.monotonic()
    with _RATE_LOCK:
        bucket = _RATE_BUCKETS.setdefault(token, deque())
        cutoff = now - RATE_WINDOW_SECONDS
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= rpm:
            return False
        bucket.append(now)
        return True


def _reset_rate_limits() -> None:
    """清空所有限流窗口(测试辅助;生产中窗口随时间自然滑动,无需调用)。"""
    with _RATE_LOCK:
        _RATE_BUCKETS.clear()


def _path_allowed(token_scope: str, path: str) -> bool:
    """路径匹配:精确命中,或命中以 ``/*`` 结尾的前缀规则。

    ``/api/sessions/*`` 覆盖 ``/api/sessions/<sid>/turns`` 等动态子路径;
    未登记路径一律拒绝(fail-closed),新端点忘登记不会静默放开。
    """
    rules = SCOPE_ROUTES.get(token_scope, ())
    if path in rules:
        return True
    return any(rule.endswith("/*") and path.startswith(rule[:-1]) for rule in rules)


def _scope_allows(token_scope: str, required_level: int, path: str) -> bool:
    """双重检查:token 档位不低于端点要求,且路径在 token 档位的规则表集合内。"""
    if _SCOPE_LEVEL.get(token_scope, -1) < required_level:
        return False
    return _path_allowed(token_scope, path)


def require_mcp_token(scope: str = "readwrite") -> Callable[[Request], Any]:
    """FastAPI dependency 工厂:生成要求「最低 scope」为 ``scope`` 的闸门 dependency。

    main.py 各端点按需声明:/api/validate 用 readonly 即可,
    /api/backtest、/api/arena、/api/chat 用 readwrite。
    闸门关闭(未配置任何 token)时,生成的 dependency 直接放行(现状行为)。

    检查顺序:认证(401)→ scope(403)→ 限流(429)。
    """
    if scope not in _SCOPE_LEVEL:  # 声明错误 fail-fast:启动期即暴露拼写问题
        raise ValueError(
            f"未知 scope={scope!r}:请先在 token_gate._SCOPE_LEVEL 登记该档位。"
        )
    required_level = _SCOPE_LEVEL[scope]

    async def _gate(
        request: Request,
        x_mcp_token: str | None = Header(default=None),
    ) -> None:
        tokens = _load_tokens()
        if tokens is None:
            return  # 闸门关闭:本机开放模式,完全放行(现状行为,不限流)
        presented = (x_mcp_token or "").strip()
        record = tokens.get(presented) if presented else None
        if record is None:
            raise HTTPException(status_code=401, detail=MSG_UNAUTHORIZED)
        if not _scope_allows(record.scope, required_level, request.url.path):
            raise HTTPException(
                status_code=403, detail=MSG_FORBIDDEN.format(scope=record.scope)
            )
        if not _allow_request(presented, record.rpm):
            raise HTTPException(
                status_code=429, detail=MSG_RATE_LIMITED.format(rpm=record.rpm)
            )

    _gate.__name__ = f"require_mcp_token[scope={scope}]"  # 调试/文档可读性
    return _gate
