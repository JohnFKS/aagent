"""基准合约查询(aquant.data.instruments)单元测试:label 解析与预校验。"""

from __future__ import annotations

import pickle

import pytest

from aquant.data.instruments import (
    benchmark_exists,
    benchmark_label,
    instrument_symbol,
    load_instruments,
)


@pytest.fixture()
def bundle_dir(tmp_path):
    """最小 instruments.pk bundle(一只股票 + 一只指数合约)。"""
    insts = [
        {
            "order_book_id": "600519.XSHG",
            "symbol": "贵州茅台",
            "type": "CS",
            "listed_date": "2001-08-27",
            "de_listed_date": "0000-00-00",
            "round_lot": 100,
            "exchange": "XSHG",
        },
        {
            "order_book_id": "000300.XSHG",
            "symbol": "沪深300",
            "type": "INDX",
            "listed_date": "2005-04-08",
            "de_listed_date": "0000-00-00",
            "round_lot": 1,
            "exchange": "XSHG",
        },
    ]
    with open(tmp_path / "instruments.pk", "wb") as f:
        pickle.dump(insts, f)
    return tmp_path


def test_load_instruments(bundle_dir):
    table = load_instruments(bundle_dir)
    assert set(table) == {"600519.XSHG", "000300.XSHG"}
    assert table["600519.XSHG"]["symbol"] == "贵州茅台"


def test_instrument_symbol_and_fallback(bundle_dir):
    assert instrument_symbol("600519.XSHG", bundle_dir) == "贵州茅台"
    assert instrument_symbol("999999.XSHE", bundle_dir) is None
    # bundle 缺失 → 空表,None 回退
    assert instrument_symbol("600519.XSHG", bundle_dir / "nope") is None


def test_benchmark_label_precedence(bundle_dir):
    # 内置指数映射优先于 bundle 合约名
    assert benchmark_label("000001.XSHG", bundle_dir) == "上证指数"
    # bundle 合约名(此处沪深300 恰好同名,用茅台验证 bundle 命中)
    assert benchmark_label("600519.XSHG", bundle_dir) == "贵州茅台"
    # 未知 oid 回退裸 oid
    assert benchmark_label("999999.XSHE", bundle_dir) == "999999.XSHE"
    assert benchmark_label(None, bundle_dir) == ""


def test_benchmark_exists(bundle_dir):
    assert benchmark_exists("600519.XSHG", bundle_dir)
    # 内置指数恒存在(即使 bundle 里没注册)
    assert benchmark_exists("000001.XSHG", bundle_dir)
    assert not benchmark_exists("999999.XSHE", bundle_dir)
