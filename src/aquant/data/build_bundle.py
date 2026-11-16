"""Layer 2:bundle 构建 CLI。

用法::

    python -m aquant.data.build_bundle --source etf --out ~/.aquant/bundle \
        [--top 300 --start 2021-01-01 --end 2026-11-15 --benchmark 000001.XSHG]
    python -m aquant.data.build_bundle --source akshare --out ... --codes 000001,600519
    python -m aquant.data.build_bundle --source synthetic --out ... --n 5

数据源(``--source`` 缺省为 ``etf``,免费方案中覆盖最全、双源降级最稳):
- ``etf``      → EtfSource:AKShare 双源自动降级(东财 fund_etf_hist_em → 新浪
                 fund_etf_hist_sina),从全市场 ETF 按成交额取 Top-N,需
                 ``pip install 'aquant[akshare]'``;
- ``akshare``  → AkshareSource:AKShare A 股股票真实日线(东财→新浪降级);
- ``synthetic``→ SyntheticSource:确定性合成数据(测试/演示)。

构建完成后打印产物清单并自检:用 rqalpha  storages 真实加载 instruments 与
stocks.h5 / funds.h5 首行,确保 bundle 可被 BaseDataSource 直接消费。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from aquant.data.bundle import build_bundle

_SOURCES = ("etf", "synthetic", "akshare")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m aquant.data.build_bundle",
        description="构建 RQAlpha 6.5.0 数据 bundle(默认 etf:AKShare 全市场 ETF 双源降级)",
    )
    parser.add_argument("--source", choices=_SOURCES, default="etf",
                        help="数据源(默认 etf):etf=全市场ETF双源降级;akshare=A股股票;synthetic=合成")
    parser.add_argument("--out", required=True, help="bundle 输出目录")
    parser.add_argument("--n", type=int, default=5, help="[synthetic] 合约数量(默认 5)")
    parser.add_argument("--start", default="2021-01-01", help="起始日 YYYY-MM-DD(默认 2021-01-01)")
    parser.add_argument("--end", default="2024-12-31", help="结束日 YYYY-MM-DD(默认 2024-12-31)")
    parser.add_argument("--seed", type=int, default=42, help="[synthetic] 随机种子(默认 42)")
    parser.add_argument("--codes", default=None,
                        help="逗号分隔代码(akshare/etf 可指定;etf 不指定则自动发现全市场 Top-N)")
    parser.add_argument("--top", type=int, default=300,
                        help="[etf] 自动发现时按成交额取前 N 只(默认 300)")
    parser.add_argument("--benchmark", default=None, help="基准指数 oid(如 000001.XSHG)")
    return parser


def _parse_args(parser: argparse.ArgumentParser, argv: list[str] | None) -> argparse.Namespace:
    return parser.parse_args(argv)


def _make_source(args: argparse.Namespace, parser: argparse.ArgumentParser):
    codes = [c.strip() for c in args.codes.split(",") if c.strip()] if args.codes else None
    if args.source == "synthetic":
        from aquant.data.akshare_source import AkshareSource
        from aquant.data.synthetic_source import SyntheticSource

        if codes is not None:
            codes = [AkshareSource._normalize_code(c) if "." not in c else c for c in codes]
        if codes is not None and len(codes) < args.n:
            codes = None
        return SyntheticSource(
            n_instruments=args.n, start=args.start, end=args.end,
            seed=args.seed, codes=codes,
        )

    if args.source == "etf":
        from aquant.data.etf_source import EtfSource, discover_etf_codes

        if codes:
            discovered = [(c, "", 0.0) for c in codes]
        else:
            print(f"[etf] 自动发现全市场 ETF(取成交额 Top-{args.top})…", file=sys.stderr)
            discovered = discover_etf_codes(top_n=args.top)
            if not discovered:
                parser.error("ETF 标的池发现失败,请用 --codes 显式指定")
            codes = [c for c, _, _ in discovered]
            print(f"[etf] 已发现 {len(codes)} 只 ETF,开始抓取日线", file=sys.stderr)
        names = {c: name for c, name, _ in discovered if name}
        return EtfSource(codes=codes, start=args.start, end=args.end, names=names)

    from aquant.data.akshare_source import AkshareSource

    if not codes:
        parser.error("--source akshare 需要 --codes 000001,600519")
    return AkshareSource(codes=codes, start=args.start, end=args.end)


def _self_check(out: Path) -> None:
    """自检:用 rqalpha storages 真实加载 instruments 与 stocks/funds 首行。"""
    from rqalpha.data.base_data_source.storages import (
        FutureInfoStore,
        SecuritiesDayBarStore,
        load_instruments_from_pkl,
    )

    future_info = FutureInfoStore(str(out / "future_info.json"), {})
    instruments = load_instruments_from_pkl(str(out / "instruments.pk"), future_info)
    cs = [i for i in instruments if i.type == "CS"]
    etfs = [i for i in instruments if i.type == "ETF"]
    print(f"[self-check] instruments 加载成功:共 {len(instruments)} 条"
          f"(CS {len(cs)} 条,ETF {len(etfs)} 条,其他 {len(instruments) - len(cs) - len(etfs)} 条)")
    for insts, filename, label in ((cs, "stocks.h5", "CS"), (etfs, "funds.h5", "ETF")):
        if not insts:
            continue
        first = insts[0]
        print(f"[self-check] 首个 {label} 合约:{first.order_book_id}({first.symbol}),"
              f"listed_date={first.listed_date:%Y-%m-%d}")
        store = SecuritiesDayBarStore(str(out / filename))
        bars = store.get_bars(first.order_book_id)
        if len(bars) > 0:
            row = bars[0]
            print(f"[self-check] {filename} 首行[{first.order_book_id}]:"
                  f"datetime={row['datetime']} open={row['open']:.4f} close={row['close']:.4f} "
                  f"limit_up={row['limit_up']:.4f} limit_down={row['limit_down']:.4f}")
        else:
            print(f"[self-check] {filename}[{first.order_book_id}] 为空")


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = _parse_args(parser, argv)
    try:
        source = _make_source(args, parser)
    except ImportError as e:
        print(f"错误:{e}", file=sys.stderr)
        return 2
    out = build_bundle([source], Path(args.out), benchmark_oid=args.benchmark)

    print(f"bundle 构建完成:{out}")
    files = sorted(p.name for p in out.iterdir() if p.is_file())
    for name in files:
        size_kb = (out / name).stat().st_size / 1024
        print(f"  - {name}({size_kb:.1f} KB)")
    _self_check(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
