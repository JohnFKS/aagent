"""校验结果数据结构。

Finding 是四层校验体系的统一产出物:所有校验规则(AST 静态规则、配置 lint、
数据完整性检查)都产出 Finding 列表,由编排器聚合为 Verdict。

severity 语义:
- error   : 绝对阻断。回测结果不可信(未来函数/关闭了 T+1/零费率等),必须修复。
- warning : 风险提示。不阻断,但必须在回测报告页向用户展示。
- info    : 事实说明(如"已自动注入平台默认滑点 0.2%")。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class RuleId(str, Enum):
    """规则标识。命名:<层>.<规则名>。"""

    # Layer 1: AST 未来函数 / 可复现性
    AST_SHIFT_NEGATIVE = "ast.shift_negative"                 # .shift(-n) 向未来取数
    AST_WALL_CLOCK = "ast.wall_clock"                         # 使用 datetime.now()/today() 等墙钟
    AST_EXTERNAL_DATA = "ast.external_data"                   # 策略内直接访问网络/文件数据
    AST_GLOBAL_FIT = "ast.global_fit"                         # 在 init/全局用全样本 fit 模型
    AST_REVERSE_SLICE = "ast.reverse_slice"                   # [::-1] 反转后取"最新"的脆弱模式
    AST_BEFORE_TRADING_PRICE = "ast.before_trading_price"     # before_trading 中使用当日价格(未发生)
    AST_ORDER_API_MISUSE = "ast.order_api_misuse"             # 在 before_trading/open_auction 下单等时序错误
    AST_SYNTAX = "ast.syntax"                                 # 代码语法错误/缺少必需入口函数

    # Layer 1: 配置 lint(rqalpha 6.5.0)
    CFG_T1_DISABLED = "cfg.stock_t1_disabled"                 # stock_t1 被关闭
    CFG_PRICE_LIMIT_OFF = "cfg.price_limit_off"               # 涨跌停撮合限制被关闭
    CFG_VOLUME_LIMIT_OFF = "cfg.volume_limit_off"             # 成交量限制被关闭
    CFG_VOLUME_PERCENT_HIGH = "cfg.volume_percent_high"       # volume_percent 超过 0.25
    CFG_ZERO_SLIPPAGE = "cfg.zero_slippage"                   # 滑点为 0(默认即 0,必须显式保守)
    CFG_LOW_SLIPPAGE = "cfg.low_slippage"                     # 滑点低于平台保守值
    CFG_ZERO_COST = "cfg.zero_cost"                           # 手续费/印花税倍率 < 1
    CFG_SIGNAL_MODE = "cfg.signal_mode"                       # 信号模式(跳过撮合,非真实回测)
    CFG_UNSUPPORTED_ACCOUNT = "cfg.unsupported_account"       # 非股票现货账户
    CFG_SLIPPAGE_MODEL = "cfg.slippage_model"                 # 非白名单滑点模型
    CFG_UNSAFE_MATCHING = "cfg.unsafe_matching"               # matching_type 非 current_bar/vwap

    # Layer 1: 平台注入说明
    INJECTED_DEFAULT = "cfg.injected_default"                 # 已自动注入保守默认参数


@dataclass(frozen=True)
class Finding:
    """单条校验发现。"""

    rule: RuleId
    severity: Severity
    message: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["rule"] = self.rule.value
        d["severity"] = self.severity.value
        return d


@dataclass(frozen=True)
class Verdict:
    """一次校验的聚合结果。"""

    findings: tuple[Finding, ...]

    @property
    def has_error(self) -> bool:
        return any(f.severity is Severity.ERROR for f in self.findings)

    @property
    def error_count(self) -> int:
        return sum(1 for f in self.findings if f.severity is Severity.ERROR)

    @property
    def warning_count(self) -> int:
        return sum(1 for f in self.findings if f.severity is Severity.WARNING)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": not self.has_error,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "findings": [f.to_dict() for f in self.findings],
        }

    def error_messages(self) -> list[str]:
        """全部 ERROR 级 finding 的紧凑文本([rule] message),供阻断提示复用。"""
        return [
            f"[{f.rule.value}] {f.message}"
            for f in self.findings
            if f.severity is Severity.ERROR
        ]
