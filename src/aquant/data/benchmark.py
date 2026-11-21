"""Layer 2:真实 A 股基准指数注入既有 bundle。

背景:build_bundle 的 ``indexes.h5`` 只保证 ``000001.XSHG`` 恒存在;无真实指数
数据时用成分股等权归一**合成基准**(见 bundle._synthetic_index_bars)。合成基准
可满足区间收敛与形态对照,但不是真实 A 股基准表现。

本模块把真实指数日线(Baostock 指数接口,免 token、稳定)注入既有 bundle:

1. ``indexes.h5``:写入/覆盖指数 dataset(INDXDayBarStore.DEFAULT_DTYPE;
   ``000001.XSHG`` 被真实上证指数覆盖后,``available_data_range`` 读到的仍是
   真实交易日范围,与 trading_dates.npy 一致);
2. ``instruments.pk``:注册/替换 INDX 合约(中文名:上证指数/沪深300…);
3. ``benchmark_meta.json``:来源与行数,供 API/前端如实标注"真实基准"。

设计要点:
- 指数行情**按 bundle 交易日历裁剪对齐**(只保留日历内的交易日,不引入
  bundle 之外的日期,避免 available_data_range 与 trading_dates.npy 不一致);
- 注入函数与抓取函数分离:``inject_benchmark`` 纯本地可测(单测喂合成 DataFrame),
  CLI/测试均不经网络;
- 幂等:重复运行覆盖同名 dataset 与同名 INDX 合约,meta 亦覆盖。
"""

from __future__ import annotations

import json
import pickle
from datetime import datetime
from pathlib import Path

import h5py
import numpy as np
import pandas as pd

from aquant.compat import current_profile
from aquant.data.adjust import date_to_int14

__all__ = ["inject_benchmark", "fetch_baostock_index", "DEFAULT_BENCHMARK_OIDS"]

# 常用 A 股基准(前端基准下拉与本文档同名);键为 rqalpha order_book_id
DEFAULT_BENCHMARK_LABELS = {
    "000001.XSHG": "上证指数",
    "000300.XSHG": "沪深300",
    "000905.XSHG": "中证500",
    "399006.XSHE": "创业板指",
}

# 默认注入:上证指数(平台默认基准,fallback oid)+ 沪深300(最常用对照)
DEFAULT_BENCHMARK_OIDS = ("000001.XSHG", "000300.XSHG")

# oid → Baostock 指数代码(XSHG=沪市 sh.,XSHE=深市 sz.)
_BAOSTOCK_EXCHANGE = {"XSHG": "sh", "XSHE": "sz", "BJ": "bj"}


def oid_to_baostock_code(oid: str) -> str:
    """'000300.XSHG' → 'sh.000300'(Baostock 指数代码形态)。"""
    symbol, _, suffix = oid.partition(".")
    prefix = _BAOSTOCK_EXCHANGE.get(suffix or "XSHG", "sh")
    return f"{prefix}.{symbol}"


def fetch_baostock_index(
    oid: str,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
) -> pd.DataFrame:
    """从 Baostock 抓单只指数日线(真实开高低收/成交量额,不复权概念)。

    :returns: DataFrame(index=DatetimeIndex, columns=open/high/low/close/volume/total_turnover)
    :raises RuntimeError: 登录失败或查询出错(调用方给出中文指引)
    """
    import baostock as bs

    code = oid_to_baostock_code(oid)
    login = bs.login()
    if getattr(login, "error_code", "0") != "0":
        raise RuntimeError(f"Baostock 登录失败:{login.error_msg}")
    try:
        rs = bs.query_history_k_data_plus(
            code,
            "date,code,open,high,low,close,volume,amount",
            start_date=str(pd.Timestamp(start).date()),
            end_date=str(pd.Timestamp(end).date()),
            frequency="d",
            adjustflag="3",
        )
        if getattr(rs, "error_code", "0") != "0":
            raise RuntimeError(f"Baostock 指数查询失败({code}):{rs.error_msg}")
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
    finally:
        bs.logout()

    if not rows:
        raise RuntimeError(f"Baostock 未返回指数数据({code}),请检查网络或代码。")
    df = pd.DataFrame(rows, columns=["date", "code", "open", "high", "low", "close", "volume", "amount"])
    df = df.drop(columns=["code"])
    for col in ("open", "high", "low", "close", "volume", "amount"):
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    df = df.rename(columns={"amount": "total_turnover"})
    df.index = pd.DatetimeIndex(pd.to_datetime(df.pop("date")))
    return df.sort_index()


def _index_instrument(oid: str, symbol: str, first_day: pd.Timestamp) -> dict:
    """INDX 合约元数据(键与 bundle._index_instrument 同构,名称用真实指数中文名)。"""
    suffix = oid.split(".")[-1] if "." in oid else "XSHG"
    return {
        "order_book_id": oid,
        "symbol": symbol,
        "type": "INDX",
        "listed_date": first_day.strftime("%Y-%m-%d"),
        "de_listed_date": "0000-00-00",
        "round_lot": 1,
        "exchange": suffix,
        "market_tplus": 0,
        "status": "Active",
    }


def inject_benchmark(
    bundle_dir: str | Path,
    frames: dict[str, pd.DataFrame],
    *,
    labels: dict[str, str] | None = None,
    source: str = "baostock",
) -> dict:
    """把真实指数日线注入既有 bundle(覆盖同名 dataset / INDX 合约 / meta)。

    :param bundle_dir: bundle 目录(须已含 indexes.h5 / instruments.pk / trading_dates.npy)
    :param frames: {oid: DataFrame(index=日期, columns=open/high/low/close/volume/total_turnover)}
    :param labels: {oid: 中文名};缺省用 DEFAULT_BENCHMARK_LABELS,再缺省 f"指数{oid}"
    :param source: 数据来源标注(写入 benchmark_meta.json)
    :returns: benchmark_meta dict(与落盘文件同构)
    """
    bundle = Path(bundle_dir)
    labels = {**DEFAULT_BENCHMARK_LABELS, **(labels or {})}
    if not (bundle / "indexes.h5").exists() or not (bundle / "instruments.pk").exists():
        raise FileNotFoundError(f"bundle 不完整,缺少 indexes.h5 / instruments.pk:{bundle}")

    # bundle 交易日历:注入的指数行只保留日历内的交易日(保证区间口径一致)
    cal = pd.DatetimeIndex(
        pd.to_datetime(
            np.load(bundle / "trading_dates.npy").astype(str), format="%Y%m%d"
        )
    ).sort_values()
    cal_set = set(cal)
    dtype = current_profile().index_bar_dtype

    meta: dict = {
        "source": source,
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "indexes": {},
    }
    written_oids: list[str] = []
    with h5py.File(bundle / "indexes.h5", "a") as h5:
        for oid, df in frames.items():
            if df is None or df.empty:
                continue
            aligned = df[df.index.isin(cal_set)].sort_index()
            if aligned.empty:
                continue
            bars = np.empty(len(aligned), dtype=dtype)
            bars["datetime"] = [date_to_int14(d) for d in aligned.index]
            for name in dtype.names:
                if name == "datetime":
                    continue
                bars[name] = (
                    aligned[name].to_numpy(dtype=np.float64)
                    if name in aligned.columns
                    else 0.0
                )
            if oid in h5:
                del h5[oid]  # 覆盖真实数据(如 000001.XSHG 的合成基准)
            h5.create_dataset(oid, data=bars)
            written_oids.append(oid)
            meta["indexes"][oid] = {
                "symbol": labels.get(oid, f"指数{oid}"),
                "rows": int(len(bars)),
                "start": aligned.index.min().strftime("%Y-%m-%d"),
                "end": aligned.index.max().strftime("%Y-%m-%d"),
            }

    if not written_oids:
        raise RuntimeError("没有可用指数数据被写入(输入为空或与交易日历无交集)。")

    # instruments.pk:替换同名 INDX 合约(保持 pickle list[dict] 官方格式)
    with open(bundle / "instruments.pk", "rb") as f:
        instruments = pickle.load(f)
    first_day = cal[0]
    new_insts = {
        oid: _index_instrument(oid, labels.get(oid, f"指数{oid}"), first_day)
        for oid in written_oids
    }
    instruments = [i for i in instruments if i.get("order_book_id") not in new_insts]
    instruments.extend(new_insts.values())
    with open(bundle / "instruments.pk", "wb") as f:
        pickle.dump(instruments, f)

    (bundle / "benchmark_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return meta


def main(argv: list[str] | None = None) -> int:
    """CLI:python -m aquant.data.benchmark --bundle ~/.aquant/bundle-baostock \
        --indexes 000001.XSHG,000300.XSHG"""
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="python -m aquant.data.benchmark",
        description="向既有 RQAlpha bundle 注入真实 A 股基准指数日线(Baostock)",
    )
    parser.add_argument("--bundle", required=True, help="bundle 目录")
    parser.add_argument("--indexes", default=",".join(DEFAULT_BENCHMARK_OIDS),
                        help="逗号分隔指数 oid(默认 000001.XSHG,000300.XSHG)")
    args = parser.parse_args(argv)

    bundle = Path(args.bundle)
    if not (bundle / "trading_dates.npy").exists():
        print(f"bundle 不完整(缺 trading_dates.npy):{bundle}", file=sys.stderr)
        return 2

    # 日历范围:指数行情抓取范围与 bundle 交易日历一致
    cal = np.load(bundle / "trading_dates.npy")
    start = f"{int(cal.min()):08d}"[:4] + "-" + f"{int(cal.min()):08d}"[4:6] + "-" + f"{int(cal.min()):08d}"[6:]
    end = f"{int(cal.max()):08d}"[:4] + "-" + f"{int(cal.max()):08d}"[4:6] + "-" + f"{int(cal.max()):08d}"[6:]

    frames: dict[str, pd.DataFrame] = {}
    for oid in [s.strip() for s in args.indexes.split(",") if s.strip()]:
        print(f"[benchmark] 抓取 {oid}({oid_to_baostock_code(oid)}){start} ~ {end} …", file=sys.stderr)
        try:
            frames[oid] = fetch_baostock_index(oid, start, end)
        except Exception as exc:  # noqa: BLE001 单只失败不拖垮整批,但要有明确输出
            print(f"[benchmark] {oid} 抓取失败:{exc}", file=sys.stderr)
    if not frames:
        print("没有抓到任何指数数据,注入中止(Baostock 需可访问外网)。", file=sys.stderr)
        return 1

    meta = inject_benchmark(bundle, frames)
    for oid, info in meta["indexes"].items():
        print(f"[benchmark] 已写入 {oid}({info['symbol']}):"
              f"{info['rows']} 行,{info['start']} ~ {info['end']}")
    print(f"[benchmark] 元数据:{bundle / 'benchmark_meta.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
