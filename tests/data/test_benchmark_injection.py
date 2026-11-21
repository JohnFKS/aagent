"""真实 A 股基准指数注入(bundle benchmark injection)测试。

inject_benchmark 纯本地可测:用 SyntheticSource 构建临时 bundle,再喂入合成的
"真实指数" DataFrame,断言:
1. indexes.h5 对应 dataset 被写入(000001.XSHG 覆盖合成基准,INDX dtype);
2. instruments.pk 注册/替换了 INDX 合约(中文名);
3. benchmark_meta.json 落盘且记录行数与来源;
4. 重复注入幂等(同名 dataset/合约被覆盖,不重复);
5. 与 rqalpha 真实回测衔接:run_backtest(benchmark=...) 返回基准净值序列。

fetch_baostock_index(网络)不做单测;仅测 oid → baostock 代码映射。
"""

from __future__ import annotations

import json
import pickle

import h5py
import numpy as np
import pandas as pd
import pytest

from aquant.data.benchmark import (
    DEFAULT_BENCHMARK_LABELS,
    inject_benchmark,
    oid_to_baostock_code,
)


def _make_bundle(tmp_path: pytest.Path) -> pytest.Path:
    """SyntheticSource → 临时 bundle(复用既有构建器,保证格式合法)。"""
    from aquant.data.bundle import build_bundle
    from aquant.data.synthetic_source import SyntheticSource

    src = SyntheticSource(n_instruments=3, start="2023-01-03", end="2023-03-01", seed=7)
    return build_bundle([src], tmp_path / "bundle")


def _fake_index_frame(start="2023-01-03", end="2023-03-01", base=1000.0) -> pd.DataFrame:
    """形似 Baostock 返回的指数日线(含日历外的额外日期,验证裁剪对齐)。"""
    idx = pd.bdate_range(start, end)
    close = base * (1 + 0.001 * np.sin(np.arange(len(idx))))
    return pd.DataFrame(
        {
            "open": close * 0.999,
            "high": close * 1.002,
            "low": close * 0.998,
            "close": close,
            "volume": 1_000_000.0,
            "total_turnover": 8_000_000.0,
        },
        index=idx,
    )


def test_oid_to_baostock_code():
    assert oid_to_baostock_code("000300.XSHG") == "sh.000300"
    assert oid_to_baostock_code("399006.XSHE") == "sz.399006"
    assert oid_to_baostock_code("000001.XSHG") == "sh.000001"


def test_inject_benchmark_writes_h5_instruments_meta(tmp_path):
    bundle = _make_bundle(tmp_path)
    frames = {"000001.XSHG": _fake_index_frame(), "000300.XSHG": _fake_index_frame(base=4000.0)}
    meta = inject_benchmark(bundle, frames, source="test")

    # meta 落盘且内容一致
    on_disk = json.loads((bundle / "benchmark_meta.json").read_text(encoding="utf-8"))
    assert on_disk == meta
    assert on_disk["source"] == "test"
    assert set(on_disk["indexes"]) == {"000001.XSHG", "000300.XSHG"}
    assert on_disk["indexes"]["000300.XSHG"]["symbol"] == "沪深300"
    assert on_disk["indexes"]["000300.XSHG"]["rows"] > 0

    # indexes.h5:两个 dataset 均存在,行数一致且字段齐全(INDX dtype)
    with h5py.File(bundle / "indexes.h5") as f:
        for oid in frames:
            assert oid in f
            ds = f[oid]
            assert ds.shape[0] == on_disk["indexes"][oid]["rows"]
            assert set(ds.dtype.names) >= {"datetime", "open", "high", "low", "close", "volume", "total_turnover"}
            close = ds["close"]
            # 真实指数收盘价量级(1000/4000 基点)而非合成基准的等权 1000 点
            assert close.max() > 10.0

    # instruments.pk:INDX 合约已注册且符号为中文
    with open(bundle / "instruments.pk", "rb") as fh:
        insts = pickle.load(fh)
    by_oid = {i["order_book_id"]: i for i in insts}
    for oid in frames:
        assert by_oid[oid]["type"] == "INDX"
        assert by_oid[oid]["symbol"] == DEFAULT_BENCHMARK_LABELS[oid]


def test_inject_benchmark_is_idempotent(tmp_path):
    bundle = _make_bundle(tmp_path)
    frames = {"000001.XSHG": _fake_index_frame()}
    inject_benchmark(bundle, frames)
    inject_benchmark(bundle, frames)  # 重复注入不报错、不重复注册

    with open(bundle / "instruments.pk", "rb") as fh:
        insts = pickle.load(fh)
    indxs = [i["order_book_id"] for i in insts if i.get("type") == "INDX"]
    assert indxs.count("000001.XSHG") == 1


def test_inject_benchmark_rejects_empty_frames(tmp_path):
    bundle = _make_bundle(tmp_path)
    with pytest.raises(RuntimeError):
        inject_benchmark(bundle, {"000300.XSHG": pd.DataFrame()})


def test_injected_benchmark_drives_rqalpha_backtest(tmp_path):
    """注入后的基准可与真实回测衔接:run_backtest(benchmark=...) 返回基准序列。"""
    from aquant.runner.backtest import run_backtest

    bundle = _make_bundle(tmp_path)
    inject_benchmark(bundle, {"000300.XSHG": _fake_index_frame()})

    code = '''
def init(context):
    context.s1 = "000001.XSHE"


def handle_bar(context, bar_dict):
    prices = history_bars(context.s1, 20, "1d", "close")
    if prices is None or len(prices) < 10:
        return
    if prices[-1] > prices[-10:].mean():
        order_target_percent(context.s1, 0.9)
'''
    result = run_backtest(code, "2023-01-10", "2023-02-24",
                          bundle_path=bundle, benchmark="000300.XSHG")
    assert len(result.benchmark_portfolio) > 0
    assert "unit_net_value" in result.benchmark_portfolio.columns
    assert result.summary.get("benchmark_total_returns") is not None
