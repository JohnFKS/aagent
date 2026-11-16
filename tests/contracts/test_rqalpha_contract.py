"""RQAlpha 版本契约快照:冻结已核实版本,放行新版本前必须显式复核。

- 6.5.0 快照为字面量(不从安装包 import),防止"生产与测试同源自动跟随";
- 新版本在 AQUANT_RQALPHA_ALLOW=1 下可跑同一套测试,冻结断言自动 skip,
  但 gate 断言仍要求 compat 能给出可用 profile。
"""

from __future__ import annotations

import pytest

from aquant import compat

FROZEN_6_5_0 = {
    "stock_fields": (
        "datetime",
        "open",
        "close",
        "high",
        "low",
        "volume",
        "total_turnover",
        "limit_up",
        "limit_down",
    ),
    "index_fields": ("datetime", "open", "close", "high", "low", "volume", "total_turnover"),
    "tenors": ("0S", "1M", "3M", "6M", "1Y", "2Y", "3Y", "5Y", "10Y"),
    "benchmark": "000001.XSHG",
    "mod_sections": ("sys_simulation", "sys_transaction_cost", "sys_accounts", "sys_analyser"),
    "summary_keys": (
        "total_returns",
        "annualized_returns",
        "benchmark_total_returns",
        "sharpe",
        "max_drawdown",
        "volatility",
        "alpha",
        "beta",
        "turnover",
        "win_rate",
    ),
}


def _only_6_5_0() -> None:
    if compat.installed_rqalpha_version() != "6.5.0":
        pytest.skip("冻结快照只约束 6.5.0;升级新版本时需人工复核并更新快照")


class TestVersionGate:
    def test_installed_version_has_profile(self):
        report = compat.version_report()
        assert report["status"] in {"ok", "allowed"}, report.get("detail")
        assert report["installed"] == compat.installed_rqalpha_version()
        assert compat.current_profile().version == report["installed"]

    def test_unknown_version_fails_closed(self, monkeypatch):
        monkeypatch.delenv(compat.ALLOW_ENV, raising=False)
        with pytest.raises(compat.UnsupportedRQAlphaError):
            compat.current_profile("0.0.0-contract-probe")

    def test_allow_unknown_derives_profile_from_installed(self, monkeypatch):
        monkeypatch.setenv(compat.ALLOW_ENV, "1")
        base = compat.current_profile()
        probe = compat.current_profile("0.0.0-contract-allowed")
        assert probe.stock_bar_dtype.names == base.stock_bar_dtype.names
        assert probe.index_bar_dtype.names == base.index_bar_dtype.names
        assert probe.position_direction_required is True


class TestFrozen65Contract:
    def test_bundle_dtype_snapshot(self):
        _only_6_5_0()
        profile = compat.current_profile()
        assert tuple(profile.stock_bar_dtype.names) == FROZEN_6_5_0["stock_fields"]
        assert tuple(profile.index_bar_dtype.names) == FROZEN_6_5_0["index_fields"]

    def test_engine_constants_snapshot(self):
        _only_6_5_0()
        profile = compat.current_profile()
        assert profile.yield_curve_tenors == FROZEN_6_5_0["tenors"]
        assert profile.benchmark_fallback_oid == FROZEN_6_5_0["benchmark"]
        assert profile.mod_config_sections == FROZEN_6_5_0["mod_sections"]
        assert profile.summary_keys == FROZEN_6_5_0["summary_keys"]
        assert profile.trades_is_dataframe is True
        assert profile.position_direction_required is True

    def test_supported_list_contains_baseline(self):
        assert "6.5.0" in compat.supported_versions()
