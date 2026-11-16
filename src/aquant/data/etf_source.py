"""Layer 2:ETF 数据源(基于 AKShare 双源自动降级,仅供研究)。

**数据源选型**:AKShare 是当前免费、最全、最稳定的 ETF 数据聚合库,底层双源:
- 主源:东方财富 ``fund_etf_hist_em`` —— 覆盖全、支持前/后复权、字段规整,
  但 push2 行情接口有 IP 级限流(连续请求临时封禁);
- 降级源:新浪 ``fund_etf_hist_sina`` —— 独立数据商、不限流、历史可回溯至 ETF
  上市日,但不提供复权参数(以真实价为准,复权因子置 1)。

``_fetch_daily`` 自动在主源连续失败后切换到新浪,与股票源 AkshareSource 的
"东财→新浪"降级模式一致(多源降级来源:PA_Agent 调研,见 docs/design.md §1.2)。

标的池:``discover_etf_codes`` 优先东财实时列表 ``fund_etf_spot_em``,
失败降级新浪 ``fund_etf_category_sina('ETF基金')``(实测 927 只),可按成交额
取 Top-N 保证流动性。

实现 BarSource 协议,口径与 AkshareSource 一致(ADR-1):
- ``daily_raw``:真实价(不复权),列 = DAILY_COLUMNS,DatetimeIndex(name='date');
- ``adjust_factor``:累计 hfq 因子 = hfq_close / raw_close(降级源为常数 1);
- 合约 ``type='ETF'`` 且带 ``fund_type`` —— RQAlpha 6.5.0 要求 ETF 写入 funds.h5。

代码映射:5/6 开头 → XSHG(51/56/58 沪市 ETF),1/0/3 开头 → XSHE(15 深市 ETF)。
"""

from __future__ import annotations

import random
import re
import time
import warnings

import numpy as np
import pandas as pd

from aquant.data.provider import DAILY_COLUMNS

__all__ = ["EtfSource", "discover_etf_codes"]

_AKSHARE_INSTALL_HINT = (
    "AKShare 未安装。ETF 数据源依赖它,请执行:pip install 'aquant[akshare]'"
)
_DEFAULT_SLEEP_RANGE = (0.4, 0.9)
_DEFAULT_MAX_RETRIES = 3

# 东财 ETF 中文列 → 标准列(成交量单位为"手",×100 转股)
_EM_COLUMNS = {
    "开盘": "open", "收盘": "close", "最高": "high", "最低": "low",
    "成交量": "volume", "成交额": "total_turnover",
}
_REQUIRED = ("日期", "开盘", "收盘", "最高", "最低", "成交量", "成交额")


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


class EtfSource:
    """AKShare ETF 日线数据源(东财→新浪自动降级),实现 BarSource 协议。

    :param codes: ETF 代码列表,如 ``['510300', '159915']``;也可带后缀
    :param start/end: 'YYYY-MM-DD' 数据区间(闭区间)
    :param max_retries: 单接口最大重试次数(超过后降级到备源)
    :param sleep_range: 每次请求前的限速区间(秒)
    """

    def __init__(
        self,
        codes: list[str],
        start: str,
        end: str,
        *,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        sleep_range: tuple[float, float] = _DEFAULT_SLEEP_RANGE,
        names: dict[str, str] | None = None,
    ) -> None:
        if not codes:
            raise ValueError("ETF codes 不能为空,如 ['510300', '159915']")
        self._start = pd.Timestamp(start)
        self._end = pd.Timestamp(end)
        if self._start >= self._end:
            raise ValueError(f"start({start}) 必须早于 end({end})")
        self._oids = [self._normalize_code(c) for c in codes]
        if len(set(self._oids)) != len(self._oids):
            raise ValueError(f"codes 存在重复:{self._oids}")
        self._max_retries = int(max_retries)
        self._sleep_range = sleep_range
        self._names = {self._normalize_code(k): v for k, v in (names or {}).items()}

        self._ak = None
        self._calendar: pd.DatetimeIndex | None = None
        self._daily: dict[str, pd.DataFrame] = {}
        self._factor: dict[str, pd.Series] = {}
        self._source_used: dict[str, str] = {}  # oid -> 'eastmoney'/'sina'
        self._loaded: set[str] = set()
        # 东财熔断:一旦确认限流,本实例后续标的直接用新浪,避免每只都空等重试
        self._em_circuit_open = False

    # ---- BarSource 协议 ---------------------------------------------------
    def instruments(self) -> list[dict]:
        return [self._instrument_of(oid) for oid in self._oids]

    def daily_raw(self, oid: str) -> pd.DataFrame:
        self._ensure_loaded(oid)
        return self._daily[oid].copy()

    def adjust_factor(self, oid: str) -> pd.Series:
        self._ensure_loaded(oid)
        return self._factor[oid].copy()

    def dividends(self, oid: str) -> pd.DataFrame:
        # ETF 分红信息已含在 hfq 因子中;现金流到账对研究回测非必需 → 空表
        return pd.DataFrame(columns=[
            "book_closure_date", "announcement_date", "dividend_cash_before_tax",
            "ex_dividend_date", "payable_date", "round_lot",
        ])

    def splits(self, oid: str) -> pd.DataFrame:
        return pd.DataFrame(columns=["ex_date", "split_factor"])

    def suspended_days(self, oid: str) -> list[int]:
        self._ensure_loaded(oid)
        bar_days = {self._int8(d) for d in self._daily[oid].index}
        return [self._int8(d) for d in self._trading_calendar() if self._int8(d) not in bar_days]

    def st_days(self, oid: str) -> list[int]:
        return []  # ETF 无 ST 制度

    @property
    def order_book_ids(self) -> list[str]:
        return list(self._oids)

    @property
    def source_used(self) -> dict[str, str]:
        """每个标的最终使用的数据源(便于审计/日志)。"""
        return dict(self._source_used)

    # ---- 网络与加载 -------------------------------------------------------
    def _akshare(self):
        if self._ak is None:
            try:
                import akshare as ak
            except ImportError as e:
                raise ImportError(_AKSHARE_INSTALL_HINT) from e
            self._ak = ak
        return self._ak

    def _request(self, fn, *args, **kwargs):
        last_exc: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            _sleep(random.uniform(*self._sleep_range))
            try:
                return fn(*args, **kwargs)
            except Exception as exc:  # AKShare 接口异常形态多变,统一捕获重试
                last_exc = exc
                warnings.warn(
                    f"ETF 接口请求失败(第 {attempt}/{self._max_retries} 次):{exc!r}"
                )
        raise RuntimeError(
            f"ETF 接口连续 {self._max_retries} 次失败:{getattr(fn, '__name__', fn)}"
        ) from last_exc

    def _trading_calendar(self) -> pd.DatetimeIndex:
        if self._calendar is None:
            df = self._request(self._akshare().tool_trade_date_hist_sina)
            dates = pd.to_datetime(df["trade_date"])
            dates = dates[(dates >= self._start) & (dates <= self._end)]
            self._calendar = pd.DatetimeIndex(sorted(dates))
        return self._calendar

    def _ensure_loaded(self, oid: str) -> None:
        if oid in self._loaded:
            return
        if oid not in self._oids:
            raise KeyError(f"未知的 ETF 代码 {oid!r},可用:{self._oids}")
        raw, source = self._fetch_daily(oid, adjust="")
        if raw.empty:
            raise RuntimeError(
                f"ETF 数据源未返回 {oid} 在 {self._start:%Y-%m-%d}~{self._end:%Y-%m-%d} 的日线"
            )
        if source == "eastmoney":
            hfq, _ = self._fetch_daily(oid, adjust="hfq")
            factor = self._factor_from(raw, hfq)
        else:
            # 新浪接口无复权数据:真实价即信号价,因子置 1(不虚构复权)
            factor = pd.Series(1.0, index=raw.index, name="adjust_factor")
        self._daily[oid] = raw
        self._factor[oid] = factor
        self._source_used[oid] = source
        self._loaded.add(oid)

    def _fetch_daily(self, oid: str, adjust: str) -> tuple[pd.DataFrame, str]:
        """东财 fund_etf_hist_em → 新浪 fund_etf_hist_sina 自动降级。"""
        ak = self._akshare()
        code = oid.split(".")[0]
        if not self._em_circuit_open:
            try:
                df = self._request(
                    ak.fund_etf_hist_em,
                    symbol=code,
                    period="daily",
                    start_date=self._start.strftime("%Y%m%d"),
                    end_date=self._end.strftime("%Y%m%d"),
                    adjust=adjust,
                )
                return self._map_em(df), "eastmoney"
            except RuntimeError as em_exc:
                self._em_circuit_open = True  # 熔断:后续标的直接走新浪
                warnings.warn(
                    f"{oid}:东方财富 ETF 日线失败({em_exc}),降级新浪;本批次已熔断东财"
                )
        df = self._request(
            ak.fund_etf_hist_sina, symbol=self._sina_symbol(oid)
        )
        mapped = self._map_sina(df)
        # 新浪返回全历史,按区间裁剪
        mapped = mapped[(mapped.index >= self._start) & (mapped.index <= self._end)]
        return mapped, "sina"

    # ---- 数据映射 ---------------------------------------------------------
    @staticmethod
    def _map_em(df: pd.DataFrame) -> pd.DataFrame:
        missing = [c for c in _REQUIRED if c not in df.columns]
        if missing:
            raise RuntimeError(f"东财 ETF 日线缺少列 {missing},实际:{list(df.columns)}")
        index = pd.DatetimeIndex(pd.to_datetime(df["日期"]), name="date")
        out = pd.DataFrame(
            {
                "open": df["开盘"].to_numpy(dtype=float),
                "high": df["最高"].to_numpy(dtype=float),
                "low": df["最低"].to_numpy(dtype=float),
                "close": df["收盘"].to_numpy(dtype=float),
                "volume": df["成交量"].to_numpy(dtype=float) * 100.0,  # 手→股
                "total_turnover": df["成交额"].to_numpy(dtype=float),
            },
            index=index,
        )
        return out.sort_index()[list(DAILY_COLUMNS)]

    @staticmethod
    def _map_sina(df: pd.DataFrame) -> pd.DataFrame:
        missing = [c for c in ("date", "open", "close", "high", "low", "volume", "amount") if c not in df.columns]
        if missing:
            raise RuntimeError(f"新浪 ETF 日线缺少列 {missing},实际:{list(df.columns)}")
        index = pd.DatetimeIndex(pd.to_datetime(df["date"]), name="date")
        out = pd.DataFrame(
            {
                "open": df["open"].to_numpy(dtype=float),
                "high": df["high"].to_numpy(dtype=float),
                "low": df["low"].to_numpy(dtype=float),
                "close": df["close"].to_numpy(dtype=float),
                "volume": df["volume"].to_numpy(dtype=float),  # 已是股
                "total_turnover": df["amount"].to_numpy(dtype=float),
            },
            index=index,
        )
        return out.sort_index()[list(DAILY_COLUMNS)]

    @staticmethod
    def _factor_from(raw: pd.DataFrame, hfq: pd.DataFrame) -> pd.Series:
        if hfq.empty:
            return pd.Series(1.0, index=raw.index, name="adjust_factor")
        raw_close = raw["close"].reindex(raw.index)
        hfq_close = hfq["close"].reindex(raw.index).ffill().bfill()
        factor = hfq_close / raw_close.replace(0, np.nan)
        factor = factor.ffill().fillna(1.0)
        factor.name = "adjust_factor"
        return factor.astype(float)

    @staticmethod
    def _sina_symbol(oid: str) -> str:
        code, _, suffix = oid.partition(".")
        return ("sh" if suffix == "XSHG" else "sz") + code

    # ---- 元数据 -----------------------------------------------------------
    @staticmethod
    def _normalize_code(code: str) -> str:
        text = str(code).strip()
        if text.startswith(("sh", "sz")) and len(text) == 8:  # 新浪格式 sh510300
            symbol, suffix = text[2:], ("XSHG" if text[:2] == "sh" else "XSHE")
            return f"{symbol}.{suffix}"
        if "." in text:
            symbol, _, suffix = text.partition(".")
            suffix = suffix.upper()
            if suffix in ("XSHG", "XSHE"):
                return f"{symbol}.{suffix}"
            raise ValueError(f"无法识别的交易所后缀:{text!r}")
        if not (text.isdigit() and len(text) == 6):
            raise ValueError(f"ETF 代码 {text!r} 应为六位数字(如 510300)")
        if text.startswith(("5", "6")):
            return f"{text}.XSHG"
        if text.startswith(("1", "0", "3")):
            return f"{text}.XSHE"
        raise ValueError(f"无法识别的 ETF 代码前缀:{text!r}")

    def _instrument_of(self, oid: str) -> dict:
        symbol, _, suffix = oid.partition(".")
        name = self._names.get(oid) or f"ETF {symbol}"
        # ETF 涨跌停:科创/创业 ETF 20%,其余 10%(board_type 用 MainBoard/KSH/GEM)
        if symbol.startswith(("588", "589")):
            board = "KSH"
        elif symbol.startswith(("1599", "1598")) and not symbol.startswith("15992"):
            board = "GEM"
        else:
            board = "MainBoard"
        return {
            "order_book_id": oid,
            "symbol": symbol,
            "type": "ETF",
            "fund_type": "ETF",
            "listed_date": self._start.strftime("%Y-%m-%d"),
            "de_listed_date": "0000-00-00",
            "round_lot": 100,
            "exchange": suffix,
            "board_type": board,
            "market_tplus": 1,
            "status": "Active",
            "special_type": "Normal",
            "industry_code": "S90",
            "industry_name": name[:40],
        }

    @staticmethod
    def _int8(ts: pd.Timestamp) -> int:
        return int(ts.strftime("%Y%m%d"))


def discover_etf_codes(
    top_n: int | None = None,
    *,
    max_retries: int = _DEFAULT_MAX_RETRIES,
    sleep_range: tuple[float, float] = _DEFAULT_SLEEP_RANGE,
) -> list[tuple[str, str, float]]:
    """发现全市场 ETF 标的池,返回 [(六位代码, 名称, 成交额), ...]。

    优先东财实时列表(含成交额,便于按流动性排序),失败降级新浪分类列表
    (实测 927 只;成交额字段为字符串,尽力解析)。东财 push2 接口有 IP 限流,
    限流时自动走新浪,保证标的范围不中断。
    """
    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        _sleep(random.uniform(*sleep_range))
        try:
            import akshare as ak
            df = ak.fund_etf_spot_em()
            col_code = "代码"
            col_name = "名称"
            col_amount = "成交额"
            rows = []
            for _, r in df.iterrows():
                code = str(r.get(col_code, "")).strip()
                if not re.fullmatch(r"\d{6}", code):
                    continue
                amount = pd.to_numeric(r.get(col_amount), errors="coerce")
                rows.append((code, str(r.get(col_name, "")), 0.0 if pd.isna(amount) else float(amount)))
            rows.sort(key=lambda x: -x[2])
            return rows[:top_n] if top_n else rows
        except Exception as exc:
            last_exc = exc
            warnings.warn(f"东财 ETF 列表失败(第 {attempt}/{max_retries} 次):{exc!r}")

    warnings.warn(f"东财 ETF 列表不可用({last_exc!r}),降级新浪 fund_etf_category_sina")
    _sleep(random.uniform(*sleep_range))
    import akshare as ak

    frames = []
    for category in ("ETF基金", "场内货币", "QDII基金"):
        try:
            frames.append(ak.fund_etf_category_sina(symbol=category))
        except Exception as exc:
            warnings.warn(f"新浪 ETF 分类 {category} 获取失败:{exc!r}")
    if not frames:
        raise RuntimeError("新浪 ETF 列表全部分类获取失败,无法确定标的池")
    df = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["代码"])
    rows = []
    for _, r in df.iterrows():
        m = re.search(r"(\d{6})", str(r.get("代码", "")))
        if not m:
            continue
        amount = pd.to_numeric(r.get("成交额"), errors="coerce")
        rows.append((m.group(1), str(r.get("名称", "")), 0.0 if pd.isna(amount) else float(amount)))
    # 新浪成交额常为 0,按代码稳定排序保证可复现
    rows.sort(key=lambda x: (-x[2], x[0]))
    return rows[:top_n] if top_n else rows
