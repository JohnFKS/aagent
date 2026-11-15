"""SyntheticSource 测试:确定性、协议满足、分红/拆股、因子单调、复权无跳空。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from aquant.data.adjust import ex_cum_factor_from
from aquant.data.provider import DAILY_COLUMNS, BarSource
from aquant.data.synthetic_source import SyntheticSource


@pytest.fixture(scope="module")
def source() -> SyntheticSource:
    return SyntheticSource(n_instruments=4, start="2023-06-01", end="2024-12-31", seed=42)


class TestProtocolAndDeterminism:
    def test_isinstance_bar_source(self, source):
        """实现 BarSource 协议(runtime_checkable)。"""
        assert isinstance(source, BarSource)

    def test_deterministic_same_seed(self):
        """同 seed 两次实例化,数据逐值相等。"""
        a = SyntheticSource(n_instruments=3, start="2023-06-01", end="2024-12-31", seed=7)
        b = SyntheticSource(n_instruments=3, start="2023-06-01", end="2024-12-31", seed=7)
        for inst in a.instruments():
            oid = inst["order_book_id"]
            pd.testing.assert_frame_equal(a.daily_raw(oid), b.daily_raw(oid))
            pd.testing.assert_series_equal(a.adjust_factor(oid), b.adjust_factor(oid))
            assert a.suspended_days(oid) == b.suspended_days(oid)
            assert a.st_days(oid) == b.st_days(oid)
            pd.testing.assert_frame_equal(a.dividends(oid), b.dividends(oid))
            pd.testing.assert_frame_equal(a.splits(oid), b.splits(oid))

    def test_different_seed_differs(self):
        a = SyntheticSource(n_instruments=2, start="2023-06-01", end="2024-12-31", seed=1)
        b = SyntheticSource(n_instruments=2, start="2023-06-01", end="2024-12-31", seed=2)
        oid = a.instruments()[0]["order_book_id"]
        assert not a.daily_raw(oid)["close"].equals(b.daily_raw(oid)["close"])

    def test_instruments_standard_dict(self, source):
        insts = source.instruments()
        assert len(insts) == 4
        first = insts[0]
        assert first["type"] == "CS"
        assert first["de_listed_date"] == "0000-00-00"
        assert first["round_lot"] == 100
        assert first["order_book_id"].endswith((".XSHG", ".XSHE"))
        assert isinstance(first["symbol"], str) and first["symbol"]
        for key in ("order_book_id", "symbol", "listed_date", "de_listed_date", "round_lot"):
            assert key in first
        # 平台示例策略依赖的代码必须存在(默认池首个)
        assert first["order_book_id"] == "000001.XSHE"

    def test_codes_param(self):
        src = SyntheticSource(n_instruments=2, codes=["SYN00001.XSHG", "SYN00002.XSHE"], seed=3)
        assert [i["order_book_id"] for i in src.instruments()] == [
            "SYN00001.XSHG", "SYN00002.XSHE",
        ]


class TestDailyBars:
    def test_columns_and_index(self, source):
        oid = source.instruments()[0]["order_book_id"]
        df = source.daily_raw(oid)
        assert list(df.columns) == list(DAILY_COLUMNS)
        assert df.index.is_monotonic_increasing
        assert df.index.map(lambda d: d == d.normalize()).all()

    def test_price_band_and_positivity(self, source):
        """初始真实价在 10~50 元;价格全程为正。"""
        for inst in source.instruments():
            oid = inst["order_book_id"]
            df = source.daily_raw(oid)
            assert 10.0 <= df["close"].iloc[0] <= 50.0
            assert (df["low"] > 0).all()
            assert (df[["open", "high", "low", "close"]] > 0).all().all()

    def test_ohlc_consistency(self, source):
        for inst in source.instruments():
            oid = inst["order_book_id"]
            df = source.daily_raw(oid)
            assert (df["high"] >= df[["open", "close"]].max(axis=1) - 1e-9).all()
            assert (df["low"] <= df[["open", "close"]].min(axis=1) + 1e-9).all()

    def test_bar_count_in_range(self, source):
        """默认区间约 400 个交易日,落在 300~500 量级。"""
        for inst in source.instruments():
            oid = inst["order_book_id"]
            n = len(source.daily_raw(oid))
            assert 300 <= n <= 500, f"{oid} 日线数 {n} 不在 300~500"

    def test_suspended_days_removed(self, source):
        """停牌日从交易日历中抽掉:不在 daily_raw 索引中,且有记录。"""
        for inst in source.instruments():
            oid = inst["order_book_id"]
            df = source.daily_raw(oid)
            susp = source.suspended_days(oid)
            assert susp, "每合约应有少量停牌日"
            for d in susp:
                ts = pd.Timestamp(str(d))
                assert ts not in df.index
        # 停牌日数量少(2~5 天)
        assert all(len(source.suspended_days(i["order_book_id"])) <= 5 for i in source.instruments())

    def test_daily_volatility_about_25pct_annual(self, source):
        """对数收益年化波动约 25%(用后复权价,剔除事件跳空;允许抽样误差 ±8pp)。"""
        oid = source.instruments()[0]["order_book_id"]
        raw = source.daily_raw(oid)
        hfq = raw["close"] * source.adjust_factor(oid)
        rets = np.log(hfq / hfq.shift(1)).dropna()
        annual = float(rets.std() * np.sqrt(244))
        assert 0.17 <= annual <= 0.33, f"年化波动 {annual:.3f} 偏离 25% 过多"


class TestCorporateActions:
    def test_dividend_and_split_present(self, source):
        """至少 1 个合约注入 1 次现金分红 + 1 次拆股(合约 0)。"""
        oid = source.instruments()[0]["order_book_id"]
        div = source.dividends(oid)
        spl = source.splits(oid)
        assert len(div) == 1 and len(spl) == 1
        row = div.iloc[0]
        assert row["dividend_cash_before_tax"] > 0
        assert row["round_lot"] == 100
        assert spl.iloc[0]["split_factor"] == 2.0
        # 其余合约无事件
        for inst in source.instruments()[1:]:
            oid2 = inst["order_book_id"]
            assert source.dividends(oid2).empty
            assert source.splits(oid2).empty

    def test_raw_price_drops_on_events(self, source):
        """事件日真实价体现除权除息:除息日回落 ~d_ps,拆股日减半。"""
        oid = source.instruments()[0]["order_book_id"]
        df = source.daily_raw(oid)
        spl = source.splits(oid).iloc[0]
        ex14 = int(spl["ex_date"])
        ex_ts = pd.Timestamp(
            f"{ex14 // 10_000_000_000:04d}-{ex14 // 100_000_000 % 100:02d}-{ex14 // 1_000_000 % 100:02d}"
        )
        pos = df.index.get_loc(ex_ts)
        ratio = df["close"].iloc[pos] / df["close"].iloc[pos - 1]
        # 拆股日收益≈-50%(叠加正常日波动)
        assert -0.55 < ratio - 1 < -0.45


class TestAdjustFactor:
    def test_factor_monotone_non_decreasing(self, source):
        """factor 单调不减;无事件合约恒为 1。"""
        for inst in source.instruments():
            oid = inst["order_book_id"]
            f = source.adjust_factor(oid)
            assert (f > 0).all()
            assert (np.diff(f.to_numpy()) >= 0).all()
        oid = source.instruments()[0]["order_book_id"]
        f0 = source.adjust_factor(oid)
        assert f0.iloc[0] == 1.0  # 首值(上市起)为 1
        assert f0.iloc[-1] > 1.0  # 分红+拆股后因子 > 1

    def test_hfq_identity(self, source):
        """hfq = raw × factor 恒等式。"""
        oid = source.instruments()[0]["order_book_id"]
        raw = source.daily_raw(oid)
        f = source.adjust_factor(oid)
        hfq = raw["close"] * f.reindex(raw.index)
        # 因子跳变处 hfq 连续性由下一测试覆盖;这里验证乘法关系本身
        assert np.allclose(hfq.to_numpy(), raw["close"].to_numpy() * f.to_numpy())

    def test_hfq_no_jump_at_events(self, source):
        """复权后价格序列无跳空:hfq 相邻日收益在事件日不突变。

        对照:raw 在拆股日收益 ≈ -50%,hfq 收益应与普通交易日同量级。
        """
        oid = source.instruments()[0]["order_book_id"]
        raw = source.daily_raw(oid)
        factor = source.adjust_factor(oid)
        ex_cum = ex_cum_factor_from(raw, factor)

        # 分段因子展开到逐日
        starts = ex_cum["start_date"]
        day_int14 = pd.Series(
            [int(d.year * 10_000_000_000 + d.month * 100_000_000 + d.day * 1_000_000) for d in raw.index]
        )
        pos = np.searchsorted(starts, day_int14.to_numpy(), side="right") - 1
        daily_factor = ex_cum["ex_cum_factor"][pos]
        assert np.allclose(daily_factor, factor.to_numpy())

        hfq = raw["close"].to_numpy() * daily_factor
        hfq_rets = np.diff(hfq) / hfq[:-1]
        raw_rets = np.diff(raw["close"].to_numpy()) / raw["close"].to_numpy()[:-1]

        def ret_at(rets: np.ndarray, day_pos: int) -> float:
            # np.diff 偏移 1:进入第 day_pos 日的收益 = rets[day_pos - 1]
            return float(rets[day_pos - 1])

        # 事件日的位置(拆股日)
        spl = source.splits(oid).iloc[0]
        ex14 = int(spl["ex_date"])
        event_pos = int(np.nonzero(day_int14.to_numpy() == ex14)[0][0])
        # raw 突变(约 -50%),hfq 无跳空(|ret| < 5%,正常日波动约 1.6%)
        assert ret_at(raw_rets, event_pos) < -0.45
        assert abs(ret_at(hfq_rets, event_pos)) < 0.05
        # 除息日同样:raw 回落,hfq 平滑(数学上必有 raw_ret < hfq_ret)
        div = source.dividends(oid).iloc[0]
        div_pos = int(np.nonzero(day_int14.to_numpy() == int(div["ex_dividend_date"]) * 1_000_000)[0][0])
        assert ret_at(raw_rets, div_pos) < ret_at(hfq_rets, div_pos)
        assert abs(ret_at(hfq_rets, div_pos)) < 0.05
        # 全序列:hfq 收益不出现超过 ±10% 的跳空
        assert np.abs(hfq_rets).max() < 0.10

    def test_ex_cum_factor_records(self, source):
        """ex_cum_factor_from:首记录=数据首日+因子首值;变化点=事件日。"""
        oid = source.instruments()[0]["order_book_id"]
        raw = source.daily_raw(oid)
        factor = source.adjust_factor(oid)
        ex_cum = ex_cum_factor_from(raw, factor)
        assert ex_cum.dtype == np.dtype([("start_date", np.int64), ("ex_cum_factor", np.float64)])
        assert ex_cum["start_date"][0] == (
            raw.index[0].year * 10_000_000_000
            + raw.index[0].month * 100_000_000
            + raw.index[0].day * 1_000_000
        )
        assert ex_cum["ex_cum_factor"][0] == pytest.approx(factor.iloc[0])
        # 3 条记录:首日 + 除息日 + 拆股日
        assert len(ex_cum) == 3
        div_day = int(source.dividends(oid).iloc[0]["ex_dividend_date"]) * 1_000_000
        spl_day = int(source.splits(oid).iloc[0]["ex_date"])
        assert set(ex_cum["start_date"].tolist()) == {
            ex_cum["start_date"][0].item(), div_day, spl_day,
        }
        # 最后一条因子 = 序列末值(拆股后)
        assert ex_cum["ex_cum_factor"][-1] == pytest.approx(factor.iloc[-1])

    def test_st_window_on_last_instrument(self, source):
        """最后一个合约有 ST 窗口,其余为空。"""
        insts = source.instruments()
        assert source.st_days(insts[-1]["order_book_id"]), "末位合约应有 ST 窗口"
        for inst in insts[:-1]:
            assert source.st_days(inst["order_book_id"]) == []
