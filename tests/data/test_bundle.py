"""bundle.py 构建器测试:逐文件对照 rqalpha storages 类常量校验格式 + E2E 真实回测。

E2E 终极测试直接调用 rqalpha.run_file(不经 aquant.runner,那是协调者范围):
- 合成 bundle 上跑通日线回测,断言成本(commission/tax)与成交量限制生效;
- 策略模板已在开发期人工验证(见模块底部常量)。
"""

from __future__ import annotations

import pickle
import sys
import textwrap
from datetime import date
from pathlib import Path

import h5py
import numpy as np
import pandas as pd
import pytest

from aquant.data.adjust import EX_CUM_FACTOR_DTYPE, date_to_int14
from aquant.data.bundle import BENCHMARK_FALLBACK_OID, build_bundle
from aquant.data.limits import price_limits
from aquant.data.synthetic_source import SyntheticSource

# rqalpha 类常量 = dtype 事实来源(与 bundle.py 引用同一处,不硬编码期望 dtype)
from rqalpha.data.base_data_source.storages import (
    DateSet,
    DividendStore,
    FutureDayBarStore,
    FutureInfoStore,
    INDXDayBarStore,
    SecuritiesDayBarStore,
    ShareTransformationStore,
    SimpleFactorStore,
    YieldCurveStore,
    load_instruments_from_pkl,
)

BUNDLE_START, BUNDLE_END = "2023-01-01", "2024-12-31"

E2E_MA_STRATEGY = textwrap.dedent(
    """
    # 简单 MA 风格择时:第 10 个交易日起全仓(95%)买入,第 30 日起清仓卖出。
    from rqalpha.const import POSITION_DIRECTION

    def init(context):
        context.s1 = "000001.XSHE"
        context.day = 0
        context.bought = False

    def handle_bar(context, bar_dict):
        context.day += 1
        if is_suspended(context.s1):
            return
        pos = context.portfolio.get_position(context.s1, POSITION_DIRECTION.LONG)
        if not context.bought and context.day >= 10:
            order = order_target_percent(context.s1, 0.95)
            if order is not None and order.filled_quantity > 0:
                context.bought = True
        elif context.bought and context.day >= 30 and pos.quantity > 0:
            order_target_percent(context.s1, 0)
    """
)

E2E_VOLUME_STRATEGY = textwrap.dedent(
    """
    # 成交量限制验证:下单量(1000 万股)远超单 bar 成交量(约 200 万股),
    # 且资金充足(避免现金约束干扰),实际成交必须 < 下单量。
    def init(context):
        context.s1 = "000001.XSHE"
        context.done = False

    def handle_bar(context, bar_dict):
        if context.done:
            return
        if is_suspended(context.s1):
            return
        order = order_shares(context.s1, 10_000_000)
        if order is not None and order.filled_quantity > 0:
            context.done = True
    """
)


@pytest.fixture(scope="module")
def source() -> SyntheticSource:
    return SyntheticSource(n_instruments=2, start=BUNDLE_START, end=BUNDLE_END, seed=42)


@pytest.fixture(scope="module")
def bundle(source: SyntheticSource, tmp_path_factory) -> Path:
    return build_bundle([source], tmp_path_factory.mktemp("bundle"))


def _calendar(source: SyntheticSource) -> pd.DatetimeIndex:
    dates: set[pd.Timestamp] = set()
    for inst in source.instruments():
        dates.update(source.daily_raw(inst["order_book_id"]).index)
    return pd.DatetimeIndex(sorted(dates))


class TestBundleFiles:
    def test_all_required_files_exist(self, bundle):
        expected = {
            "stocks.h5", "indexes.h5", "funds.h5", "futures.h5",
            "dividends.h5", "split_factor.h5", "ex_cum_factor.h5",
            "suspended_days.h5", "st_stock_days.h5", "trading_dates.npy",
            "instruments.pk", "yield_curve.h5", "future_info.json",
            "share_transformation.json",
        }
        actual = {p.name for p in bundle.iterdir() if p.is_file()}
        assert expected <= actual

    def test_return_path(self, source, tmp_path):
        out = build_bundle([source], tmp_path / "b")
        assert out == tmp_path / "b" and out.is_dir()


class TestStocksH5:
    def test_dtype_matches_rqalpha_constant(self, bundle, source):
        oids = [i["order_book_id"] for i in source.instruments()]
        with h5py.File(bundle / "stocks.h5", "r") as h5:
            for oid in oids:
                assert h5[oid].dtype == SecuritiesDayBarStore.DEFAULT_DTYPE

    def test_prices_are_raw_and_match_source(self, bundle, source):
        """真实价(不复权)且与 source 逐值一致;停牌日不写行。"""
        store = SecuritiesDayBarStore(str(bundle / "stocks.h5"))
        for inst in source.instruments():
            oid = inst["order_book_id"]
            raw = source.daily_raw(oid)
            susp = set(source.suspended_days(oid))
            expected_dates = [d for d in raw.index if int(d.strftime("%Y%m%d")) not in susp]
            bars = store.get_bars(oid)
            assert len(bars) == len(expected_dates)
            np.testing.assert_allclose(bars["open"], raw.loc[expected_dates, "open"], rtol=0)
            np.testing.assert_allclose(bars["close"], raw.loc[expected_dates, "close"], rtol=0)
            np.testing.assert_allclose(bars["high"], raw.loc[expected_dates, "high"], rtol=0)
            np.testing.assert_allclose(bars["low"], raw.loc[expected_dates, "low"], rtol=0)
            np.testing.assert_allclose(bars["volume"], raw.loc[expected_dates, "volume"], rtol=0)
            np.testing.assert_allclose(
                bars["total_turnover"], raw.loc[expected_dates, "total_turnover"], rtol=0
            )
            # datetime 为 14 位 YYYYMMDD000000 且与预期日期序列一致
            np.testing.assert_array_equal(
                bars["datetime"], [date_to_int14(d) for d in expected_dates]
            )

    def test_suspended_rows_absent(self, bundle, source):
        with h5py.File(bundle / "stocks.h5", "r") as h5:
            for inst in source.instruments():
                oid = inst["order_book_id"]
                written = {int(str(d)[:8]) for d in h5[oid][:]["datetime"]}
                assert not (set(source.suspended_days(oid)) & written)

    def test_limits_recomputed_from_source_match(self, bundle, source):
        """limit_up/limit_down 与 limits.price_limits 重算结果一致;
        无 prev_close 的首日/无涨跌幅窗口行为超宽占位(close*10 / close*0.1)。"""
        calendar = _calendar(source)
        with h5py.File(bundle / "stocks.h5", "r") as h5:
            for inst in source.instruments():
                oid = inst["order_book_id"]
                st = set(source.st_days(oid))
                raw = source.daily_raw(oid)
                susp = set(source.suspended_days(oid))
                dates = [d for d in raw.index if int(d.strftime("%Y%m%d")) not in susp]
                bars = h5[oid][:]
                prev_close: float | None = None
                for i, dt in enumerate(dates):
                    close = float(raw.loc[dt, "close"])
                    if prev_close is None:
                        up, down = close * 10.0, max(close * 0.1, 0.01)
                    else:
                        limits = price_limits(
                            oid, prev_close, date=dt.date(),
                            is_st=(int(dt.strftime("%Y%m%d")) in st),
                            listed_date=pd.Timestamp(inst["listed_date"]),
                            trading_dates=[d.date() for d in calendar],
                        )
                        up, down = limits if limits is not None else (
                            close * 10.0, max(close * 0.1, 0.01)
                        )
                    assert bars[i]["limit_up"] == pytest.approx(up, abs=1e-9)
                    assert bars[i]["limit_down"] == pytest.approx(down, abs=1e-9)
                    assert bars[i]["limit_up"] > close > 0
                    prev_close = close

    def test_st_days_lower_limit_ratio(self, bundle, source):
        """ST 窗口内主板的涨跌停幅度为 ±5%(与 limits.py 规则一致)。"""
        last = source.instruments()[-1]["order_book_id"]
        st_days = source.st_days(last)
        assert st_days, "n=2 时最后一个合约应有 ST 窗口"
        raw = source.daily_raw(last)
        st_set = set(st_days)
        # ST 窗口首个有行情日:用其在行情序列中的前一日 close 计算主板 ST ±5%
        st_positions = [i for i, d in enumerate(raw.index)
                        if int(d.strftime("%Y%m%d")) in st_set]
        pos = st_positions[0]
        assert pos > 0
        prev = float(raw["close"].iloc[pos - 1])
        expected_up = round(prev * 1.05, 2)
        bars = SecuritiesDayBarStore(str(bundle / "stocks.h5")).get_bars(last)
        # bundle 中该 ST 日(跳过停牌后位置可能偏移,按 datetime 定位)
        target_dt = date_to_int14(raw.index[pos])
        idx = int(np.nonzero(bars["datetime"] == target_dt)[0][0])
        assert bars[idx]["limit_up"] == pytest.approx(expected_up, abs=1e-9)
        assert bars[idx]["limit_down"] == pytest.approx(round(prev * 0.95, 2), abs=1e-9)


class TestIndexesH5:
    def test_dtype_matches_indx_store(self, bundle):
        store = INDXDayBarStore(str(bundle / "indexes.h5"))
        bars = store.get_bars(BENCHMARK_FALLBACK_OID)
        assert len(bars) > 0
        # get_bars 取 DEFAULT_DTYPE 字段;原 dataset dtype 应精确等于 INDX 默认 dtype(无 limit 字段)
        with h5py.File(bundle / "indexes.h5", "r") as h5:
            assert h5[BENCHMARK_FALLBACK_OID].dtype == INDXDayBarStore.DEFAULT_DTYPE

    def test_index_covers_full_calendar(self, bundle, source):
        calendar = _calendar(source)
        with h5py.File(bundle / "indexes.h5", "r") as h5:
            ds = h5[BENCHMARK_FALLBACK_OID][:]
        assert len(ds) == len(calendar)
        np.testing.assert_array_equal(ds["datetime"], [date_to_int14(d) for d in calendar])
        assert (ds["close"] > 0).all()

    def test_empty_funds_and_futures_files_loadable(self, bundle):
        """funds.h5/futures.h5 必须存在(data_source.py 初始化强制),内容为空。"""
        assert len(SecuritiesDayBarStore(str(bundle / "funds.h5")).get_bars("X")) == 0
        assert len(FutureDayBarStore(str(bundle / "futures.h5")).get_bars("X")) == 0


class TestCorporateActions:
    def test_dividends_dataset(self, bundle, source):
        oid = source.instruments()[0]["order_book_id"]
        div = source.dividends(oid)
        assert len(div) == 1
        store = DividendStore(str(bundle / "dividends.h5"))
        arr = store.get_dividend(oid)
        assert arr is not None and len(arr) == 1
        row = arr[0]
        assert row["book_closure_date"] == int(div["book_closure_date"].iloc[0])
        assert row["announcement_date"] == int(div["announcement_date"].iloc[0])
        assert row["ex_dividend_date"] == int(div["ex_dividend_date"].iloc[0])
        assert row["payable_date"] == int(div["payable_date"].iloc[0])
        assert row["dividend_cash_before_tax"] == pytest.approx(
            float(div["dividend_cash_before_tax"].iloc[0])
        )
        assert row["round_lot"] == pytest.approx(float(div["round_lot"].iloc[0]))
        # 日期为 8 位 YYYYMMDD
        for f in ("book_closure_date", "announcement_date", "ex_dividend_date", "payable_date"):
            assert 19000101 <= row[f] <= 29991231
        # 无分红的合约无 dataset → DividendStore 返回 None
        other = source.instruments()[1]["order_book_id"]
        assert store.get_dividend(other) is None

    def test_split_factor_dataset(self, bundle, source):
        oid = source.instruments()[0]["order_book_id"]
        split = source.splits(oid)
        assert len(split) == 1
        arr = SimpleFactorStore(str(bundle / "split_factor.h5")).get_factors(oid)
        assert arr is not None and len(arr) == 1
        assert arr[0]["ex_date"] == int(split["ex_date"].iloc[0])
        assert arr[0]["split_factor"] == pytest.approx(float(split["split_factor"].iloc[0]))
        # ex_date 为 14 位
        assert arr[0]["ex_date"] > 19000101000000
        # 无拆股合约无 dataset
        other = source.instruments()[1]["order_book_id"]
        assert SimpleFactorStore(str(bundle / "split_factor.h5")).get_factors(other) is None

    def test_ex_cum_factor_segments(self, bundle, source):
        oid = source.instruments()[0]["order_book_id"]
        arr = SimpleFactorStore(str(bundle / "ex_cum_factor.h5")).get_factors(oid)
        assert arr is not None
        # 结构化 dtype 与 adjust.py 产出的常量一致
        assert EX_CUM_FACTOR_DTYPE == np.dtype(
            [("start_date", np.int64), ("ex_cum_factor", np.float64)]
        )
        raw = source.daily_raw(oid)
        factor = source.adjust_factor(oid)
        # 首段 = 数据首日;尾段因子 = source 因子末值;段数 = 变化点数 + 1
        assert arr["start_date"][0] == date_to_int14(raw.index[0])
        assert arr["ex_cum_factor"][-1] == pytest.approx(float(factor.iloc[-1]))
        values = factor.to_numpy()
        change_points = (np.nonzero(np.diff(values))[0] + 1).tolist()
        assert len(arr) == len(change_points) + 1
        for k, seg in enumerate(change_points):
            assert arr["start_date"][k + 1] == date_to_int14(raw.index[seg])
            assert arr["ex_cum_factor"][k + 1] == pytest.approx(values[seg])
        # start_date 为 14 位升序
        assert (np.diff(arr["start_date"]) > 0).all()


class TestDateSetsAndCalendar:
    def test_suspended_days_dataset(self, bundle, source):
        ds = DateSet(str(bundle / "suspended_days.h5"))
        for inst in source.instruments():
            oid = inst["order_book_id"]
            assert ds.get_days(oid) == set(source.suspended_days(oid))

    def test_st_stock_days_dataset(self, bundle, source):
        ds = DateSet(str(bundle / "st_stock_days.h5"))
        for inst in source.instruments():
            oid = inst["order_book_id"]
            assert ds.get_days(oid) == set(source.st_days(oid))
        # n=2 时第二个合约应有 ST 窗口(链路覆盖)
        assert ds.get_days(source.instruments()[-1]["order_book_id"])

    def test_trading_dates_npy(self, bundle, source):
        calendar = _calendar(source)
        dates = np.load(bundle / "trading_dates.npy", allow_pickle=False)
        assert dates.dtype == np.int64
        assert (np.diff(dates) > 0).all()
        np.testing.assert_array_equal(dates, [int(d.strftime("%Y%m%d")) for d in calendar])
        # rqalpha 日历 store 可直接加载
        from rqalpha.data.base_data_source.storages import ExchangeTradingCalendarStore

        cal = ExchangeTradingCalendarStore(str(bundle / "trading_dates.npy")).get_trading_calendar()
        assert len(cal) == len(calendar)
        assert cal[0] == calendar[0]


class TestInstrumentsPk:
    def test_instruments_loadable_by_rqalpha(self, bundle, source):
        fi = FutureInfoStore(str(bundle / "future_info.json"), {})
        instruments = load_instruments_from_pkl(str(bundle / "instruments.pk"), fi)
        oids = {i.order_book_id: i for i in instruments}
        for inst in source.instruments():
            oid = inst["order_book_id"]
            assert oid in oids
            ins = oids[oid]
            assert ins.type == "CS"
            assert ins.round_lot == inst["round_lot"]
            assert ins.listed_date.date() == date.fromisoformat(inst["listed_date"])
            # '0000-00-00' → rqalpha 默认 de_listed_date(2999-12-31)
            assert ins.de_listed_date == pd.Timestamp("2999-12-31").to_pydatetime()
        # 基准指数以 INDX 类型注册
        assert BENCHMARK_FALLBACK_OID in oids
        assert oids[BENCHMARK_FALLBACK_OID].type == "INDX"

    def test_pickle_is_list_of_dicts(self, bundle):
        with open(bundle / "instruments.pk", "rb") as f:
            raw = pickle.load(f)
        assert isinstance(raw, list) and all(isinstance(d, dict) for d in raw)
        for d in raw:
            assert {"order_book_id", "symbol", "type", "listed_date",
                    "de_listed_date", "round_lot"} <= set(d)


class TestYieldCurveAndJson:
    def test_yield_curve_store(self, bundle):
        store = YieldCurveStore(str(bundle / "yield_curve.h5"))
        calendar_dates = np.load(bundle / "trading_dates.npy")
        mid = str(calendar_dates[len(calendar_dates) // 2])
        d = date(int(mid[:4]), int(mid[4:6]), int(mid[6:8]))
        curve = store.get_yield_curve(d, d, tenor=None)
        assert curve is not None and len(curve) == 1
        for tenor in ("0S", "1M", "3M", "6M", "1Y", "2Y", "3Y", "5Y", "10Y"):
            assert curve[tenor].iloc[0] == pytest.approx(0.02)
        with h5py.File(bundle / "yield_curve.h5", "r") as h5:
            data = h5["data"][:]
        assert data.dtype.names[0] == "date"
        assert set(data.dtype.names[1:]) >= {"0S", "1M", "3M", "6M", "1Y", "2Y", "3Y", "5Y", "10Y"}
        assert (np.diff(data["date"]) > 0).all()
        assert 19000101 <= int(data["date"][0]) <= 29991231

    def test_future_info_json_not_empty(self, bundle):
        """FutureInfoStore 对空列表会 StopIteration,必须含 margin_rate 条目。"""
        from rqalpha.const import COMMISSION_TYPE

        custom = {}
        fi = FutureInfoStore(str(bundle / "future_info.json"), custom)
        params = fi.get_future_info("SYNDEM00", "SYNDEM")
        assert params.commission_type == COMMISSION_TYPE.BY_VOLUME
        assert params.long_margin_ratio == pytest.approx(0.10)
        assert params.open_commission_ratio == pytest.approx(1.0)
        assert fi.get_tick_size(type("X", (), {
            "order_book_id": "SYNDEM00", "underlying_symbol": "SYNDEM"})) == pytest.approx(1.0)

    def test_share_transformation_empty_dict(self, bundle):
        store = ShareTransformationStore(str(bundle / "share_transformation.json"))
        assert store.get_share_transformation("000001.XSHE") is None


class TestNoLimitWindowBundle:
    def test_gem_first_days_use_wide_limits(self, source, tmp_path):
        """创业板前 5 个交易日无涨跌幅 → bundle 写超宽占位;第 6 根起按 ±20% 计算。"""
        gem = SyntheticSource(n_instruments=1, start=BUNDLE_START, end=BUNDLE_END,
                              seed=7, codes=["300001.XSHE"])
        out = build_bundle([gem], tmp_path / "gem")
        raw = gem.daily_raw("300001.XSHE")
        bars = SecuritiesDayBarStore(str(out / "stocks.h5")).get_bars("300001.XSHE")
        for i in range(5):
            close = float(raw["close"].iloc[i])
            assert bars[i]["limit_up"] == pytest.approx(close * 10.0)
            assert bars[i]["limit_down"] == pytest.approx(max(close * 0.1, 0.01))
        # 第 6 根(第 5 个交易日,day_number=5)恢复有效涨跌停
        prev = float(raw["close"].iloc[4])
        limits = price_limits("300001.XSHE", prev, date=raw.index[5].date())
        assert bars[5]["limit_up"] == pytest.approx(limits[0])
        assert bars[5]["limit_down"] == pytest.approx(limits[1])


# ----------------------------------------------------------------------
# E2E 终极测试:run_file 直接跑 rqalpha 回测(不经 aquant.runner)
# ----------------------------------------------------------------------
def _run_e2e(bundle: Path, strategy_code: str, tmp_dir: Path, cash: float) -> dict:
    from rqalpha import run_file

    strategy_path = tmp_dir / "e2e_strategy.py"
    strategy_path.write_text(strategy_code, encoding="utf-8")
    result_file = tmp_dir / "result.pkl"
    config = {
        "base": {
            "start_date": "2023-06-01",
            "end_date": "2024-12-31",
            "frequency": "1d",
            "accounts": {"STOCK": cash},
            "data_bundle_path": str(bundle),
        },
        "mod": {
            "sys_simulation": {"slippage": 0.002},  # 平台口径:比例滑点,禁零滑点
            "sys_analyser": {"output_file": str(result_file), "plot": False},
        },
    }
    run_file(str(strategy_path), config)
    assert result_file.exists()
    with open(result_file, "rb") as f:
        return pickle.load(f)


class TestE2EBacktest:
    def test_ma_strategy_full_run(self, bundle, tmp_path):
        """合成 bundle 上真实跑通 rqalpha 回测:summary/portfolio/trades 齐备,成本生效。"""
        result = _run_e2e(bundle, E2E_MA_STRATEGY, tmp_path, cash=1_000_000)

        summary = result["summary"]
        total_returns = summary["total_returns"]
        assert np.isfinite(total_returns)
        assert np.isfinite(result["summary"]["max_drawdown"])
        assert len(result["portfolio"]) > 100, "portfolio 每个交易日应有一条记录"
        trades = result["trades"]
        assert len(trades) >= 2, "MA 策略应产生买入与卖出两笔以上交易"
        # 成本生效:commission/tax 字段存在且 ≥0(实际必然 >0:万八佣金 + 最低 5 元 + 千一印花)
        assert "commission" in trades.columns
        assert (trades["commission"] >= 0).all() and (trades["commission"] > 0).all()
        assert "tax" in trades.columns and (trades["tax"] >= 0).all()
        # 买入在先,卖出在后;T+1 下不可能当日回转
        assert trades.iloc[0]["side"] == "BUY" and trades.iloc[-1]["side"] == "SELL"
        assert np.isfinite(result["summary"]["total_value"])

    def test_volume_limit_enforced(self, bundle, tmp_path):
        """下单量远超 bar 成交量 → 实际成交 < 下单(成交量限制生效)。"""
        result = _run_e2e(bundle, E2E_VOLUME_STRATEGY, tmp_path, cash=2_000_000_000)
        trades = result["trades"]
        assert len(trades) >= 1
        filled = float(trades["last_quantity"].sum())
        assert 0 < filled < 10_000_000, (
            f"下单 10,000,000 股,实际成交 {filled:.0f} 股,成交量限制应把成交压到 25% 以下"
        )
        # rqalpha 默认 volume_percent=0.25;合成数据单 bar 成交量 ≤ 3e6 → 成交 ≤ 0.26×3e6
        assert filled <= 0.26 * 3_000_000
        assert (trades["commission"] > 0).all()


class TestCli:
    def test_build_bundle_cli_synthetic(self, tmp_path, capsys):
        """CLI 全链路:构建 + 产物清单 + rqalpha 自检(instruments 与 stocks 首行)。"""
        from aquant.data.build_bundle import main

        out = tmp_path / "cli-bundle"
        code = main([
            "--source", "synthetic", "--out", str(out),
            "--n", "2", "--start", "2023-01-01", "--end", "2023-12-31",
        ])
        assert code == 0
        captured = capsys.readouterr()
        assert "instruments 加载成功" in captured.out
        assert "stocks.h5 首行" in captured.out
        assert (out / "stocks.h5").exists()


# ----------------------------------------------------------------------
# AkshareSource:monkeypatch/stub 模拟 akshare 模块,不打网络
# ----------------------------------------------------------------------
FAKE_CALENDAR_DAYS = [
    "2023-01-02", "2023-01-03", "2023-01-04", "2023-01-05",
    "2023-01-06", "2023-01-09", "2023-01-10", "2023-01-11",
]

FAKE_RAW_ROWS = {  # 真实价;2023-01-05 停牌(相对日历缺失),2023-01-10 除息
    "2023-01-02": (10.0, 10.2, 10.4, 9.9, 20000, 2040000),
    "2023-01-03": (10.2, 10.5, 10.6, 10.1, 22000, 2310000),
    "2023-01-04": (10.5, 10.3, 10.7, 10.2, 18000, 1890000),
    "2023-01-06": (10.3, 10.8, 10.9, 10.2, 25000, 2675000),
    "2023-01-09": (10.8, 10.6, 11.0, 10.5, 21000, 2226000),
    "2023-01-10": (10.6, 10.1, 10.7, 10.0, 24000, 2496000),  # 除息日 close 回落
    "2023-01-11": (10.1, 10.3, 10.5, 10.0, 19000, 1966000),
}
FAKE_HFQ_ROWS = {  # 后复权:除息日前 hfq=raw(因子 1.0);除息日因子跳至 10.6/10.1,hfq 无跳空
    "2023-01-02": 10.2, "2023-01-03": 10.5, "2023-01-04": 10.3,
    "2023-01-06": 10.8, "2023-01-09": 10.6, "2023-01-10": 10.6, "2023-01-11": 10.81,
}


def _fake_daily_df(rows: dict, symbol: str, start: str, end: str, hfq: bool) -> pd.DataFrame:
    dates = [d for d in sorted(rows) if start <= d.replace("-", "") <= end]
    if hfq:
        data = {"日期": dates, "开盘": [rows[d] for d in dates], "收盘": [rows[d] for d in dates],
                "最高": [rows[d] for d in dates], "最低": [rows[d] for d in dates],
                "成交量": [1] * len(dates), "成交额": [1] * len(dates)}
    else:
        data = {"日期": dates,
                "开盘": [rows[d][0] for d in dates], "收盘": [rows[d][1] for d in dates],
                "最高": [rows[d][2] for d in dates], "最低": [rows[d][3] for d in dates],
                "成交量": [rows[d][4] for d in dates], "成交额": [rows[d][5] for d in dates]}
    data["股票代码"] = symbol
    return pd.DataFrame(data)


class FakeAkshareModule:
    """akshare 桩:复刻 stock_zh_a_hist/tool_trade_date_hist_sina/stock_fh_dividend 形态。"""

    def __init__(self, raw_rows=FAKE_RAW_ROWS, hfq_rows=FAKE_HFQ_ROWS, with_dividend=True):
        self._raw, self._hfq = raw_rows, hfq_rows
        self.with_dividend = with_dividend
        self.calls: list[tuple] = []

    def tool_trade_date_hist_sina(self):
        return pd.DataFrame({"trade_date": [pd.Timestamp(d).date() for d in FAKE_CALENDAR_DAYS]})

    def stock_zh_a_hist(self, symbol, period, start_date, end_date, adjust):
        self.calls.append((symbol, start_date, end_date, adjust))
        rows = self._hfq if adjust == "hfq" else self._raw
        return _fake_daily_df(rows, symbol, start_date, end_date, hfq=(adjust == "hfq"))

    def stock_fh_dividend(self, symbol):
        if not self.with_dividend:
            raise RuntimeError("interface down")
        return pd.DataFrame({
            "公告日期": ["2023-01-04"],
            "股权登记日": ["2023-01-09"],
            "除权除息日": ["2023-01-10"],
            "派息日": ["2023-01-13"],
            "每股股利": [0.5],  # 每股 0.5 元
        })


@pytest.fixture()
def akshare_env(monkeypatch, tmp_path):
    """注入 fake akshare 模块 + 关闭限速 sleep。"""
    import aquant.data.akshare_source as ak_mod

    fake = FakeAkshareModule()
    monkeypatch.setitem(sys.modules, "akshare", fake)
    monkeypatch.setattr(ak_mod, "_sleep", lambda seconds: None)
    return fake


class TestAkshareSource:
    def test_code_normalization(self):
        from aquant.data.akshare_source import AkshareSource

        assert AkshareSource._normalize_code("000001") == "000001.XSHE"
        assert AkshareSource._normalize_code("600519") == "600519.XSHG"
        assert AkshareSource._normalize_code("300750") == "300750.XSHE"
        assert AkshareSource._normalize_code("688981") == "688981.XSHG"
        assert AkshareSource._normalize_code("920001") == "920001.BJ"
        assert AkshareSource._normalize_code("000001.XSHE") == "000001.XSHE"
        assert AkshareSource._normalize_code("600519.XSHG") == "600519.XSHG"
        with pytest.raises(ValueError):
            AkshareSource._normalize_code("1234")

    def test_daily_raw_mapping(self, akshare_env):
        """中文列 → DAILY_COLUMNS 口径;成交量 手→股;真实价(不复权)。"""
        from aquant.data.provider import DAILY_COLUMNS
        from aquant.data.akshare_source import AkshareSource

        src = AkshareSource(codes=["000001"], start="2023-01-01", end="2023-01-31")
        df = src.daily_raw("000001.XSHE")
        assert list(df.columns) == list(DAILY_COLUMNS)
        assert df.index.is_monotonic_increasing
        np.testing.assert_allclose(df["close"], [r[1] for r in FAKE_RAW_ROWS.values()])
        np.testing.assert_allclose(df["volume"], [r[4] * 100.0 for r in FAKE_RAW_ROWS.values()])
        np.testing.assert_allclose(df["total_turnover"], [r[5] for r in FAKE_RAW_ROWS.values()])
        # 请求参数:真实价一次 + hfq 一次,日期为 YYYYMMDD
        assert (a := akshare_env.calls[0])[3] == "" and (b := akshare_env.calls[1])[3] == "hfq"
        assert a[1] == "20230101" and a[2] == "20230131"

    def test_adjust_factor_alignment(self, akshare_env):
        """factor = hfq_close / raw_close,按 raw 日期对齐,除息日跳变后单调不减。"""
        from aquant.data.akshare_source import AkshareSource

        src = AkshareSource(codes=["000001"], start="2023-01-01", end="2023-01-31")
        factor = src.adjust_factor("000001.XSHE")
        raw = src.daily_raw("000001.XSHE")
        hfq_close = pd.Series(FAKE_HFQ_ROWS)
        hfq_close.index = pd.to_datetime(hfq_close.index)
        hfq_close = hfq_close.reindex(raw.index)
        np.testing.assert_allclose(factor.to_numpy(), (hfq_close / raw["close"]).to_numpy())
        assert (np.diff(factor.to_numpy()) >= 0).all()
        # 除息日(2023-01-10)因子跳变:跳变比 = 10.6/10.1(每股派 0.5 元,hfq 无跳空)
        assert factor.iloc[4] == pytest.approx(1.0)
        assert factor.iloc[5] / factor.iloc[4] == pytest.approx(10.6 / 10.1, rel=1e-6)
        assert factor.name == "adjust_factor"

    def test_suspended_days_from_missing_calendar_days(self, akshare_env):
        """2023-01-05 在日历中但无行情 → 停牌日。"""
        from aquant.data.akshare_source import AkshareSource

        src = AkshareSource(codes=["000001"], start="2023-01-01", end="2023-01-31")
        assert src.suspended_days("000001.XSHE") == [20230105]

    def test_dividends_mapping_best_effort(self, akshare_env):
        from aquant.data.akshare_source import AkshareSource

        src = AkshareSource(codes=["000001"], start="2023-01-01", end="2023-01-31")
        div = src.dividends("000001.XSHE")
        assert len(div) == 1
        row = div.iloc[0]
        # 每股 0.5 元 → 每 round_lot(100) 股 50 元
        assert row["dividend_cash_before_tax"] == pytest.approx(50.0)
        assert int(row["ex_dividend_date"]) == 20230110
        assert int(row["book_closure_date"]) == 20230109
        assert int(row["announcement_date"]) == 20230104
        assert int(row["payable_date"]) == 20230113
        assert int(row["round_lot"]) == 100

    def test_dividends_failure_degrades_to_empty_with_warning(self, monkeypatch):
        """分红接口失败 → 空表 + warnings.warn,主链路(daily/factor)不受影响。"""
        import aquant.data.akshare_source as ak_mod
        from aquant.data.akshare_source import AkshareSource

        monkeypatch.setitem(sys.modules, "akshare", FakeAkshareModule(with_dividend=False))
        monkeypatch.setattr(ak_mod, "_sleep", lambda seconds: None)
        with pytest.warns(UserWarning):
            src = AkshareSource(codes=["000001"], start="2023-01-01", end="2023-01-31")
            div = src.dividends("000001.XSHE")
        assert len(div) == 0
        assert list(div.columns) == [
            "book_closure_date", "announcement_date", "dividend_cash_before_tax",
            "ex_dividend_date", "payable_date", "round_lot",
        ]
        # 主链路不受影响
        assert len(src.daily_raw("000001.XSHE")) == 7

    def test_retry_then_success(self, akshare_env, monkeypatch):
        """失败 2 次后第 3 次成功 → 返回结果;3 次全败 → RuntimeError。"""
        import aquant.data.akshare_source as ak_mod
        from aquant.data.akshare_source import AkshareSource

        src = AkshareSource(codes=["000001"], start="2023-01-01", end="2023-01-31")
        attempts = {"n": 0}

        def flaky():
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise ConnectionError("boom")
            return "ok"

        with pytest.warns(UserWarning):
            assert src._request(flaky) == "ok"
        assert attempts["n"] == 3

        def always_fail():
            raise ConnectionError("boom")

        with pytest.warns(UserWarning), pytest.raises(RuntimeError):
            src._request(always_fail)

    def test_instruments_metadata(self, akshare_env):
        from aquant.data.akshare_source import AkshareSource

        src = AkshareSource(codes=["688001", "300001"], start="2023-01-01", end="2023-01-31")
        insts = {i["order_book_id"]: i for i in src.instruments()}
        assert set(insts) == {"688001.XSHG", "300001.XSHE"}
        assert insts["688001.XSHG"]["board_type"] == "KSH"
        assert insts["300001.XSHE"]["board_type"] == "GEM"
        for inst in insts.values():
            assert inst["type"] == "CS" and inst["round_lot"] == 100
            assert inst["market_tplus"] == 1 and inst["de_listed_date"] == "0000-00-00"
            assert inst["listed_date"] <= insts["688001.XSHG"]["listed_date"]

    def test_akshare_import_error_hint(self, monkeypatch):
        """未安装 akshare → ImportError 且提示 pip install 'aquant[akshare]'。"""
        import aquant.data.akshare_source as ak_mod
        from aquant.data.akshare_source import AkshareSource

        monkeypatch.setitem(sys.modules, "akshare", None)  # import akshare → ImportError
        src = AkshareSource(codes=["000001"], start="2023-01-01", end="2023-01-31")
        with pytest.raises(ImportError, match=r"aquant\[akshare\]"):
            src.daily_raw("000001.XSHE")

    def test_akshare_to_bundle_pipeline(self, akshare_env, tmp_path):
        """桩数据源 → build_bundle → rqalpha storages 可加载(全管线,零网络)。"""
        from aquant.data.akshare_source import AkshareSource

        src = AkshareSource(codes=["000001"], start="2023-01-01", end="2023-01-31")
        out = build_bundle([src], tmp_path / "ak-bundle")
        with h5py.File(out / "stocks.h5", "r") as h5:
            assert h5["000001.XSHE"].dtype == SecuritiesDayBarStore.DEFAULT_DTYPE
            bars = h5["000001.XSHE"][:]
        assert len(bars) == 7  # 停牌日 2023-01-05 不写行
        fi = FutureInfoStore(str(out / "future_info.json"), {})
        instruments = load_instruments_from_pkl(str(out / "instruments.pk"), fi)
        assert any(i.order_book_id == "000001.XSHE" and i.type == "CS" for i in instruments)
