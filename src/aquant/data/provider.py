"""Layer 2:数据源协议(可插拔)。

统一口径的约定(见 docs/design.md ADR-1):
- 所有价格字段(open/high/low/close)一律**真实价(不复权)**;
- 复权信息单独通过 adjust_factor 提供(累计 hfq 因子,按日期索引,
  真实价 × factor = 后复权价;volume ÷ factor = 复权量);
- 分红/拆股/停牌/ST 以结构化明细提供,bundle 构建时落到 RQAlpha 对应文件,
  由回测引擎的账户/撮合模块在运行时处理,避免任何"双重复权"。

实现方:
- SyntheticSource:确定性合成数据(单元测试/演示,无网络依赖)
- AkshareSource:AKShare 真实数据(可选依赖,自带缓存)

任何新数据源(如 tushare/自建数仓)实现本协议即可接入平台。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import pandas as pd

# daily_raw() 必须返回的列(真实价)
DAILY_COLUMNS = ("open", "high", "low", "close", "volume", "total_turnover")


@runtime_checkable
class BarSource(Protocol):
    """A 股日线数据源协议。所有日期均为 pandas.Timestamp 或 'YYYY-MM-DD' 字符串。"""

    def instruments(self) -> list[dict]:
        """全部合约元数据。

        每项至少包含:order_book_id(如 '000001.XSHE')、symbol(名称)、
        type('CS')、listed_date、de_listed_date('YYYY-MM-DD' 或 '0000-00-00' 表示未退市)、
        round_lot(一手股数,股票为 100)。
        """
        ...

    def daily_raw(self, oid: str) -> pd.DataFrame:
        """该合约全部日线(真实价,未复权), DatetimeIndex 升序,列同 DAILY_COLUMNS。"""
        ...

    def adjust_factor(self, oid: str) -> pd.Series:
        """累计复权因子序列(hfq 口径), DatetimeIndex 升序, float64。

        约定:hfq_price = raw_price * factor;factor 从上市日起累计,
        无除权除息事件期间保持不变。
        """
        ...

    def dividends(self, oid: str) -> pd.DataFrame:
        """现金分红明细,列:
        book_closure_date / announcement_date / dividend_cash_before_tax / ex_dividend_date /
        payable_date(YYYYMMDD int) / round_lot(每 round_lot 股派 dividend_cash_before_tax 元)。
        """
        ...

    def splits(self, oid: str) -> pd.DataFrame:
        """拆股明细,列:ex_date(YYYYMMDDHHMMSS int)、split_factor(每股拆为 factor 股)。"""
        ...

    def suspended_days(self, oid: str) -> list[int]:
        """停牌交易日集合(YYYYMMDD int)。"""
        ...

    def st_days(self, oid: str) -> list[int]:
        """ST 状态交易日集合(YYYYMMDD int),非 ST 返回空。"""
        ...
