"""RQAlpha 版本兼容层:把"换引擎版本"收敛到一个 profile。

设计目标:
- 默认只放行经过核实的版本(当前 6.5.0),启动时即可 fail-fast;
- 需要在隔离环境快速试装更新版时,设置 AQUANT_RQALPHA_ALLOW=1,
  自动从当前安装包读取 dtype 等事实并生成临时 profile,先跑门禁测试再决定是否转正;
- 业务模块只依赖本层导出的常量/函数,不直接 import rqalpha 私有实现。

转正新版本只需三步:
1. 在 SUPPORTED_VERSIONS 加入版本号;
2. 若上游语义有变,新增 CompatProfile 覆盖字段;
3. 跑 scripts/upgrade_rqalpha.sh <版本> 完成验证与锁文件更新。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from importlib import metadata
from typing import Any

ALLOW_ENV = "AQUANT_RQALPHA_ALLOW"

# 经过源码核实、默认放行的版本。新增版本在这里登记即可。
SUPPORTED_VERSIONS: tuple[str, ...] = ("6.5.0",)

# 以下为 6.5.0 契约快照,供 tests/contracts 冻结比对;上游语义变化时应在新 profile 覆盖。
YIELD_CURVE_TENORS: tuple[str, ...] = ("0S", "1M", "3M", "6M", "1Y", "2Y", "3Y", "5Y", "10Y")
BENCHMARK_FALLBACK_OID = "000001.XSHG"
MOD_CONFIG_SECTIONS: tuple[str, ...] = (
    "sys_simulation",
    "sys_transaction_cost",
    "sys_accounts",
    "sys_analyser",
)
SUMMARY_KEYS: tuple[str, ...] = (
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
)


class UnsupportedRQAlphaError(RuntimeError):
    """安装版本与平台兼容清单不一致。"""


@dataclass(frozen=True)
class CompatProfile:
    """一个 RQAlpha 版本的全部适配事实。"""

    version: str
    stock_bar_dtype: Any
    index_bar_dtype: Any
    benchmark_fallback_oid: str = BENCHMARK_FALLBACK_OID
    yield_curve_tenors: tuple[str, ...] = YIELD_CURVE_TENORS
    mod_config_sections: tuple[str, ...] = MOD_CONFIG_SECTIONS
    summary_keys: tuple[str, ...] = SUMMARY_KEYS
    trades_is_dataframe: bool = True
    position_direction_required: bool = True


_PROFILES: dict[str, CompatProfile] = {}


def allow_unknown() -> bool:
    """是否显式放行未登记版本(仅用于隔离验证,不写入默认行为)。"""
    return os.environ.get(ALLOW_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def installed_rqalpha_version() -> str:
    try:
        return metadata.version("rqalpha")
    except metadata.PackageNotFoundError as exc:  # pragma: no cover - 环境异常分支
        raise UnsupportedRQAlphaError("未检测到 rqalpha 安装包,请先执行 pip install -e '.[dev]'") from exc


def supported_versions() -> tuple[str, ...]:
    return tuple(dict.fromkeys((*SUPPORTED_VERSIONS, *_PROFILES)))


def register_profile(profile: CompatProfile) -> None:
    """登记自定义 profile;升级时可用 replace(base, version=...) 快速派生。"""
    _PROFILES[profile.version] = profile


def _profile_from_installed(version: str) -> CompatProfile:
    """从当前安装包读取 dtype 事实,生成 best-effort profile。"""
    from rqalpha.data.base_data_source.storages import (  # 延迟导入:只在真正需要引擎时加载
        INDXDayBarStore,
        SecuritiesDayBarStore,
    )

    return CompatProfile(
        version=version,
        stock_bar_dtype=SecuritiesDayBarStore.DEFAULT_DTYPE,
        index_bar_dtype=INDXDayBarStore.DEFAULT_DTYPE,
    )


def current_profile(version: str | None = None) -> CompatProfile:
    """返回当前(或指定)版本的 profile;不兼容时给出可操作的中文错误。"""
    target = version or installed_rqalpha_version()
    if target not in _PROFILES:
        if target not in SUPPORTED_VERSIONS and not allow_unknown():
            raise UnsupportedRQAlphaError(
                f"当前 rqalpha {target} 尚未纳入平台兼容清单 {list(supported_versions())}。"
                f"请在隔离环境先用 scripts/upgrade_rqalpha.sh {target} 验证; "
                f"临时验证可设置 {ALLOW_ENV}=1(结果仅供研究)。"
            )
        _PROFILES[target] = _profile_from_installed(target)
    return _PROFILES[target]


def check_rqalpha_version() -> CompatProfile:
    """回测/构建入口调用:版本不受支持时立即失败,不进入撮合。"""
    return current_profile()


def version_report() -> dict[str, Any]:
    """供 /api/health 与升级脚本消费的非抛异常版本报告。"""
    allowed = allow_unknown()
    try:
        installed = installed_rqalpha_version()
    except UnsupportedRQAlphaError as exc:
        return {
            "status": "missing",
            "installed": "unknown",
            "supported": list(supported_versions()),
            "allow_unknown": allowed,
            "detail": str(exc),
        }
    try:
        profile = current_profile(installed)
    except UnsupportedRQAlphaError as exc:
        return {
            "status": "unsupported",
            "installed": installed,
            "supported": list(supported_versions()),
            "allow_unknown": allowed,
            "detail": str(exc),
        }
    return {
        "status": "ok" if installed in SUPPORTED_VERSIONS else "allowed",
        "installed": installed,
        "supported": list(supported_versions()),
        "allow_unknown": allowed,
        "profile": profile.version,
    }


def main() -> None:
    report = version_report()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(1 if report["status"] in {"unsupported", "missing"} else 0)


__all__ = [
    "ALLOW_ENV",
    "SUPPORTED_VERSIONS",
    "YIELD_CURVE_TENORS",
    "BENCHMARK_FALLBACK_OID",
    "MOD_CONFIG_SECTIONS",
    "SUMMARY_KEYS",
    "CompatProfile",
    "UnsupportedRQAlphaError",
    "allow_unknown",
    "check_rqalpha_version",
    "current_profile",
    "installed_rqalpha_version",
    "register_profile",
    "replace",
    "supported_versions",
    "version_report",
]


if __name__ == "__main__":
    main()
