"""Layer 1:回测配置 lint(T+1 / 涨跌停 / 手续费 / 滑点 / 成交量限制)。

判定依据全部来自 rqalpha 6.5.0 源码核实的事实(见 docs/design.md 第 2 节),
阈值来自 Layer 3 的 defaults.yaml(唯一事实来源)。
"""

from __future__ import annotations

from typing import Any

from aquant.config.schema import injected_defaults_descriptions, load_defaults, platform_mod_config
from aquant.validation.findings import Finding, RuleId, Severity

_SLIPPAGE_MODEL_WHITELIST = {
    "PriceRatioSlippage",
    "TickSizeSlippage",
}


def check_config(run_config: dict[str, Any]) -> list[Finding]:
    """对最终 run 配置(mod 段)执行保守性 lint。

    run_config 应由 aquant.config.schema.build_run_config 产出
    (平台默认已注入),也接受外部构造的等价结构。
    """
    findings: list[Finding] = []
    dfts = load_defaults()
    mod = run_config.get("mod", {})
    sim = mod.get("sys_simulation", {})
    cost = mod.get("sys_transaction_cost", {})
    accounts = mod.get("sys_accounts", {})
    base = run_config.get("base", {})

    # ---- T+1:A股市场规则,不允许关闭 ------------------------------------
    if accounts.get("stock_t1") is not True:
        findings.append(
            Finding(
                RuleId.CFG_T1_DISABLED,
                Severity.ERROR,
                "T+1 被关闭(mod.sys_accounts.stock_t1 != True):A股实盘不存在当日买入当日卖出,"
                "关闭 T+1 的回测结果不可信。平台强制开启。",
                detail={"stock_t1": accounts.get("stock_t1")},
            )
        )

    # ---- 涨跌停撮合限制 ---------------------------------------------------
    if sim.get("price_limit") is not True:
        findings.append(
            Finding(
                RuleId.CFG_PRICE_LIMIT_OFF,
                Severity.ERROR,
                "涨跌停撮合限制被关闭(mod.sys_simulation.price_limit != True):"
                "回测将允许在涨跌停板上成交,与实盘严重不符。平台强制开启。",
                detail={"price_limit": sim.get("price_limit")},
            )
        )

    # ---- 成交量限制 -------------------------------------------------------
    if sim.get("volume_limit") is not True:
        findings.append(
            Finding(
                RuleId.CFG_VOLUME_LIMIT_OFF,
                Severity.ERROR,
                "成交量限制被关闭(mod.sys_simulation.volume_limit != True):"
                "订单将不受市场流动性约束,小盘股策略收益会严重虚高。平台强制开启。",
                detail={"volume_limit": sim.get("volume_limit")},
            )
        )
    vp = sim.get("volume_percent")
    if isinstance(vp, (int, float)):
        if vp > dfts["volume_limit"]["max_allowed"]:
            findings.append(
                Finding(
                    RuleId.CFG_VOLUME_PERCENT_HIGH,
                    Severity.ERROR,
                    f"volume_percent={vp} 超过平台上限 {dfts['volume_limit']['max_allowed']}:"
                    "单根K线成交量占比过高的回测在实盘无法按价成交。",
                    detail={"volume_percent": vp, "max": dfts["volume_limit"]["max_allowed"]},
                )
            )

    # ---- 滑点 -------------------------------------------------------------
    slip = sim.get("slippage")
    if isinstance(slip, (int, float)):
        if slip <= 0:
            findings.append(
                Finding(
                    RuleId.CFG_ZERO_SLIPPAGE,
                    Severity.ERROR,
                    f"滑点为 {slip}(零滑点):实盘不存在零冲击成交,零滑点回测是收益虚高的第一大来源。"
                    f"平台强制 ≥{dfts['slippage']['min_allowed']}(比例)。",
                    detail={"slippage": slip},
                )
            )
        elif slip < dfts["slippage"]["min_allowed"]:
            findings.append(
                Finding(
                    RuleId.CFG_LOW_SLIPPAGE,
                    Severity.ERROR,
                    f"滑点 {slip} 低于平台保守值 {dfts['slippage']['min_allowed']}(比例)。"
                    "低于该值时小单也难以在实盘全部按价成交。",
                    detail={"slippage": slip, "min": dfts["slippage"]["min_allowed"]},
                )
            )
    model = sim.get("slippage_model")
    if model is not None and model not in _SLIPPAGE_MODEL_WHITELIST:
        findings.append(
            Finding(
                RuleId.CFG_SLIPPAGE_MODEL,
                Severity.WARNING,
                f"滑点模型 {model} 不在白名单 {_SLIPPAGE_MODEL_WHITELIST}:"
                "自定义模型请提供冲击成本依据,否则视为激进假设。",
                detail={"model": model},
            )
        )

    # ---- 手续费/印花税 -----------------------------------------------------
    scm = cost.get("stock_commission_multiplier")
    if isinstance(scm, (int, float)) and scm < dfts["transaction_cost"]["stock_commission_multiplier_min"]:
        findings.append(
            Finding(
                RuleId.CFG_ZERO_COST,
                Severity.ERROR,
                f"佣金倍率 {scm} < 1:低于真实佣金(万八基准)。手续费是实盘损耗的确定项,"
                "手续费不完整的回测必然高估收益。平台强制 ≥1。",
                detail={"stock_commission_multiplier": scm},
            )
        )
    tax = cost.get("tax_multiplier")
    if isinstance(tax, (int, float)) and tax < dfts["transaction_cost"]["tax_multiplier_min"]:
        findings.append(
            Finding(
                RuleId.CFG_ZERO_COST,
                Severity.ERROR,
                f"印花税倍率 {tax} < 1:A股卖出印花税(千一、单边)是刚性成本,禁止减免。",
                detail={"tax_multiplier": tax},
            )
        )

    # ---- 信号模式(跳过撮合)----------------------------------------------
    if sim.get("signal") is True:
        findings.append(
            Finding(
                RuleId.CFG_SIGNAL_MODE,
                Severity.ERROR,
                "信号模式开启(mod.sys_simulation.signal=True):所有订单不撮合直接成交,"
                "这不是回测而是理想化推演,平台禁止。",
                detail={"signal": True},
            )
        )

    # ---- 撮合方式 ---------------------------------------------------------
    mt = sim.get("matching_type")
    if mt is not None and mt not in dfts["matching"]["allowed_matching_types"]:
        findings.append(
            Finding(
                RuleId.CFG_UNSAFE_MATCHING,
                Severity.ERROR,
                f"撮合方式 {mt} 不被日线回测支持(允许:{dfts['matching']['allowed_matching_types']})。",
                detail={"matching_type": mt},
            )
        )

    # ---- 账户类型 ---------------------------------------------------------
    accs = base.get("accounts", {})
    unsupported = {k for k in accs if k not in dfts["account"]["supported_types"]}
    if unsupported:
        findings.append(
            Finding(
                RuleId.CFG_UNSUPPORTED_ACCOUNT,
                Severity.ERROR,
                f"账户类型 {sorted(unsupported)} 不在本平台支持范围(仅 {dfts['account']['supported_types']}):"
                "期货/信用等账户的保证金、强平逻辑不在四层校验覆盖内。",
                detail={"accounts": sorted(accs)},
            )
        )

    # ---- 平台注入说明(INFO)---------------------------------------------
    user_overrides = _extract_user_overrides(mod)
    for key, value, reason in injected_defaults_descriptions(user_overrides):
        findings.append(
            Finding(
                RuleId.INJECTED_DEFAULT,
                Severity.INFO,
                f"平台注入保守默认参数 {key}={value}({reason})",
                detail={"key": key, "value": value},
            )
        )
    return findings


def _extract_user_overrides(mod: dict[str, Any]) -> dict[str, Any]:
    """从最终 mod 配置中还原"用户覆盖集"。

    平台注入的键值与 defaults 一致;凡与默认值不同的键视为用户覆盖。
    这样无需在调用链上传透原始 overrides 即可生成准确的 INFO 说明。
    """
    defaults = platform_mod_config()
    overrides: dict[str, Any] = {}
    for section, values in mod.items():
        if not isinstance(values, dict):
            continue
        dflt_section = defaults.get(section, {})
        diff = {k: v for k, v in values.items() if k not in dflt_section or dflt_section[k] != v}
        if diff:
            overrides[section] = diff
    return overrides
