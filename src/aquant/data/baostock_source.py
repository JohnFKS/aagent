"""Baostock 主力日线源:真实价 + 后复权因子派生,停牌/ST 取原始字段。"""
from __future__ import annotations
import random, time
from typing import Any
import pandas as pd
from aquant.data.provider import DAILY_COLUMNS
__all__ = ["BaostockSource"]
_FIELDS = "date,code,open,high,low,close,preclose,volume,amount,adjustflag,turn,tradestatus,pctChg,isST"
_NUM = ("open", "high", "low", "close", "preclose", "volume", "amount", "turn", "pctChg")
_INT = ("tradestatus", "isST")


def _oid(code: str) -> str:
    c = str(code).strip()
    if c.startswith(("sh.", "sz.", "bj.")):
        n = c.split(".", 1)[1]
        s = {"sh": "XSHG", "sz": "XSHE", "bj": "BJ"}[c[:2]]
        return f"{n}.{s}"
    if "." in c:
        n, s = c.split(".", 1)
        if s in {"XSHE", "XSHG", "BJ"}:
            return f"{n}.{s}"
    else:
        n = c
    if n.startswith("6"):
        return f"{n}.XSHG"
    if n.startswith(("4", "8", "92")):
        return f"{n}.BJ"
    return f"{n}.XSHE"


def _bcode(oid: str) -> str:
    n, s = oid.split(".", 1)
    return {"XSHG": f"sh.{n}", "XSHE": f"sz.{n}", "BJ": f"bj.{n}"}[s]


def _i8(d: Any) -> int:
    return int(pd.Timestamp(d).strftime("%Y%m%d"))


class BaostockSource:
    def __init__(self, codes: list[str], start: str, end: str,
                 *, max_retries: int = 3,
                 sleep_range: tuple[float, float] = (0.10, 0.30)) -> None:
        if not codes:
            raise ValueError("codes 不能为空")
        self.start, self.end = pd.Timestamp(start), pd.Timestamp(end)
        if self.start >= self.end:
            raise ValueError("start 必须早于 end")
        self.oids = list(dict.fromkeys(_oid(c) for c in codes))
        self.max_retries, self.sleep_range = int(max_retries), sleep_range
        self._bs: Any = None
        self._daily: dict[str, pd.DataFrame] = {}
        self._factor: dict[str, pd.Series] = {}
        self._status: dict[str, pd.DataFrame] = {}
        self._basics: dict[str, dict] = {}
        self._loaded: set[str] = set()

    def _baostock(self) -> Any:
        if self._bs is None:
            try:
                import baostock as bs
            except ImportError as exc:
                raise ImportError("请先安装:pip install 'aquant[baostock]'") from exc
            lg = bs.login()
            if str(getattr(lg, "error_code", "0")) != "0":
                raise RuntimeError(f"Baostock 登录失败:{getattr(lg, 'error_msg', lg)}")
            self._bs = bs
        return self._bs

    def _req(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        last: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            time.sleep(random.uniform(*self.sleep_range))
            try:
                rs = fn(*args, **kwargs)
                if str(getattr(rs, "error_code", "0")) != "0":
                    raise RuntimeError(str(getattr(rs, "error_msg", rs.error_code)))
                return rs
            except Exception as exc:
                last = exc
                if attempt == self.max_retries:
                    break
        raise RuntimeError(f"Baostock 请求失败(重试 {self.max_retries} 次):{last}")

    @staticmethod
    def _rows_of(rs: Any) -> list[dict]:
        data = rs.get_data()
        if isinstance(data, pd.DataFrame):
            return data.to_dict("records")
        fields = list(rs.fields)
        return [dict(zip(fields, r, strict=False)) for r in data]

    @classmethod
    def _frame(cls, rs: Any) -> pd.DataFrame:
        fields = list(rs.fields)
        rows = cls._rows_of(rs)
        if not rows:
            return pd.DataFrame(columns=fields)
        df = pd.DataFrame(rows)
        for c in _NUM:
            if c in df:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        for c in _INT:
            if c in df:
                df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype(int)
        if "date" in df:
            df.index = pd.DatetimeIndex(pd.to_datetime(df["date"]))
            df = df.sort_index()
        return df

    def _load_basic(self, bs: Any, code: str) -> dict:
        try:
            rows = self._rows_of(self._req(bs.query_stock_basic, code=code))
            return rows[0] if rows else {}
        except Exception:
            return {}

    def _load(self, oid: str) -> None:
        if oid in self._loaded:
            return
        bs = self._baostock()
        code = _bcode(oid)
        s, e = self.start.strftime("%Y-%m-%d"), self.end.strftime("%Y-%m-%d")
        raw = self._frame(self._req(bs.query_history_k_data_plus, code, _FIELDS, s, e, "d", "3"))
        hfq = self._frame(self._req(bs.query_history_k_data_plus, code, _FIELDS, s, e, "d", "1"))
        if raw.empty:
            raise ValueError(f"{oid}:Baostock 区间 {s}~{e} 无日线数据")
        raw = raw[~raw["close"].isna()]
        hfq_close = hfq["close"].reindex(raw.index) if not hfq.empty else pd.Series(dtype=float)
        factor = (hfq_close / raw["close"]).ffill().fillna(1.0).astype(float)
        factor.name = "ex_cum_factor"
        daily = pd.DataFrame(index=raw.index)
        for col in DAILY_COLUMNS:
            daily[col] = raw["amount" if col == "total_turnover" else col]
        self._daily[oid] = daily[list(DAILY_COLUMNS)]
        self._factor[oid] = factor
        self._status[oid] = raw[list(_INT)] if set(_INT) <= set(raw) else pd.DataFrame(index=raw.index)
        self._basics[oid] = self._load_basic(bs, code)
        self._loaded.add(oid)

    def instruments(self) -> list[dict]:
        out = []
        for oid in self.oids:
            self._load(oid)
            info = self._basics.get(oid, {})
            btype = str(info.get("type", "1"))
            listed = info.get("ipoDate") or self.start.strftime("%Y-%m-%d")
            out_date = info.get("outDate") or ""
            delisted = str(info.get("status", "1")) == "0" or bool(out_date)
            symbol, _, suffix = oid.partition(".")
            board = "BJS" if suffix == "BJ" else (
                "KSH" if symbol.startswith("688") else
                "GEM" if symbol.startswith(("300", "301")) else "MainBoard"
            )
            item = {
                "order_book_id": oid,
                "symbol": info.get("code_name") or symbol,
                "type": "ETF" if btype == "5" else "CS",
                "listed_date": listed,
                "de_listed_date": out_date if delisted and out_date else "0000-00-00",
                "round_lot": 100,
                "exchange": suffix,
                "board_type": board,
                "market_tplus": 1,
                "status": "Delisted" if delisted else "Active",
                "special_type": "ST" if self.st_days(oid) else "Normal",
                "industry_code": "S90",
                "industry_name": "Baostock(仅供研究)",
            }
            if btype == "5":
                item["fund_type"] = "ETF"
            out.append(item)
        return out

    def daily_raw(self, oid: str) -> pd.DataFrame:
        self._load(oid)
        return self._daily[oid].copy()

    def adjust_factor(self, oid: str) -> pd.Series:
        self._load(oid)
        return self._factor[oid].copy()

    def dividends(self, oid: str) -> pd.DataFrame:
        self._load(oid)
        return pd.DataFrame(columns=[
            "book_closure_date", "announcement_date", "dividend_cash_before_tax",
            "ex_dividend_date", "payable_date", "round_lot",
        ])

    def splits(self, oid: str) -> pd.DataFrame:
        self._load(oid)
        return pd.DataFrame(columns=["ex_date", "split_factor"])

    def suspended_days(self, oid: str) -> list[int]:
        self._load(oid)
        st = self._status[oid]
        if "tradestatus" not in st:
            return []
        return [_i8(d) for d in st.index[st["tradestatus"] == 0]]

    def st_days(self, oid: str) -> list[int]:
        self._load(oid)
        st = self._status[oid]
        if "isST" not in st:
            return []
        return [_i8(d) for d in st.index[st["isST"] == 1]]

    def close(self) -> None:
        if self._bs is not None:
            self._bs.logout()
            self._bs = None


