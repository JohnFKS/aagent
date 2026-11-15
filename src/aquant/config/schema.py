"""Layer 3:保守默认参数的加载、合并、强制校验与 RQAlpha 配置导出。

职责:
1. 加载 defaults.yaml(平台保守默认,唯一事实来源);
2. 与用户覆盖值合并(用户更保守 → 尊重用户;更激进 → 校验层报 ERROR);
3. 输出符合 rqalpha 6.5.0 schema 的 run 配置 dict(供 runner 直接消费)。

注意:本模块只做"参数就位",判定激进/保守的规则在 validation/config_rules.py
中实现(单一职责:校验规则与注入逻辑共享同一份 defaults 事实)。
"""

from __future__ import annotations

import copy
import os
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

_DFTS: dict[str, Any] | None = None


def default_bundle_path() -> Path:
    """RQAlpha 数据 bundle 的平台默认路径(AQUANT_BUNDLE_PATH 可覆盖)。

    runner 与 api/adapter 共用本函数,保证口径唯一。
    """
    env = os.environ.get("AQUANT_BUNDLE_PATH")
    return Path(env) if env else Path.home() / ".aquant" / "bundle"


def load_defaults() -> dict[str, Any]:
    """读取平台保守默认参数(带缓存)。"""
    global _DFTS
    if _DFTS is None:
        text = resources.files("aquant.config").joinpath("defaults.yaml").read_text("utf-8")
        _DFTS = yaml.safe_load(text)
    return copy.deepcopy(_DFTS)


def platform_mod_config(user_overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """构造 rqalpha `mod` 配置段。

    顺序:平台默认 → 用户覆盖(原样并入)。覆盖值是否越界由校验层判定,
    这里不做拦截,保证"注入逻辑"与"判定逻辑"分离。
    """
    dfts = load_defaults()
    mod: dict[str, Any] = {
        "sys_simulation": {
            "signal": dfts["matching"]["signal_mode_enabled"],
            "matching_type": dfts["matching"]["default_matching_type"],
            "price_limit": dfts["matching"]["price_limit_enabled"],
            "volume_limit": dfts["volume_limit"]["enabled"],
            "volume_percent": dfts["volume_limit"]["percent"],
            "slippage_model": dfts["slippage"]["model"],
            "slippage": dfts["slippage"]["value"],
            "inactive_limit": True,
        },
        "sys_transaction_cost": {
            "stock_min_commission": dfts["transaction_cost"]["stock_min_commission"],
            "stock_commission_multiplier": 1.0,
            "tax_multiplier": 1.0,
        },
        "sys_accounts": {
            "stock_t1": dfts["matching"]["stock_t1_enabled"],
        },
        "sys_analyser": {
            "plot": False,
        },
    }
    if user_overrides:
        for section, values in user_overrides.items():
            target = mod.setdefault(section, {})
            if isinstance(values, dict):
                target.update(values)
            else:
                mod[section] = values
    return mod


def platform_base_config(
    start_date: str,
    end_date: str,
    cash: float | None = None,
    benchmark: str | None = None,
) -> dict[str, Any]:
    """构造 rqalpha `base` 配置段(股票现货账户)。"""
    dfts = load_defaults()
    base: dict[str, Any] = {
        "start_date": start_date,
        "end_date": end_date,
        "frequency": "1d",
        "accounts": {"STOCK": cash if cash is not None else dfts["account"]["default_cash"]},
    }
    if benchmark:
        base["benchmark"] = benchmark
    return base


def build_run_config(
    start_date: str,
    end_date: str,
    user_mod_overrides: dict[str, Any] | None = None,
    cash: float | None = None,
    benchmark: str | None = None,
) -> dict[str, Any]:
    """汇总为完整 RQAlpha run 配置。mod/base 由平台产出,校验层负责把关。"""
    return {
        "base": platform_base_config(start_date, end_date, cash=cash, benchmark=benchmark),
        "mod": platform_mod_config(user_mod_overrides),
    }


def injected_defaults_descriptions(
    user_overrides: dict[str, Any] | None = None,
) -> list[tuple[str, str, str]]:
    """列出哪些参数是平台注入的(未被用户覆盖),供 INFO Finding 与报告展示。

    返回 [(rqalpha配置键, 注入值, 理由), ...]
    """
    user_overrides = user_overrides or {}
    sim = user_overrides.get("sys_simulation", {})
    cost = user_overrides.get("sys_transaction_cost", {})
    accounts = user_overrides.get("sys_accounts", {})
    dfts = load_defaults()

    items: list[tuple[str, str, str]] = []
    if "slippage" not in sim:
        items.append(
            ("mod.sys_simulation.slippage", str(dfts["slippage"]["value"]), "比例滑点,消除零滑点收益虚高")
        )
    if "volume_percent" not in sim:
        items.append(
            ("mod.sys_simulation.volume_percent", str(dfts["volume_limit"]["percent"]), "单K线成交量占比限制")
        )
    if "stock_commission_multiplier" not in cost:
        items.append(("mod.sys_transaction_cost.stock_commission_multiplier", "1.0", "真实佣金(万八基准)"))
    if "tax_multiplier" not in cost:
        items.append(("mod.sys_transaction_cost.tax_multiplier", "1.0", "真实印花税(千一单边)"))
    if "stock_t1" not in accounts:
        items.append(("mod.sys_accounts.stock_t1", "True", "A股 T+1 规则"))
    if "price_limit" not in sim:
        items.append(("mod.sys_simulation.price_limit", "True", "涨跌停撮合限制"))
    if "volume_limit" not in sim:
        items.append(("mod.sys_simulation.volume_limit", "True", "成交量限制开关"))
    if "signal" not in sim:
        items.append(("mod.sys_simulation.signal", "False", "禁止信号模式(必须真实撮合)"))
    return items
