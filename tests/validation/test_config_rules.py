"""Layer 1 配置 lint + Layer 3 注入语义测试。"""

from aquant.config.schema import build_run_config, platform_mod_config
from aquant.validation.config_rules import check_config
from aquant.validation.findings import RuleId, Severity


def _error_rules(verdict_findings):
    return {f.rule for f in verdict_findings if f.severity is Severity.ERROR}


def test_platform_default_config_passes_lint():
    """平台默认配置(注入后)必须全部通过保守性 lint。"""
    config = build_run_config("2024-01-01", "2024-06-30")
    findings = check_config(config)
    assert _error_rules(findings) == set()
    # 且包含平台注入说明
    assert any(f.rule == RuleId.INJECTED_DEFAULT for f in findings)


def test_zero_slippage_default_is_blocked():
    """rqalpha 默认 slippage=0 —— 平台默认注入 0.002;若用户覆盖为 0 必须 ERROR。"""
    config = build_run_config("2024-01-01", "2024-06-30",
                              user_mod_overrides={"sys_simulation": {"slippage": 0.0}})
    assert RuleId.CFG_ZERO_SLIPPAGE in _error_rules(check_config(config))


def test_low_slippage_below_conservative_blocked():
    config = build_run_config("2024-01-01", "2024-06-30",
                              user_mod_overrides={"sys_simulation": {"slippage": 0.001}})
    assert RuleId.CFG_LOW_SLIPPAGE in _error_rules(check_config(config))


def test_user_more_conservative_respected():
    """用户给更保守的滑点(0.005)必须被尊重,不报错。"""
    config = build_run_config("2024-01-01", "2024-06-30",
                              user_mod_overrides={"sys_simulation": {"slippage": 0.005}})
    rules = {f.rule for f in check_config(config)}
    assert not rules & {RuleId.CFG_ZERO_SLIPPAGE, RuleId.CFG_LOW_SLIPPAGE}


def test_t1_disabled_blocked():
    config = build_run_config("2024-01-01", "2024-06-30",
                              user_mod_overrides={"sys_accounts": {"stock_t1": False}})
    assert RuleId.CFG_T1_DISABLED in _error_rules(check_config(config))


def test_price_limit_off_blocked():
    config = build_run_config("2024-01-01", "2024-06-30",
                              user_mod_overrides={"sys_simulation": {"price_limit": False}})
    assert RuleId.CFG_PRICE_LIMIT_OFF in _error_rules(check_config(config))


def test_volume_limit_off_and_high_percent_blocked():
    config = build_run_config(
        "2024-01-01", "2024-06-30",
        user_mod_overrides={"sys_simulation": {"volume_limit": False, "volume_percent": 0.5}},
    )
    rules = _error_rules(check_config(config))
    assert RuleId.CFG_VOLUME_LIMIT_OFF in rules
    assert RuleId.CFG_VOLUME_PERCENT_HIGH in rules


def test_zero_commission_and_tax_blocked():
    config = build_run_config(
        "2024-01-01", "2024-06-30",
        user_mod_overrides={"sys_transaction_cost": {"stock_commission_multiplier": 0.0, "tax_multiplier": 0.5}},
    )
    rules = _error_rules(check_config(config))
    assert RuleId.CFG_ZERO_COST in rules


def test_signal_mode_blocked():
    config = build_run_config("2024-01-01", "2024-06-30",
                              user_mod_overrides={"sys_simulation": {"signal": True}})
    assert RuleId.CFG_SIGNAL_MODE in _error_rules(check_config(config))


def test_unsupported_account_blocked():
    config = build_run_config("2024-01-01", "2024-06-30")
    config["base"]["accounts"]["FUTURE"] = 100000
    assert RuleId.CFG_UNSUPPORTED_ACCOUNT in _error_rules(check_config(config))


def test_injected_defaults_info_lists_platform_values():
    config = platform_mod_config()
    findings = check_config({"mod": config})
    infos = [f for f in findings if f.rule == RuleId.INJECTED_DEFAULT]
    keys = {f.detail["key"] for f in infos}
    assert "mod.sys_simulation.slippage" in keys
    assert "mod.sys_accounts.stock_t1" in keys
    assert "mod.sys_transaction_cost.stock_commission_multiplier" in keys
