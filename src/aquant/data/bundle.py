"""Layer 2:RQAlpha 6.5.0 数据 bundle 构建器。

产物格式**逐条对照 rqalpha 6.5.0 源码**核实
(``.venv/.../rqalpha/data/base_data_source/{data_source.py,storages.py}``
与 ``rqalpha/data/bundle/__init__.py`` 官方生成器):

- ``stocks.h5``:每 CS 合约一个 dataset,结构化数组 dtype 精确等于
  ``SecuritiesDayBarStore.DEFAULT_DTYPE``;价格一律**真实价(不复权)**,
  limit_up/limit_down 由 ``aquant.data.limits.price_limits`` 按"前一交易日收盘价"计算
  (上市首日/新股无涨跌幅窗口用超宽占位值,见 ``_wide_limits``);停牌日不写行。
- ``indexes.h5``:``INDXDayBarStore.DEFAULT_DTYPE``(**不含** limit 字段)。
  ``BaseDataSource.available_data_range`` 硬编码读取 ``000001.XSHG`` 的日期范围来收敛
  回测区间(data_source.py L413;缺 dataset 时 rqalpha main.py 会把区间收敛为
  2005-01-04~2005-01-04 → "There is no data" 报错),因此**始终保证存在覆盖
  全部交易日历的 '000001.XSHG' dataset**;无真实基准数据时用成分股等权归一
  合成基准(仅用于区间收敛与基准对照,不参与撮合)。
- ``funds.h5``:每 ETF/LOF/REITs 合约一个 dataset(与 stocks.h5 同 dtype);
  ``futures.h5``:空文件(data_source.py 初始化强制要求存在)。
- ``dividends.h5``:字段与 interface.py 文档一致(book_closure_date/announcement_date/
  dividend_cash_before_tax/ex_dividend_date/payable_date/round_lot),
  账户模块按 ``dividend_cash_before_tax / round_lot`` 折算每股现金(position_model.py L262)。
- ``split_factor.h5``:ex_date(14 位 YYYYMMDD000000)+ split_factor。
- ``ex_cum_factor.h5``:由 ``aquant.data.adjust.ex_cum_factor_from`` 产出
  (start_date 14 位 + ex_cum_factor);rqalpha 在首条 start_date != 0 时
  自行补 (0, 1.0) 初始段(data_source.py get_ex_cum_factor)。
- ``suspended_days.h5`` / ``st_stock_days.h5``:每 oid int64 YYYYMMDD(8 位)数组。
- ``trading_dates.npy``:全部合约日期并集,int64 8 位升序(官方同格式)。
- ``instruments.pk``(**后缀 .pk**):pickle list[dict],键与 listed_date 格式对照
  ``load_instruments_from_pkl`` + ``Instrument._fix_date``('YYYY-MM-DD',
  '0000-00-00' → de_listed 默认 2999-12-31;CS 必须含 board_type,round_lot 会访问)。
- ``yield_curve.h5``:含 ``data`` dataset(date int64 8 位 + tenor float64 列,
  常数 0.02;tenor 名与 ``rqalpha.utils.risk_free_helper.YIELD_CURVE_TENORS`` 对齐,
  get_risk_free_rate 对缺失 tenor 逐级回退,最短 '0S' 恒存在)。
- ``future_info.json``:1 条 dummy 期货条目(FutureInfoStore.__init__ 对空列表
  ``next(iter({}))`` 会 StopIteration,且要求 margin_rate 键);条目结构对照
  官方 hard_code 模式(**只含 underlying_symbol**)——若同时含 order_book_id 与
  underlying_symbol,``_to_namedtuple`` 只删其一,会残留非法键导致 TypeError。
- ``share_transformation.json``:空表为 ``{}``(dict);官方 ``df.to_json(orient='index')``
  同构,``ShareTransformationStore.get_share_transformation`` 对 dict 缺键返回 None
  (空 list 会 TypeError)。
"""

from __future__ import annotations

import json
import pickle
from pathlib import Path

import h5py
import numpy as np
import pandas as pd

from aquant.compat import BENCHMARK_FALLBACK_OID, YIELD_CURVE_TENORS, current_profile
from aquant.data.adjust import date_to_int14, ex_cum_factor_from
from aquant.data.limits import price_limits

__all__ = ["build_bundle", "BENCHMARK_FALLBACK_OID", "YIELD_CURVE_TENORS"]

# dtype 事实来源已收口到 aquant.compat:换版本只需切换 profile,
# 本模块与 build_bundle 的自检不再直接依赖 rqalpha 私有类常量。

YIELD_RATE = 0.02

# 官方 gen_future_info 的 hard_code 条目模式(commission_type 必须在
# FutureInfoStore.COMMISSION_TYPE_MAP 内)
_FUTURE_INFO_DUMMY = {
    "underlying_symbol": "SYNDEM",
    "close_commission_ratio": 1.0,
    "close_commission_today_ratio": 0.0,
    "commission_type": "by_volume",
    "open_commission_ratio": 1.0,
    "margin_rate": 0.10,
    "tick_size": 1.0,
}

_DIVIDEND_DTYPE = np.dtype([
    ("book_closure_date", np.int64),
    ("announcement_date", np.int64),
    ("dividend_cash_before_tax", np.float64),
    ("ex_dividend_date", np.int64),
    ("payable_date", np.int64),
    ("round_lot", np.float64),
])

_SPLIT_DTYPE = np.dtype([("ex_date", np.int64), ("split_factor", np.float64)])


def _int8(ts: pd.Timestamp) -> int:
    return int(ts.year * 10000 + ts.month * 100 + ts.day)


def _date_of(value) -> pd.Timestamp:
    """'YYYY-MM-DD' / date / Timestamp → Timestamp。"""
    return pd.Timestamp(value)


def _collect(sources: list) -> dict:
    """合并多个 source:同 oid 先到先得,保持来源顺序。"""
    insts: dict[str, dict] = {}
    daily: dict[str, pd.DataFrame] = {}
    factor: dict[str, pd.Series] = {}
    dividends: dict[str, pd.DataFrame] = {}
    splits: dict[str, pd.DataFrame] = {}
    suspended: dict[str, list[int]] = {}
    st: dict[str, list[int]] = {}
    for src in sources:
        for inst in src.instruments():
            oid = inst["order_book_id"]
            if oid in insts:
                continue
            insts[oid] = dict(inst)
            daily[oid] = src.daily_raw(oid)
            factor[oid] = src.adjust_factor(oid)
            dividends[oid] = src.dividends(oid)
            splits[oid] = src.splits(oid)
            suspended[oid] = list(src.suspended_days(oid))
            st[oid] = list(src.st_days(oid))
    return {
        "instruments": insts, "daily": daily, "factor": factor,
        "dividends": dividends, "splits": splits,
        "suspended": suspended, "st": st,
    }


def _first_source_daily(sources: list, oid: str) -> pd.DataFrame | None:
    """首个能提供该 oid 非空日线的 source;均无则 None。"""
    for src in sources:
        try:
            df = src.daily_raw(oid)
        except Exception:
            continue
        if df is not None and len(df) > 0:
            return df
    return None


def _bars_from_df(df: pd.DataFrame, dtype: np.dtype) -> np.ndarray:
    """DataFrame → 指定 dtype 的结构化数组(datetime 14 位;缺失数值字段补 0)。"""
    bars = np.empty(len(df), dtype=dtype)
    bars["datetime"] = [date_to_int14(d) for d in df.index]
    for name in dtype.names:
        if name == "datetime":
            continue
        if name in df.columns:
            bars[name] = df[name].to_numpy(dtype=np.float64)
        else:
            bars[name] = 0.0
    return bars


def _synthetic_index_bars(data: dict, calendar: pd.DatetimeIndex) -> np.ndarray:
    """成分股等权归一合成基准(基点 1000):level = mean_i(close_i(t)/close_i(first_i))。

    仅用于 available_data_range 的区间收敛与基准对照,不参与撮合(volume=0)。
    """
    dtype = current_profile().index_bar_dtype
    norm = {f: [] for f in ("open", "high", "low", "close")}
    for df in data["daily"].values():
        if df.empty:
            continue
        first = df.iloc[0]
        for f in norm:
            norm[f].append(df[f] / first[f])
    n = len(calendar)
    bars = np.empty(n, dtype=dtype)
    bars["datetime"] = [date_to_int14(d) for d in calendar]
    if norm["close"]:
        for f in norm:
            series = pd.concat(norm[f], axis=1, sort=False).mean(axis=1)
            bars[f] = (series.reindex(calendar).ffill().bfill().to_numpy() * 1000.0)
    else:
        for f in norm:
            bars[f] = 1000.0
    bars["volume"] = 0.0
    bars["total_turnover"] = 0.0
    return bars


def _wide_limits(close: float) -> tuple[float, float]:
    """无有效涨跌幅(上市首日/新股窗口)时的超宽占位:close*10 / close*0.1。

    占位值远超任何正常价格波动,撮合的涨跌停检查不会实际约束成交。
    """
    return close * 10.0, max(close * 0.1, 0.01)


def _stock_bars_with_limits(
    oid: str,
    inst: dict,
    df: pd.DataFrame,
    suspended: list[int],
    st: set[int],
    calendar: pd.DatetimeIndex,
) -> np.ndarray:
    """真实价日线 + limits.py 涨跌停 → SecuritiesDayBarStore.DEFAULT_DTYPE 数组。

    - prev_close = 该合约前一**交易日**(本合约上一根已写 bar)的 close;
    - 首日无 prev_close,或处于无涨跌幅窗口(上市首日/创业板·科创板前 5 交易日)
      时,写超宽占位值(见 _wide_limits 注释);
    - 停牌日不写行(suspended 集合内的日期直接剔除)。
    """
    dtype = current_profile().stock_bar_dtype
    susp = set(suspended)
    keep = [d for d in df.index if _int8(d) not in susp]
    df = df.loc[keep]
    listed_date = _date_of(inst["listed_date"])
    # price_limits 的"上市后第几个交易日"按市场日历计数;先按上市日裁剪,避免逐 bar 全量扫描
    cal_after_listing = [d.date() for d in calendar if d >= listed_date]
    bars = np.empty(len(df), dtype=dtype)
    prev_close: float | None = None
    for i, (dt, row) in enumerate(df.iterrows()):
        close = float(row["close"])
        if prev_close is None:
            up, down = _wide_limits(close)
        else:
            limits = price_limits(
                oid,
                prev_close,
                date=dt.date(),
                is_st=(_int8(dt) in st),
                listed_date=listed_date,
                trading_dates=cal_after_listing,
            )
            if limits is None:
                up, down = _wide_limits(close)
            else:
                up, down = limits
        bars[i]["datetime"] = date_to_int14(dt)
        bars[i]["open"] = float(row["open"])
        bars[i]["close"] = close
        bars[i]["high"] = float(row["high"])
        bars[i]["low"] = float(row["low"])
        bars[i]["volume"] = float(row["volume"])
        bars[i]["total_turnover"] = float(row["total_turnover"])
        bars[i]["limit_up"] = up
        bars[i]["limit_down"] = down
        prev_close = close
    return bars


def _index_instrument(oid: str, calendar: pd.DatetimeIndex) -> dict:
    """基准指数的 INDX 合约元数据(键对照 Instrument._fix_date / round_lot)。"""
    suffix = oid.split(".")[-1] if "." in oid else "XSHG"
    return {
        "order_book_id": oid,
        "symbol": "合成基准指数" if oid == BENCHMARK_FALLBACK_OID else f"基准指数{oid}",
        "type": "INDX",
        "listed_date": calendar[0].strftime("%Y-%m-%d"),
        "de_listed_date": "0000-00-00",
        "round_lot": 1,
        "exchange": suffix,
        "market_tplus": 0,
        "status": "Active",
    }


def _write_factor_datasets(path: Path, per_oid_arrays: dict[str, np.ndarray]) -> None:
    with h5py.File(path, "w") as h5:
        for oid, arr in per_oid_arrays.items():
            if len(arr) > 0:
                h5.create_dataset(oid, data=arr)


def build_bundle(
    sources: list,
    out_dir: str | Path,
    benchmark_oid: str | None = None,
) -> Path:
    """从 BarSource 构建可直接被 rqalpha 6.5.0 BaseDataSource 加载的数据 bundle。

    :param sources: 实现 BarSource 协议的数据源(可多个,同 oid 先到先得)
    :param out_dir: 输出目录(不存在则创建)
    :param benchmark_oid: 基准指数 oid;None 或无真实数据时合成 '000001.XSHG' 等权基准
    :returns: bundle 目录 Path
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    data = _collect(sources)

    # 全市场交易日历 = 全部合约日线日期并集(升序)
    all_dates: set[pd.Timestamp] = set()
    for df in data["daily"].values():
        all_dates.update(df.index)
    calendar = pd.DatetimeIndex(sorted(all_dates))
    if len(calendar) == 0:
        raise ValueError("sources 未提供任何日线数据,无法构建 bundle")

    # ---- stocks.h5 / funds.h5(真实价 + limits.py 涨跌停;停牌日不写行)----
    # RQAlpha 6.5.0:CS → stocks.h5;ETF/LOF/REITs → funds.h5(同一 SecuritiesDayBarStore
    # dtype)。按合约 type 分流,ETF 的 type='ETF' 且带 fund_type 由 source 提供。
    _fund_types = {"ETF", "LOF", "REITs"}
    with h5py.File(out / "stocks.h5", "w") as h5_stocks, \
            h5py.File(out / "funds.h5", "w") as h5_funds:
        for inst in data["instruments"].values():
            oid = inst["order_book_id"]
            df = data["daily"][oid]
            if df.empty:
                continue
            bars = _stock_bars_with_limits(
                oid, inst, df, data["suspended"][oid], set(data["st"][oid]), calendar
            )
            target = h5_funds if inst.get("type") in _fund_types else h5_stocks
            target.create_dataset(oid, data=bars)

    # ---- indexes.h5 ----
    # '000001.XSHG' 恒存在(available_data_range 硬编码,见模块 docstring);
    # benchmark_oid 有真实数据时额外写其 dataset 并注册 INDX 合约。
    index_inst_oids = [BENCHMARK_FALLBACK_OID]
    fallback = data["daily"].get(BENCHMARK_FALLBACK_OID)
    if fallback is None or fallback.empty:
        if benchmark_oid == BENCHMARK_FALLBACK_OID:
            fallback = _first_source_daily(sources, benchmark_oid)
        else:
            fallback = None
    if fallback is not None:
        fallback_bars = _bars_from_df(fallback, INDXDayBarStore.DEFAULT_DTYPE)
    else:
        fallback_bars = _synthetic_index_bars(data, calendar)
    with h5py.File(out / "indexes.h5", "w") as h5:
        h5.create_dataset(BENCHMARK_FALLBACK_OID, data=fallback_bars)
        if benchmark_oid and benchmark_oid != BENCHMARK_FALLBACK_OID:
            df = _first_source_daily(sources, benchmark_oid)
            if df is not None:
                h5.create_dataset(
                    benchmark_oid, data=_bars_from_df(df, INDXDayBarStore.DEFAULT_DTYPE)
                )
                index_inst_oids.append(benchmark_oid)

    # ---- futures.h5:空文件(data_source.py 初始化强制要求存在)----
    # funds.h5 已在上面按 ETF 分流写入(无 ETF 时仍为空文件,要求满足)
    with h5py.File(out / "futures.h5", "w"):
        pass

    # ---- dividends.h5 / split_factor.h5 / ex_cum_factor.h5 ----
    div_arrays: dict[str, np.ndarray] = {}
    split_arrays: dict[str, np.ndarray] = {}
    ex_arrays: dict[str, np.ndarray] = {}
    for oid, inst in data["instruments"].items():
        div_df = data["dividends"].get(oid)
        if div_df is not None and len(div_df) > 0:
            arr = np.empty(len(div_df), dtype=_DIVIDEND_DTYPE)
            for field in _DIVIDEND_DTYPE.names:
                values = div_df[field].to_numpy()
                if field in ("dividend_cash_before_tax", "round_lot"):
                    arr[field] = values.astype(np.float64)
                else:
                    arr[field] = values.astype(np.int64)
            div_arrays[oid] = arr
        split_df = data["splits"].get(oid)
        if split_df is not None and len(split_df) > 0:
            arr = np.empty(len(split_df), dtype=_SPLIT_DTYPE)
            arr["ex_date"] = split_df["ex_date"].to_numpy(dtype=np.int64)
            arr["split_factor"] = split_df["split_factor"].to_numpy(dtype=np.float64)
            split_arrays[oid] = arr
        df = data["daily"][oid]
        if not df.empty:
            ex_arrays[oid] = ex_cum_factor_from(df, data["factor"][oid])
    _write_factor_datasets(out / "dividends.h5", div_arrays)
    _write_factor_datasets(out / "split_factor.h5", split_arrays)
    _write_factor_datasets(out / "ex_cum_factor.h5", ex_arrays)

    # ---- suspended_days.h5 / st_stock_days.h5(8 位 int;无记录建空文件)----
    for name, key in (("suspended_days.h5", "suspended"), ("st_stock_days.h5", "st")):
        with h5py.File(out / name, "w") as h5:
            for oid, days in data[key].items():
                if days:
                    h5.create_dataset(oid, data=np.asarray(sorted(days), dtype=np.int64))

    # ---- trading_dates.npy(int64 8 位升序)----
    np.save(
        out / "trading_dates.npy",
        np.asarray([_int8(d) for d in calendar], dtype=np.int64),
        allow_pickle=False,
    )

    # ---- instruments.pk(pickle list[dict];CS + 基准 INDX)----
    instruments = [dict(inst) for inst in data["instruments"].values()]
    instruments.extend(_index_instrument(oid, calendar) for oid in index_inst_oids)
    with open(out / "instruments.pk", "wb") as f:
        pickle.dump(instruments, f, protocol=2)

    # ---- yield_curve.h5(data dataset:date 8 位 + tenor 常数 0.02)----
    yc_dtype = np.dtype([("date", np.int64)] + [(t, np.float64) for t in YIELD_CURVE_TENORS])
    yc = np.empty(len(calendar), dtype=yc_dtype)
    yc["date"] = [_int8(d) for d in calendar]
    for t in YIELD_CURVE_TENORS:
        yc[t] = YIELD_RATE
    with h5py.File(out / "yield_curve.h5", "w") as h5:
        h5.create_dataset("data", data=yc)

    # ---- future_info.json(≥1 条目,见 FutureInfoStore.__init__ 强制要求)----
    with open(out / "future_info.json", "w", encoding="utf-8") as f:
        json.dump([_FUTURE_INFO_DUMMY], f, ensure_ascii=False, indent=2)

    # ---- share_transformation.json(空 dict,ShareTransformationStore 安全结构)----
    with open(out / "share_transformation.json", "w", encoding="utf-8") as f:
        json.dump({}, f)

    return out
