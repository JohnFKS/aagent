"""LLM 客户端抽象层。

设计要点(ADR 5:LLM 可插拔,校验是硬门禁,LLM 无法绕过):
- ``LLMClient`` Protocol 定义统一接口,Agent 层只依赖协议;
- ``DeterministicLLM`` 无需网络的兜底实现,保证无 API Key 时整条编排管线可演示;
- ``OpenAICompatLLM`` 对接任意 OpenAI 兼容 /chat/completions 服务(同步 httpx)。

凭据一律从环境变量读取:AQUANT_LLM_BASE_URL / AQUANT_LLM_API_KEY / AQUANT_LLM_MODEL。
"""

from __future__ import annotations

import os
import re
from typing import Protocol, runtime_checkable

import httpx
import time

ENV_BASE_URL = "AQUANT_LLM_BASE_URL"
ENV_API_KEY = "AQUANT_LLM_API_KEY"
ENV_MODEL = "AQUANT_LLM_MODEL"
ENV_AUTH_SCHEME = "AQUANT_LLM_AUTH_SCHEME"  # 可选:bearer(默认)| api-key

# 三者齐备才启用真实 LLM;缺任一项视为未配置完整(避免半配置导致运行期才发现)
_REQUIRED_ENVS = (ENV_BASE_URL, ENV_API_KEY, ENV_MODEL)

_DEFAULT_TARGET_PERCENT = 0.95  # 模板默认仓位(留现金缓冲)
_MIN_TARGET_PERCENT = 0.10
_MAX_TARGET_PERCENT = 0.95

# 确定性 LLM 的"评审角色"分派标记:系统提示词同时包含这两个词视为代码评审请求
_REVIEW_MARKERS = ("评审", "JSON")

# 兜底评审输出:合法 JSON,供 ReviewAgent 正常合并(演示编排用)
_REVIEW_JSON = (
    '{"findings": [{"rule": "llm.manual_review", "severity": "info", '
    '"message": "确定性兜底评审:未发现明显未来函数/外部数据/成本缺失问题,'
    '请以平台规则校验结论为准。"}]}'
)


class LLMAPIError(RuntimeError):
    """LLM 服务调用失败(网络不可达 / 上游错误码 / 响应不符合协议)。"""


@runtime_checkable
class LLMClient(Protocol):
    """LLM 客户端协议:给定系统提示词与用户消息,返回补全文案。"""

    def complete(self, system: str, user: str) -> str:
        """执行一次补全调用。"""
        ...


class DeterministicLLM:
    """无网络的确定性兜底 LLM。

    行为约定(不解析自然语言,仅为演示编排管线):
    - 系统提示词是"代码评审"角色时,返回一段合法的评审 JSON;
    - 其余情况视为策略生成请求,忽略 user 的措辞差异,返回内置的双均线策略模板
      (模板符合平台校验:history_bars 取数、无外部数据、无墙钟时间);
    - 仅对 user 中出现的极简关键词做参数替换(如"仓位 80%" → 目标资金比例 0.8)。
    """

    _STRATEGY_TEMPLATE = '''"""双均线择时策略(平台兜底模板,目标仓位 {target_percent:.2f})。

模板已对齐平台四层校验:
- 信号只用 history_bars 截至当前 bar 的收盘价,无未来函数;
- 不访问外部数据、不使用墙钟时间,回测可复现;
- 不假设零成本:滑点/佣金/印花税/T+1/成交量限制由平台强制注入。
"""


def init(context):
    context.s1 = "000001.XSHE"
    context.fast = 5
    context.slow = 20
    context.target_percent = {target_percent}


def handle_bar(context, bar_dict):
    from rqalpha.const import POSITION_DIRECTION

    prices = history_bars(context.s1, context.slow + 2, "1d", "close")
    if prices is None or len(prices) < context.slow:
        return
    ma_fast = prices[-context.fast:].mean()
    ma_slow = prices[-context.slow:].mean()

    pos = context.portfolio.get_position(context.s1, POSITION_DIRECTION.LONG)
    if ma_fast > ma_slow and pos.quantity == 0:
        order_target_percent(context.s1, context.target_percent)
    elif ma_fast < ma_slow and pos.quantity > 0:
        order_target_percent(context.s1, 0.0)
'''

    def complete(self, system: str, user: str) -> str:
        """按角色分派输出(见类 docstring)。"""
        if all(marker in system for marker in _REVIEW_MARKERS):
            return _REVIEW_JSON
        return self._render_strategy(user)

    def _render_strategy(self, user: str) -> str:
        """渲染策略模板;仅从 user 中提取"资金比例"这一类极简参数。"""
        target = _DEFAULT_TARGET_PERCENT
        match = re.search(r"(\d{1,3})\s*%", user)
        if match:
            target = int(match.group(1)) / 100.0
        target = min(max(target, _MIN_TARGET_PERCENT), _MAX_TARGET_PERCENT)
        return self._STRATEGY_TEMPLATE.format(target_percent=target)


class OpenAICompatLLM:
    """OpenAI 兼容 /chat/completions 客户端(同步 httpx,超时 60s)。

    auth_scheme 兼容不同开放平台的认证头差异:
    - "bearer"(默认,OpenAI 标准):``Authorization: Bearer <key>``;
    - "api-key"(如 Dots API 等平台):``api-key: <key>``。
    """

    _AUTH_SCHEMES = ("bearer", "api-key")

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 120.0,
        auth_scheme: str = "bearer",
        max_retries: int = 2,
    ) -> None:
        if auth_scheme not in self._AUTH_SCHEMES:
            raise ValueError(
                f"auth_scheme 必须为 {self._AUTH_SCHEMES} 之一,收到 {auth_scheme!r}"
            )
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.auth_scheme = auth_scheme
        self.max_retries = max_retries  # 网络类瞬时错误(超时/断连)的自动重试次数

    def complete(self, system: str, user: str) -> str:
        """调用 /chat/completions;任何失败都抛出带上下文的 LLMAPIError。

        网络类瞬时错误(超时/连接失败)自动重试 max_retries 次(间隔 2s/4s);
        HTTP 4xx/5xx 不重试(限流/配置类错误应由调用方感知,重试会放大故障)。
        """
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
        }
        if self.auth_scheme == "api-key":
            headers = {"api-key": self.api_key}
        else:
            headers = {"Authorization": f"Bearer {self.api_key}"}
        url = f"{self.base_url}/chat/completions"
        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 2):
            try:
                resp = httpx.post(url, json=payload, headers=headers, timeout=self.timeout)
                break
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt <= self.max_retries:
                    time.sleep(2.0 * attempt)
                    continue
                raise LLMAPIError(f"LLM 请求失败(已重试 {self.max_retries} 次,{url}):{exc!r}") from exc
        if resp.status_code >= 400:
            raise LLMAPIError(
                f"LLM 服务返回错误状态 {resp.status_code}({url}):{resp.text[:300]}"
            )
        try:
            data = resp.json()
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise LLMAPIError(
                f"LLM 响应不符合 OpenAI 兼容协议(缺少 choices[0].message.content):"
                f"{resp.text[:300]}"
            ) from exc


def make_llm() -> LLMClient:
    """构造 LLM 客户端:网页设置页保存的配置优先,其次环境变量,否则确定性兜底。"""
    from aquant.config.llm_settings import load_settings

    saved = load_settings()
    file_keys = {
        ENV_BASE_URL: "base_url",
        ENV_API_KEY: "api_key",
        ENV_MODEL: "model",
    }
    values = {
        name: (saved.get(file_keys[name]) or os.environ.get(name))
        for name in _REQUIRED_ENVS
    }
    configured = [name for name, value in values.items() if value]
    if not configured:
        return DeterministicLLM()
    if len(configured) != len(_REQUIRED_ENVS):
        missing = [name for name, value in values.items() if not value]
        raise ValueError(
            f"LLM 配置不完整:已设置 {configured},缺失 {missing}。"
            f"请在网页设置页补全,或设置 {_REQUIRED_ENVS} 三项环境变量。"
        )
    auth_scheme = (
        saved.get("auth_scheme") or os.environ.get(ENV_AUTH_SCHEME, "bearer")
    ).strip().lower() or "bearer"
    return OpenAICompatLLM(
        values[ENV_BASE_URL],
        values[ENV_API_KEY],
        values[ENV_MODEL],
        auth_scheme=auth_scheme,
        timeout=float(saved.get("timeout") or 120.0),
        max_retries=int(saved.get("max_retries") or 2),
    )
