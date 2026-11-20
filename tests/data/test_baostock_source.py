"""BaostockSource 零网络桩测试:真实价/复权因子/停牌/ST。"""
import sys
from types import SimpleNamespace
import pandas as pd
from aquant.data.baostock_source import BaostockSource, _oid

FIELDS = ("date,code,open,high,low,close,preclose,volume,amount,"
          "adjustflag,turn,tradestatus,pctChg,isST").split(",")
DATES = pd.date_range("2024-01-02", periods=3, freq="B")


class RS:
    def __init__(self, fields, rows):
        self.fields, self.rows, self.error_code = fields, rows, "0"

    def get_data(self):
        return iter(self.rows)


class FakeBS:
    @staticmethod
    def login():
        return SimpleNamespace(error_code="0", error_msg="")

    @staticmethod
    def logout():
        pass

    @staticmethod
    def query_history_k_data_plus(code, fields, start, end, frequency, adjustflag):
        scale = 1.0 if adjustflag == "3" else 1.1
        rows = []
        for i, d in enumerate(DATES):
            rows.append([
                d.strftime("%Y-%m-%d"), code,
                f"{10 * scale:.4f}", f"{11 * scale:.4f}", f"{9 * scale:.4f}",
                f"{10.5 * scale:.4f}", f"{10.2 * scale:.4f}", "1000000",
                f"{10500000 * scale:.2f}", adjustflag, "1.5",
                "0" if i == 1 else "1", "0.5", "1" if i == 2 else "0",
            ])
        return RS(FIELDS, rows)


def _source(monkeypatch):
    monkeypatch.setitem(sys.modules, "baostock", FakeBS)
    monkeypatch.setattr("aquant.data.baostock_source.time.sleep", lambda _: None)
    return BaostockSource(["000001"], "2024-01-01", "2024-03-01")


def test_code_mapping():
    assert _oid("000001") == "000001.XSHE"
    assert _oid("600519") == "600519.XSHG"
    assert _oid("sh.600519") == "600519.XSHG"


def test_raw_factor_and_status(monkeypatch):
    src = _source(monkeypatch)
    oid = "000001.XSHE"
    raw = src.daily_raw(oid)
    assert list(raw.columns) == ["open", "high", "low", "close", "volume", "total_turnover"]
    assert raw["close"].iloc[0] == 10.5
    factor = src.adjust_factor(oid)
    assert abs(factor.iloc[0] - 1.1) < 1e-9
    assert src.suspended_days(oid) == [int(DATES[1].strftime("%Y%m%d"))]
    assert src.st_days(oid) == [int(DATES[2].strftime("%Y%m%d"))]
