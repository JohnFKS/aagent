"""Layer 2:AKShare 真实数据源(可选依赖,仅供研究)。

**重要声明:本数据源基于 AKShare 公开接口,数据口径与质量以 AKShare 为准,
仅供研究用途,严禁用于任何实盘决策。**

实现 BarSource 协议,口径与 SyntheticSource 一致(docs/design.md ADR-1):
- ``daily_raw``:真实价(不复权),``ak.stock_zh_a_hist(..., adjust='')``;
- ``adjust_factor``:累计 hfq 因子 = hfq_close / raw_close(两次请求按 date 对齐),
  hfq = raw × factor;
- ``dividends``:best-effort 读取 ``ak.stock_fh_dividend_*`` 族接口 —— AKShare 版本
  间接口名/列名差异较大,任何失败都降级为**空表 + warnings.warn**,不影响主链路
  (hfq 因子已含分红/拆股的复权信息,dividends 只是让现金分红进账户现金流);
- ``suspended_days``:相对交易日历(sina 日历)的缺失日;
- ``splits``:AKShare 无稳定拆股接口 → 恒为空表(拆股信息已含在 hfq 因子中);
- ``st_days``:无稳定公开接口 → 恒为空(平台 ST 规则对真实数据暂不生效)。

网络细节:每次请求前限速 0.5~1s 随机间隔,失败自动重试(默认 3 次);
akshare 为可选依赖,ImportError 时提示 ``pip install 'aquant[akshare]'``。

代码映射:6 开头 → ``.XSHG``,0/3 开头 → ``.XSHE``,4/8/92 开头 → ``.BJ``
(stock_zh_a_hist 对北交所支持有限,拿不到数据会显式报错)。
"""

from __future__ import annotations

import random
import time
import warnings

import numpy as np
import pandas as pd

from aquant.data.provider import DAILY_COLUMNS

__all__ = ["AkshareSource"]

_AKSHARE_INSTALL_HINT = (
    "AKShare 未安装。它是 aquant 的可选依赖,请执行:pip install 'aquant[akshare]'"
)

# 每次请求前的限速区间(秒)
_DEFAULT_SLEEP_RANGE = (0.5, 1.0)
_DEFAULT_MAX_RETRIES = 3


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


class AkshareSource:
    """AKShare 真实日线数据源,实现 BarSource 协议(仅供研究)。

    :param codes: 六位代码列表,如 ``['000001', '600519']``;也可带后缀('000001.XSHE')
    :param start/end: 'YYYY-MM-DD' 数据区间(闭区间)
    :param max_retries: 单个接口的最大重试次数
    :param sleep_range: 每次请求前的限速区间(秒,随机取值)
    """

    def __init__(
        self,
        codes: list[str],
        start: str,
        end: str,
        *,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        sleep_range: tuple[float, float] = _DEFAULT_SLEEP_RANGE,
    ) -> None:
        if not codes:
            raise ValueError("codes 不能为空,如 ['000001', '600519']")
        self._start = pd.Timestamp(start)
        self._end = pd.Timestamp(end)
        if self._start >= self._end:
            raise ValueError(f"start({start}) 必须早于 end({end})")
        self._oids = [self._normalize_code(c) for c in codes]
        if len(set(self._oids)) != len(self._oids):
            raise ValueError(f"codes 存在重复:{self._oids}")
        self._max_retries = int(max_retries)
        self._sleep_range = sleep_range

        self._ak = None  # 惰性导入
        self._calendar: pd.DatetimeIndex | None = None
        self._daily: dict[str, pd.DataFrame] = {}
        self._factor: dict[str, pd.Series] = {}
        self._dividends: dict[str, pd.DataFrame] = {}
        self._loaded: set[str] = set()

    # ------------------------------------------------------------------
    # BarSource 协议
    # ------------------------------------------------------------------
    def instruments(self) -> list[dict]:
        return [self._instrument_of(oid) for oid in self._oids]

    def daily_raw(self, oid: str) -> pd.DataFrame:
        self._ensure_loaded(oid)
        return self._daily[oid].copy()

    def adjust_factor(self, oid: str) -> pd.Series:
        self._ensure_loaded(oid)
        return self._factor[oid].copy()

    def dividends(self, oid: str) -> pd.DataFrame:
        self._ensure_loaded(oid)
        return self._dividends[oid].copy()

    def splits(self, oid: str) -> pd.DataFrame:
        self._ensure_loaded(oid)
        return pd.DataFrame(columns=["ex_date", "split_factor"])

    def suspended_days(self, oid: str) -> list[int]:
        self._ensure_loaded(oid)
        bar_days = {self._int8(d) for d in self._daily[oid].index}
        return [self._int8(d) for d in self._trading_calendar() if self._int8(d) not in bar_days]

    def st_days(self, oid: str) -> list[int]:
        """AKShare 无稳定公开 ST 接口 → 恒为空(见模块 docstring 限制)。"""
        return []

    # ------------------------------------------------------------------
    # 惰性加载与网络层
    # ------------------------------------------------------------------
    @property
    def order_book_ids(self) -> list[str]:
        return list(self._oids)

    def _akshare(self):
        """惰性导入 akshare(可选依赖)。"""
        if self._ak is None:
            try:
                import akshare as ak
            except ImportError as e:
                raise ImportError(_AKSHARE_INSTALL_HINT) from e
            self._ak = ak
        return self._ak

    def _request(self, fn, *args, **kwargs):
        """限速 + 重试的接口调用封装。"""
        last_exc: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            _sleep(random.uniform(*self._sleep_range))
            try:
                return fn(*args, **kwargs)
            except Exception as exc:  # AKShare 接口异常形态多变,统一捕获重试
                last_exc = exc
                warnings.warn(
                    f"AKShare 接口请求失败(第 {attempt}/{self._max_retries} 次):{exc!r}"
                )
        raise RuntimeError(
            f"AKShare 接口连续 {self._max_retries} 次失败:{fn.__name__}"
        ) from last_exc

    def _trading_calendar(self) -> pd.DatetimeIndex:
        """交易日历(sina);仅供研究,与真实交易所日历可能存在差异。"""
        if self._calendar is None:
            ak = self._akshare()
            df = self._request(ak.tool_trade_date_hist_sina)
            dates = pd.to_datetime(df["trade_date"])
            dates = dates[(dates >= self._start) & (dates <= self._end)]
            self._calendar = pd.DatetimeIndex(sorted(dates))
        return self._calendar

    def _ensure_loaded(self, oid: str) -> None:
        if oid in self._loaded:
            return
        if oid not in self._oids:
            raise KeyError(f"未知的合约代码 {oid!r},可用:{self._oids}")
        raw = self._fetch_daily(oid, adjust="")
        if raw.empty:
            raise RuntimeError(
                f"AKShare 未返回 {oid} 在 {self._start:%Y-%m-%d}~{self._end:%Y-%m-%d} "
                "的日线数据(代码可能不存在,或属于 stock_zh_a_hist 不支持的板块)"
            )
        hfq = self._fetch_daily(oid, adjust="hfq")
        self._daily[oid] = raw
        self._factor[oid] = self._factor_from(raw, hfq)
        self._dividends[oid] = self._fetch_dividends(oid)
        self._loaded.add(oid)

    def _fetch_daily(self, oid: str, adjust: str) -> pd.DataFrame:
        """日线获取,双源降级链:东方财富(stock_zh_a_hist)→ 新浪(stock_zh_a_daily)。

        东财 push2 行情接口有 IP 级限流(连续请求可能被临时封禁),
        新浪接口是独立数据商,作为降级源可绕开单点故障
        (多源降级模式来源:PA_Agent 调研结论,见 docs/design.md §1.2)。
        """
        ak = self._akshare()
        try:
            df = self._request(
                ak.stock_zh_a_hist,
                symbol=oid.split(".")[0],
                period="daily",
                start_date=self._start.strftime("%Y%m%d"),
                end_date=self._end.strftime("%Y%m%d"),
                adjust=adjust,
            )
            return self._map_daily(df)
        except RuntimeError as em_exc:
            warnings.warn(
                f"{oid}:东方财富日线获取失败({em_exc}),降级到新浪接口(成交量单位为股)"
            )
        df = self._request(
            ak.stock_zh_a_daily,
            symbol=self._sina_symbol(oid),
            start_date=self._start.strftime("%Y%m%d"),
            end_date=self._end.strftime("%Y%m%d"),
            adjust=adjust,
        )
        return self._map_daily_sina(df)

    @staticmethod
    def _sina_symbol(oid: str) -> str:
        """order_book_id → 新浪代码:000001.XSHE→sz000001;600519.XSHG→sh600519。"""
        code = oid.split(".")[0]
        if oid.endswith(".XSHG") or code.startswith("6"):
            return f"sh{code}"
        return f"sz{code}"

    @staticmethod
    def _map_daily_sina(df: pd.DataFrame) -> pd.DataFrame:
        """新浪英文列 → DAILY_COLUMNS 口径(volume 已是股,无需换算)。"""
        missing = [c for c in ("date", "open", "close", "high", "low", "volume", "amount") if c not in df.columns]
        if missing:
            raise RuntimeError(f"新浪日线缺少列 {missing},实际列:{list(df.columns)}")
        index = pd.DatetimeIndex(pd.to_datetime(df["date"]), name="date")
        out = pd.DataFrame(
            {
                "open": df["open"].to_numpy(dtype=float),
                "high": df["high"].to_numpy(dtype=float),
                "low": df["low"].to_numpy(dtype=float),
                "close": df["close"].to_numpy(dtype=float),
                "volume": df["volume"].to_numpy(dtype=float),
                "total_turnover": df["amount"].to_numpy(dtype=float),
            },
            index=index,
        )
        return out.sort_index()[list(DAILY_COLUMNS)]

    @staticmethod
    def _map_daily(df: pd.DataFrame) -> pd.DataFrame:
        """AKShare 中文列 → DAILY_COLUMNS 口径(真实价;成交量 手→股)。"""
        mapping = {
            "开盘": "open", "收盘": "close", "最高": "high", "最低": "low",
            "成交量": "volume", "成交额": "total_turnover",
        }
        missing = [c for c in ("日期", "开盘", "收盘", "最高", "最低", "成交量", "成交额") if c not in df.columns]
        if missing:
            raise RuntimeError(f"AKShare 日线缺少列 {missing},实际列:{list(df.columns)}")
        # 用 numpy 数组构造(避免 dict-of-Series + index= 触发 pandas 按索引重对齐)
        index = pd.DatetimeIndex(pd.to_datetime(df["日期"]), name="date")
        out = pd.DataFrame(
            {
                "open": df["开盘"].to_numpy(dtype=float),
                "high": df["最高"].to_numpy(dtype=float),
                "low": df["最低"].to_numpy(dtype=float),
                "close": df["收盘"].to_numpy(dtype=float),
                # 成交量单位为"手",换算为股(1 手 = 100 股)
                "volume": df["成交量"].to_numpy(dtype=float) * 100.0,
                "total_turnover": df["成交额"].to_numpy(dtype=float),
            },
            index=index,
        )
        return out.sort_index()[list(DAILY_COLUMNS)]

    def _factor_from(self, raw: pd.DataFrame, hfq: pd.DataFrame) -> pd.Series:
        """累计 hfq 因子 = hfq_close / raw_close(按 raw 日期对齐,hfq 缺口前值填充)。"""
        hfq_close = hfq["close"].reindex(raw.index).ffill()
        factor = (hfq_close / raw["close"]).astype(float)
        factor.name = "adjust_factor"
        if factor.isna().any():
            raise RuntimeError(f"{self._oids}:hfq/raw 收盘价对齐后仍存在缺失,复权因子不可计算")
        return factor

    # ------------------------------------------------------------------
    # 分红(best-effort)
    # ------------------------------------------------------------------
    def _fetch_dividends(self, oid: str) -> pd.DataFrame:
        """尝试 AKShare 分红接口;任何失败降级为空表 + warning(见模块 docstring)。"""
        ak = self._akshare()
        symbol = oid.split(".")[0]
        df: pd.DataFrame | None = None
        for attr, kwargs in (
            ("stock_fh_dividend", {"symbol": symbol}),
            ("stock_fh_dividend_em", {"symbol": symbol}),
            ("stock_fh_dividend_EM", {"symbol": symbol}),
        ):
            fn = getattr(ak, attr, None)
            if fn is None:
                continue
            try:
                result = self._request(fn, **kwargs)
                if isinstance(result, pd.DataFrame) and len(result) > 0:
                    df = result
                    break
            except Exception as exc:
                warnings.warn(f"{oid}:分红接口 {attr} 失败({exc!r}),尝试降级")
        if df is None:
            warnings.warn(
                f"{oid}:AKShare 分红数据不可得(接口缺失或失败),"
                "dividends 返回空表;复权信息已包含在 adjust_factor 中,不影响回测主链路"
            )
            return self._empty_dividends()
        try:
            return self._map_dividends(df)
        except Exception as exc:
            warnings.warn(f"{oid}:分红列映射失败({exc!r}),dividends 返回空表")
            return self._empty_dividends()

    @staticmethod
    def _empty_dividends() -> pd.DataFrame:
        return pd.DataFrame(
            columns=["book_closure_date", "announcement_date", "dividend_cash_before_tax",
                     "ex_dividend_date", "payable_date", "round_lot"]
        )

    def _map_dividends(self, df: pd.DataFrame) -> pd.DataFrame:
        """按中文列名模糊匹配映射分红字段;映射不出关键字段则抛错(上层降级为空表)。"""
        columns = list(df.columns)

        def find(*keys: str) -> str | None:
            for key in keys:
                for col in columns:
                    if key in str(col):
                        return col
            return None

        ex_col = find("除权除息日", "除息日")
        cash_col = find("每股股利", "每股派息", "股息")
        if ex_col is None or cash_col is None:
            raise KeyError(f"无法定位除息日/每股股利列,实际列:{columns}")
        # "10派X" 类列名表示每 10 股股利 → 换算为每股
        per10 = any(("10" in str(c) and "派" in str(c)) for c in columns)
        cash = df[cash_col].astype(float)
        if per10:
            cash = cash / 10.0

        def to_int8(col: str | None) -> pd.Series:
            if col is None:
                return pd.Series([np.nan] * len(df), dtype=float)
            return pd.to_datetime(df[col]).map(
                lambda d: float(d.year * 10000 + d.month * 100 + d.day) if pd.notna(d) else np.nan
            )

        out = pd.DataFrame({
            "book_closure_date": to_int8(find("股权登记日", "登记日")),
            "announcement_date": to_int8(find("公告日期", "公告日")),
            # 协议口径:每 round_lot(100)股派 dividend_cash_before_tax 元
            "dividend_cash_before_tax": (cash * 100.0).round(2),
            "ex_dividend_date": to_int8(ex_col),
            "payable_date": to_int8(find("派息日", "发放日", "到账日")),
            "round_lot": 100,
        })
        out = out.dropna(subset=["ex_dividend_date", "dividend_cash_before_tax"])
        # 辅助日期缺失时以除息日保守回填(bundle 写盘要求 int64 且无 NA;仅供研究口径)
        for col in ("book_closure_date", "announcement_date", "payable_date"):
            out[col] = out[col].fillna(out["ex_dividend_date"]).astype(np.int64)
        out["ex_dividend_date"] = out["ex_dividend_date"].astype(np.int64)
        out = out[out["ex_dividend_date"] >= int(self._start.strftime("%Y%m%d"))]
        return out.reset_index(drop=True)

    # ------------------------------------------------------------------
    # 元数据
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_code(code: str) -> str:
        """六位代码 → 标准 order_book_id(已带后缀则保留;6→XSHG,0/3→XSHE,4/8/92→BJ)。"""
        text = str(code).strip()
        if "." in text:
            symbol, _, suffix = text.partition(".")
            suffix = suffix.upper()
            if suffix in ("XSHG", "XSHE", "BJ"):
                return f"{symbol}.{suffix}"
            raise ValueError(f"无法识别的交易所后缀:{text!r}")
        if not (text.isdigit() and len(text) == 6):
            raise ValueError(f"代码 {text!r} 应为六位数字(如 000001)")
        if text.startswith("6"):
            return f"{text}.XSHG"
        if text.startswith(("0", "3")):
            return f"{text}.XSHE"
        if text.startswith(("4", "8")) or text.startswith("92"):
            return f"{text}.BJ"
        raise ValueError(f"无法识别的代码前缀:{text!r}(6→XSHG,0/3→XSHE,4/8/92→BJ)")

    def _instrument_of(self, oid: str) -> dict:
        """合约元数据。AKShare 免费接口拿不到稳定上市日/名称,以保守占位填充:
        listed_date = 数据起点(保证 <= 首个数据日,复权因子过滤不受影响),
        symbol = 六位代码。仅供研究。
        """
        symbol, _, suffix = oid.partition(".")
        board = "BJS" if suffix == "BJ" else (
            "KSH" if symbol.startswith("688") else
            "GEM" if symbol.startswith(("300", "301")) else "MainBoard"
        )
        return {
            "order_book_id": oid,
            "symbol": symbol,
            "type": "CS",
            "listed_date": self._start.strftime("%Y-%m-%d"),
            "de_listed_date": "0000-00-00",
            "round_lot": 100,
            "exchange": suffix,
            "board_type": board,
            "market_tplus": 1,
            "status": "Active",
            "special_type": "Normal",
            "industry_code": "S90",
            "industry_name": "AKShare(仅供研究)",
        }

    @staticmethod
    def _int8(ts: pd.Timestamp) -> int:
        return int(ts.year * 10000 + ts.month * 100 + ts.day)
