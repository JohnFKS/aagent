"""回测准确性验证套件(闭式对照)测试:合成 bundle,不打网络。"""

from __future__ import annotations

import pytest

from aquant.verify.accuracy import run_accuracy_checks, sell_tax_rate


def test_sell_tax_rate_pit():
    """PIT 印花税率:2023-08-28 前千一、后万五。"""
    assert sell_tax_rate("2023-08-27") == 0.001
    assert sell_tax_rate("2023-08-28") == 0.0005
    assert sell_tax_rate("2024-06-28") == 0.0005


def test_accuracy_suite_all_passed(tmp_path, monkeypatch):
    """合成 bundle 上全流程自检:闭式手算与平台输出逐项一致。"""
    from aquant.data.bundle import build_bundle
    from aquant.data.synthetic_source import SyntheticSource

    src = SyntheticSource(n_instruments=2, start="2023-01-03", end="2023-03-31", seed=23)
    bundle = build_bundle([src], tmp_path / "bundle")
    monkeypatch.delenv("AQUANT_BUNDLE_PATH", raising=False)

    result = run_accuracy_checks(bundle_path=bundle, cash=1_000_000.0, shares=100)
    assert result["checks"], "应产出检查项"
    failed = [c for c in result["checks"] if not c["pass"]]
    assert result["all_passed"], f"存在未通过检查:{[(c['name'], c['expected'], c['actual']) for c in failed]}"
    # 关键闭式项确实验证到了
    names = {c["name"] for c in result["checks"]}
    assert any("买入价" in n for n in names)
    assert any("PIT" in n for n in names)
    assert any("期末总资产" in n for n in names)
    assert "准确性验证报告" in result["report_md"]


def test_accuracy_suite_missing_bundle_graceful(tmp_path):
    """bundle 缺失 → 优雅降级(可读报告,不抛异常)。"""
    result = run_accuracy_checks(bundle_path=tmp_path / "no-such-bundle")
    assert not result["all_passed"]
    assert "bundle 不可用" in result["report_md"]
    assert result["checks"] == []
