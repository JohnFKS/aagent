"""Layer 2:确定性合成数据源(测试/演示,无网络依赖)。

设计要点:
- 同一 seed 生成完全相同的数据(逐值相等),用于回测正确性的可复现验证;
- 几何随机游走(年化波动约 25%,带温和均值回归使价格保持在合理区间),
  初始真实价(不复权)在 10~50 元之间;
- 合约 0 注入 1 次现金分红 + 1 次拆股:adjust_factor 出现非 1 因子且单调不减;
  hfq = raw × factor 在事件日无跳空(见 tests/data/test_synthetic.py);
- 每个合约从交易日历中抽掉少量日子作为停牌日(daily_raw 不含这些日期);
- 最后一个合约(当 n_instruments ≥ 2)注入一段 ST 窗口,用于 st_stock_days 与
  主板 ST ±5% / 创业板 ST ±20% 规则的回测链路覆盖;
- 日线数量由 [start, end] 的交易日(周一~周五,不含法定节假日)决定:
  默认区间(2023-06-01 ~ 2024-12-31)约 400 个交易日,满足"每合约 300~500 日线"
  的量级要求;显式传入更长区间时按区间长度生成。

代码池默认以真实格式编排(首个为 '000001.XSHE',与平台示例策略 demo_ma_cross 对齐),
也可通过 ``codes`` 参数显式指定(如 ['SYN00001.XSHG', ...])。所有数据均为合成数据,
symbol 以"合成"字样标识,严禁与真实行情混淆或用于实盘。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from aquant.data.provider import DAILY_COLUMNS

__all__ = ["SyntheticSource", "DEFAULT_CODES"]

# 默认代码池:覆盖主板/创业板/科创板,首个与平台示例策略(demo_ma_cross)对齐
DEFAULT_CODES: tuple[str, ...] = (
    "000001.XSHE",  # 主板(深)
    "600519.XSHG",  # 主板(沪)
    "300750.XSHE",  # 创业板
    "688981.XSHG",  # 科创板
    "601318.XSHG",  # 主板(沪)
    "000002.XSHE",  # 主板(深)
    "002415.XSHE",  # 主板(深)
    "301001.XSHE",  # 创业板
    "600036.XSHG",  # 主板(沪)
    "000651.XSHE",  # 主板(深)
)

_DEFAULT_START = "2023-06-01"
_DEFAULT_END = "2024-12-31"

# 年化波动 25% → 日波动
_ANNUAL_VOL = 0.25
_TRADING_DAYS_A_YEAR = 244.0
# 均值回归强度(年化),温和地将价格拉回锚点附近
_ANNUAL_KAPPA = 0.5


def _board_type_of(code: str) -> str:
    """rqalpha Instrument.board_type:MainBoard / GEM / KSH / BJS。"""
    if code.startswith(("300", "301")):
        return "GEM"
    if code.startswith("688"):
        return "KSH"
    if code.upper().endswith(".BJ"):
        return "BJS"
    return "MainBoard"


def _symbol_of(i: int) -> str:
    return f"合成测试{i + 1:02d}"


class SyntheticSource:
    """确定性合成日线数据源,实现 BarSource 协议。

    :param n_instruments: 合约数量(默认取 DEFAULT_CODES 前 n 个,不足时以 SYN 前缀续编)
    :param start/end: 'YYYY-MM-DD';默认区间约 400 个交易日
    :param seed: 随机种子(同 seed ⇒ 数据逐值相等)
    :param codes: 显式指定 order_book_id 列表(可选),长度不足 n_instruments 时报错
    """

    def __init__(
        self,
        n_instruments: int = 5,
        start: str | None = None,
        end: str | None = None,
        seed: int = 42,
        codes: list[str] | None = None,
    ) -> None:
        if n_instruments < 1:
            raise ValueError("n_instruments 至少为 1")
        self._seed = int(seed)
        self._start = pd.Timestamp(start or _DEFAULT_START)
        self._end = pd.Timestamp(end or _DEFAULT_END)
        if self._start >= self._end:
            raise ValueError(f"start({start}) 必须早于 end({end})")

        if codes is not None:
            if len(codes) < n_instruments:
                raise ValueError(f"codes 长度({len(codes)})少于 n_instruments({n_instruments})")
            oid_list = [str(c) for c in codes[:n_instruments]]
        else:
            pool = list(DEFAULT_CODES)
            oid_list = pool[:n_instruments]
            i = len(oid_list)
            while len(oid_list) < n_instruments:
                oid_list.append(f"SYN{i + 1:06d}.XSHE")
                i += 1
        self._oids = oid_list

        # 全市场交易日历:周一~周五(合成数据不含法定节假日,见模块 docstring)
        self._calendar = pd.bdate_range(self._start, self._end, normalize=True)
        self._instruments: dict[str, dict] = {}
        self._daily: dict[str, pd.DataFrame] = {}
        self._factor: dict[str, pd.Series] = {}
        self._dividends: dict[str, pd.DataFrame] = {}
        self._splits: dict[str, pd.DataFrame] = {}
        self._suspended: dict[str, list[int]] = {}
        self._st: dict[str, list[int]] = {}
        for i, oid in enumerate(self._oids):
            self._generate_instrument(i, oid)

    # ------------------------------------------------------------------
    # 对外接口(BarSource 协议)
    # ------------------------------------------------------------------
    @property
    def seed(self) -> int:
        return self._seed

    @property
    def trading_calendar(self) -> pd.DatetimeIndex:
        """全市场交易日历(周一~周五),供 bundle 构建/测试使用。"""
        return self._calendar

    def instruments(self) -> list[dict]:
        """全部合约元数据(标准 dict,type='CS',round_lot=100)。"""
        return [dict(self._instruments[oid]) for oid in self._oids]

    def daily_raw(self, oid: str) -> pd.DataFrame:
        """该合约全部日线(真实价,未复权;不含停牌日),列同 DAILY_COLUMNS。"""
        return self._daily[oid].copy()

    def adjust_factor(self, oid: str) -> pd.Series:
        """累计 hfq 因子(hfq = raw × factor),单调不减,事件日跳变。"""
        return self._factor[oid].copy()

    def dividends(self, oid: str) -> pd.DataFrame:
        """现金分红明细(日期为 YYYYMMDD int;每 round_lot 股派 dividend_cash_before_tax 元)。"""
        return self._dividends[oid].copy()

    def splits(self, oid: str) -> pd.DataFrame:
        """拆股明细:ex_date(YYYYMMDDHHMMSS int)+ split_factor(每股拆为 factor 股)。"""
        return self._splits[oid].copy()

    def suspended_days(self, oid: str) -> list[int]:
        """停牌交易日集合(YYYYMMDD int)。"""
        return list(self._suspended[oid])

    def st_days(self, oid: str) -> list[int]:
        """ST 状态交易日集合(YYYYMMDD int),非 ST 返回空。"""
        return list(self._st[oid])

    # ------------------------------------------------------------------
    # 生成逻辑(全部确定性)
    # ------------------------------------------------------------------
    def _generate_instrument(self, idx: int, oid: str) -> None:
        rng = np.random.default_rng([self._seed, idx])
        cal = self._calendar
        n = len(cal)

        # 1) 停牌日:抽掉少量日子(避开首末日),保持市场交易日历不变
        n_susp = int(rng.integers(2, 6))
        inner = np.arange(1, n - 1)
        susp_pos = np.sort(rng.choice(inner, size=n_susp, replace=False))
        susp_days = cal[susp_pos]
        susp_ints = sorted({self._to_date_int8(d) for d in susp_days})
        traded_mask = np.ones(n, dtype=bool)
        traded_mask[susp_pos] = False
        traded = cal[traded_mask]
        t = len(traded)

        # 2) 事件日(仅合约 0 注入分红+拆股;从已交易日中选取,避开首尾)
        div_pos = split_pos = None
        div_cash = split_factor = 0.0
        if idx == 0 and t >= 80:
            div_pos = min(max(30, int(t * 0.4)), t - 40)
            split_pos = min(max(div_pos + 20, int(t * 0.7)), t - 10)

        # 3) 价格路径:几何随机游走 + 温和均值回归(年化波动约 25%)
        sigma = _ANNUAL_VOL / np.sqrt(_TRADING_DAYS_A_YEAR)
        mu = 0.05 / _TRADING_DAYS_A_YEAR
        kappa = _ANNUAL_KAPPA / _TRADING_DAYS_A_YEAR
        anchor = float(np.log(rng.uniform(12.0, 42.0)))

        close = np.empty(t, dtype=np.float64)
        close[0] = float(np.exp(anchor + rng.normal(0.0, sigma)))
        rets = rng.normal(mu, sigma, size=t)
        for k in range(1, t):
            r = rets[k] + kappa * (anchor - np.log(close[k - 1]))
            close[k] = close[k - 1] * (1.0 + r)
            if k == div_pos:
                # 现金分红:除息日价格回落 d_ps(保留分位),作为除权除息事件
                d_ps = max(0.01, round(float(close[k - 1]) * 0.004, 2))
                div_cash = d_ps  # 每股现金(不复权口径)
                close[k] = close[k - 1] * (1.0 + r) - d_ps
            if k == split_pos:
                # 拆股:每股拆为 2 股,价格减半(乘以正常日收益)
                split_factor = 2.0
                close[k] = close[k - 1] * (1.0 + r) / split_factor
            if close[k] <= 0.5:  # 极端保护(均值回归下几乎不可能触发)
                close[k] = max(close[k - 1] * 0.9, 0.6)

        # 4) OHLC 与成交
        open_ = np.empty(t)
        open_[0] = close[0] * (1.0 + rng.normal(0.0, 0.004))
        open_[1:] = close[:-1] * (1.0 + rng.normal(0.0, 0.004, size=t - 1))
        spread_hi = np.abs(rng.normal(0.0, 0.004, size=t))
        spread_lo = np.abs(rng.normal(0.0, 0.004, size=t))
        high = np.maximum(open_, close) * (1.0 + spread_hi)
        low = np.minimum(open_, close) * (1.0 - spread_lo)
        volume = rng.uniform(1.5e6, 3.0e6, size=t)
        turnover = close * volume

        df = pd.DataFrame(
            {
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "total_turnover": turnover,
            },
            index=pd.DatetimeIndex(traded, name="date"),
        )[list(DAILY_COLUMNS)]
        self._daily[oid] = df

        # 5) 复权因子:hfq = raw × factor;分红日用 prev/(prev-d),拆股日 ×split_factor
        factor = np.ones(t, dtype=np.float64)
        for k in range(1, t):
            factor[k] = factor[k - 1]
            if k == div_pos:
                prev = float(close[k - 1])
                factor[k] = factor[k - 1] * prev / (prev - div_cash)
            if k == split_pos:
                factor[k] = factor[k - 1] * split_factor
        self._factor[oid] = pd.Series(factor, index=df.index, name="adjust_factor")

        # 6) 分红明细(除息日 D:股权登记日=D 前一交易日,发放日=D 后第 3 个交易日)
        if div_pos is not None:
            d8 = self._to_date_int8(traded[div_pos])
            book_closure = self._to_date_int8(traded[div_pos - 1])
            payable = self._to_date_int8(traded[min(div_pos + 3, t - 1)])
            ann_pos = max(0, div_pos - 10)
            announcement = self._to_date_int8(traded[ann_pos])
            self._dividends[oid] = pd.DataFrame(
                [{
                    "book_closure_date": book_closure,
                    "announcement_date": announcement,
                    "dividend_cash_before_tax": round(div_cash * 100.0, 2),  # 每 100 股
                    "ex_dividend_date": d8,
                    "payable_date": payable,
                    "round_lot": 100,
                }]
            )
        else:
            self._dividends[oid] = pd.DataFrame(
                columns=["book_closure_date", "announcement_date",
                         "dividend_cash_before_tax", "ex_dividend_date",
                         "payable_date", "round_lot"]
            )

        # 7) 拆股明细(ex_date 为 14 位 YYYYMMDDHHMMSS)
        if split_pos is not None:
            self._splits[oid] = pd.DataFrame(
                [{
                    "ex_date": self._to_date_int14(traded[split_pos]),
                    "split_factor": float(split_factor),
                }]
            )
        else:
            self._splits[oid] = pd.DataFrame(columns=["ex_date", "split_factor"])

        # 8) ST 窗口(最后一个合约,n≥2 时注入):用于 ST 限价规则与 st_stock_days 链路
        st_ints: list[int] = []
        if idx == len(self._oids) - 1 and len(self._oids) >= 2:
            start_st = max(20, int(t * 0.55))
            end_st = min(t - 5, start_st + 15)
            st_ints = sorted({self._to_date_int8(d) for d in traded[start_st:end_st]})
        self._suspended[oid] = susp_ints
        self._st[oid] = st_ints

        # 9) 合约元数据
        code = oid.split(".")[0]
        suffix = oid.split(".")[-1].upper() if "." in oid else "XSHE"
        self._instruments[oid] = {
            "order_book_id": oid,
            "symbol": _symbol_of(idx),
            "type": "CS",
            "listed_date": self._start.strftime("%Y-%m-%d"),
            "de_listed_date": "0000-00-00",
            "round_lot": 100,
            "exchange": suffix,
            "board_type": _board_type_of(code),
            "market_tplus": 1,
            "status": "Active",
            "special_type": "ST" if st_ints else "Normal",
            "industry_code": "S90",
            "industry_name": "综合(合成数据)",
        }

    @staticmethod
    def _to_date_int8(ts: pd.Timestamp) -> int:
        """Timestamp → 8 位 int(YYYYMMDD)。"""
        return int(ts.year * 10000 + ts.month * 100 + ts.day)

    @staticmethod
    def _to_date_int14(ts: pd.Timestamp) -> int:
        """Timestamp → 14 位 int(YYYYMMDD000000),与 rqalpha bars['datetime'] 同格式。"""
        return int(ts.year * 10_000_000_000 + ts.month * 100_000_000 + ts.day * 1_000_000)
