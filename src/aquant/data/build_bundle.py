"""Layer 2:bundle 构建 CLI。

用法::

    python -m aquant.data.build_bundle --source synthetic --out /tmp/aquant-bundle \
        [--n 5 --start 2022-01-01 --end 2024-12-31 --seed 42 \
         --codes 000001,600519 --benchmark 000001.XSHG]

- ``--source synthetic`` → SyntheticSource(确定性合成数据,测试/演示);
- ``--source akshare``   → AkshareSource(AKShare 真实数据,需 ``pip install 'aquant[akshare]'``);
- 构建完成后打印产物清单并自检:用 rqalpha 的 storages 真实加载
  instruments 列表与 stocks.h5 首行,确保 bundle 可被 BaseDataSource 直接消费。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from aquant.data.bundle import build_bundle


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m aquant.data.build_bundle",
        description="构建 RQAlpha 6.5.0 数据 bundle(synthetic/akshare 数据源)",
    )
    parser.add_argument("--source", choices=("synthetic", "akshare"), required=True,
                        help="数据源:synthetic=确定性合成;akshare=AKShare 真实数据(仅供研究)")
    parser.add_argument("--out", required=True, help="bundle 输出目录")
    parser.add_argument("--n", type=int, default=5, help="[synthetic] 合约数量(默认 5)")
    parser.add_argument("--start", default="2022-01-01", help="起始日 YYYY-MM-DD(默认 2022-01-01)")
    parser.add_argument("--end", default="2024-12-31", help="结束日 YYYY-MM-DD(默认 2024-12-31)")
    parser.add_argument("--seed", type=int, default=42, help="[synthetic] 随机种子(默认 42)")
    parser.add_argument("--codes", default=None,
                        help="逗号分隔六位代码,如 000001,600519(synthetic 可带后缀;akshare 必填)")
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
            # 裸六位代码补齐交易所后缀(6→XSHG,0/3→XSHE,4/8/92→BJ),与默认池格式一致
            codes = [AkshareSource._normalize_code(c) if "." not in c else c for c in codes]
        if codes is not None and len(codes) < args.n:
            codes = None  # codes 不足时让 SyntheticSource 用默认池,保持行为可预期
        return SyntheticSource(
            n_instruments=args.n, start=args.start, end=args.end,
            seed=args.seed, codes=codes,
        )
    from aquant.data.akshare_source import AkshareSource

    if not codes:
        parser.error("--source akshare 需要 --codes 000001,600519")
    return AkshareSource(codes=codes, start=args.start, end=args.end)


def _self_check(out: Path) -> None:
    """自检:用 rqalpha 的 storages 真实加载 instruments 与 stocks 首行。"""
    from rqalpha.data.base_data_source.storages import (
        FutureInfoStore,
        SecuritiesDayBarStore,
        load_instruments_from_pkl,
    )

    future_info = FutureInfoStore(str(out / "future_info.json"), {})
    instruments = load_instruments_from_pkl(str(out / "instruments.pk"), future_info)
    cs = [i for i in instruments if i.type == "CS"]
    print(f"[self-check] instruments 加载成功:共 {len(instruments)} 条"
          f"(CS {len(cs)} 条,其他 {len(instruments) - len(cs)} 条)")
    if cs:
        first = cs[0]
        print(f"[self-check] 首个 CS 合约:{first.order_book_id}({first.symbol}),"
              f"listed_date={first.listed_date:%Y-%m-%d}")
        store = SecuritiesDayBarStore(str(out / "stocks.h5"))
        bars = store.get_bars(first.order_book_id)
        if len(bars) > 0:
            row = bars[0]
            print(f"[self-check] stocks.h5 首行[{first.order_book_id}]:"
                  f"datetime={row['datetime']} open={row['open']:.4f} close={row['close']:.4f} "
                  f"limit_up={row['limit_up']:.4f} limit_down={row['limit_down']:.4f}")
        else:
            print(f"[self-check] stocks.h5[{first.order_book_id}] 为空")


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
