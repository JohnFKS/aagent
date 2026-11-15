"""板块涨跌停价规则测试:各板块规则、ST 变体、首日 None、分位四舍五入。"""

from __future__ import annotations

from datetime import date

import pytest

from aquant.data.limits import (
    PRICE_LIMIT_RATIO_CHANGE_DATE,
    board_of,
    limit_ratio,
    price_limits,
    round_to_cent,
)


class TestRoundToCent:
    """四舍五入到分:Decimal ROUND_HALF_UP,而非银行家舍入。"""

    def test_half_up_rounding(self):
        # 0.005 必须进位到 0.01(Python 内置 round 会得到 0.0)
        assert round_to_cent(10.025) == 10.03
        assert round_to_cent(11.033) == 11.03

    def test_prev_close_1003_limit_up(self):
        """规范用例:prev_close=10.03,主板 10% 涨停 = 11.03(10.033 → 11.03)。"""
        up, down = price_limits("600519.XSHG", 10.03, date=date(2023, 6, 1))
        assert up == 11.03
        # 跌停 10.03 * 0.9 = 9.027 → 9.03
        assert down == 9.03

    def test_exact_half_cent_goes_up(self):
        """x.xx5 情形:1.115 → 1.12(ROUND_HALF_UP),而 round() 会给 1.11(浮点)。"""
        assert round_to_cent(1.115) == 1.12


class TestBoardOf:
    def test_main_boards(self):
        assert board_of("600519.XSHG") == "main"
        assert board_of("000001.XSHE") == "main"
        assert board_of("002415.XSHE") == "main"
        assert board_of("003816.XSHE") == "main"
        assert board_of("001289.XSHE") == "main"

    def test_gem_star_bj(self):
        assert board_of("300750.XSHE") == "gem"
        assert board_of("301001.XSHE") == "gem"
        assert board_of("688981.XSHG") == "star"
        assert board_of("430047.BJ") == "bj"
        assert board_of("832566.BJ") == "bj"
        assert board_of("920002.BJ") == "bj"
        assert board_of("830799.XSHG") == "bj"  # 8 开头按北交所处理


class TestLimitRatio:
    def test_main_board_10pct(self):
        assert limit_ratio("600519.XSHG", date=date(2023, 6, 1)) == 0.10
        assert limit_ratio("000001.XSHE", date=date(2023, 6, 1)) == 0.10

    def test_gem_before_and_after_20200824(self):
        assert limit_ratio("300750.XSHE", date=date(2020, 8, 21)) == 0.10  # 改革前
        assert limit_ratio("300750.XSHE", date=date(2020, 8, 24)) == 0.20  # 改革当日
        assert limit_ratio("301001.XSHE", date=date(2023, 6, 1)) == 0.20

    def test_star_20pct(self):
        assert limit_ratio("688981.XSHG", date=date(2023, 6, 1)) == 0.20

    def test_bj_30pct(self):
        assert limit_ratio("430047.BJ", date=date(2023, 6, 1)) == 0.30
        assert limit_ratio("832566.BJ", date=date(2023, 6, 1)) == 0.30

    def test_st_variants(self):
        """ST:主板 ±5%;创业板/科创板 ST 仍 ±20%;北交所 ST 仍 ±30%。"""
        assert limit_ratio("600519.XSHG", date=date(2023, 6, 1), is_st=True) == 0.05
        assert limit_ratio("000001.XSHE", date=date(2023, 6, 1), is_st=True) == 0.05
        assert limit_ratio("300750.XSHE", date=date(2023, 6, 1), is_st=True) == 0.20
        assert limit_ratio("688981.XSHG", date=date(2023, 6, 1), is_st=True) == 0.20
        assert limit_ratio("430047.BJ", date=date(2023, 6, 1), is_st=True) == 0.30


class TestPriceLimits:
    """每板块至少 2 例 + ST 变体。"""

    def test_main_board(self):
        up, down = price_limits("600519.XSHG", 100.00, date=date(2023, 6, 1))
        assert (up, down) == (110.00, 90.00)
        up, down = price_limits("000001.XSHE", 12.34, date=date(2023, 6, 1))
        assert (up, down) == (13.57, 11.11)  # 12.34*1.1=13.574→13.57; 12.34*0.9=11.106→11.11

    def test_gem_after_reform(self):
        up, down = price_limits("300750.XSHE", 50.00, date=date(2023, 6, 1))
        assert (up, down) == (60.00, 40.00)
        up, down = price_limits("301001.XSHE", 33.33, date=date(2023, 6, 1))
        assert (up, down) == (40.00, 26.66)  # 39.996→40.00; 26.664→26.66

    def test_gem_before_reform(self):
        up, down = price_limits("300750.XSHE", 50.00, date=date(2020, 1, 2))
        assert (up, down) == (55.00, 45.00)

    def test_star_board(self):
        up, down = price_limits("688981.XSHG", 31.28, date=date(2023, 6, 1))
        assert (up, down) == (37.54, 25.02)  # 37.536→37.54; 25.024→25.02

    def test_bj_board(self):
        up, down = price_limits("430047.BJ", 10.00, date=date(2023, 6, 1))
        assert (up, down) == (13.00, 7.00)
        up, down = price_limits("920002.BJ", 25.55, date=date(2023, 6, 1))
        # 25.55*1.3=33.215 → HALF_UP → 33.22;25.55*0.7=17.885 → 17.89
        assert (up, down) == (33.22, 17.89)

    def test_st_variants(self):
        up, down = price_limits("600519.XSHG", 10.03, date=date(2023, 6, 1), is_st=True)
        assert (up, down) == (10.53, 9.53)  # 10.5315→10.53; 9.5285→9.53
        up, down = price_limits("300750.XSHE", 50.00, date=date(2023, 6, 1), is_st=True)
        assert (up, down) == (60.00, 40.00)  # 创业板 ST 不减半
        up, down = price_limits("688981.XSHG", 20.00, date=date(2023, 6, 1), is_st=True)
        assert (up, down) == (24.00, 16.00)
        up, down = price_limits("430047.BJ", 10.00, date=date(2023, 6, 1), is_st=True)
        assert (up, down) == (13.00, 7.00)

    def test_invalid_prev_close(self):
        with pytest.raises(ValueError):
            price_limits("600519.XSHG", 0.0, date=date(2023, 6, 1))
        with pytest.raises(ValueError):
            price_limits("600519.XSHG", -5.0, date=date(2023, 6, 1))


class TestNoLimitWindow:
    """上市首日及创业板/科创板前 5 个交易日无涨跌停 → None。"""

    LISTED = date(2023, 6, 5)
    # 2023-06-05(周一) 起的连续交易日
    CALENDAR = [date(2023, 6, 1), date(2023, 6, 2), date(2023, 6, 5), date(2023, 6, 6),
                date(2023, 6, 7), date(2023, 6, 8), date(2023, 6, 9), date(2023, 6, 12)]

    def test_first_day_main_board(self):
        assert price_limits("600519.XSHG", 10.0, date=self.LISTED,
                            listed_date=self.LISTED, trading_dates=self.CALENDAR) is None

    def test_gem_first_5_trading_days(self):
        args = dict(listed_date=self.LISTED, trading_dates=self.CALENDAR)
        # 首日 + 第 2~5 个交易日(0-based 0..4)均无涨跌停
        for d in (date(2023, 6, 5), date(2023, 6, 6), date(2023, 6, 7),
                  date(2023, 6, 8), date(2023, 6, 9)):
            assert price_limits("301001.XSHE", 20.0, date=d, **args) is None, d
        # 第 6 个交易日恢复正常 ±20%
        up, down = price_limits("301001.XSHE", 20.0, date=date(2023, 6, 12), **args)
        assert (up, down) == (24.00, 16.00)

    def test_star_first_5_trading_days(self):
        args = dict(listed_date=self.LISTED, trading_dates=self.CALENDAR)
        assert price_limits("688981.XSHG", 31.0, date=date(2023, 6, 8), **args) is None
        up, _ = price_limits("688981.XSHG", 31.0, date=date(2023, 6, 12), **args)
        assert up == 37.20

    def test_main_board_only_first_day(self):
        """主板只有首日无涨跌停,第 2 个交易日即 ±10%。"""
        args = dict(listed_date=self.LISTED, trading_dates=self.CALENDAR)
        assert price_limits("600519.XSHG", 10.0, date=self.LISTED, **args) is None
        up, _ = price_limits("600519.XSHG", 10.0, date=date(2023, 6, 6), **args)
        assert up == 11.00  # 第 2 个交易日恢复正常

    def test_without_calendar_only_exact_first_day(self):
        """未提供交易日历时,只能识别 date == listed_date 的首日。"""
        assert price_limits("600519.XSHG", 10.0, date=self.LISTED, listed_date=self.LISTED) is None
        up, _ = price_limits("600519.XSHG", 10.0, date=date(2023, 6, 6), listed_date=self.LISTED)
        assert up == 11.00
