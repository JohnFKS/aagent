"""Layer 2:bundle 构建 CLI。

用法::

    python -m aquant.data.build_bundle --source baostock --out ~/.aquant/bundle \
        --codes 000001,600519 --start 2021-01-01 --end 2026-11-15
    python -m aquant.data.build_bundle --source akshare --out ... --codes 000001,600519
    python -m aquant.data.build_bundle --source synthetic --out ... --n 5

数据源(``--source`` 缺省为 ``baostock`` 主力):
- ``baostock`` → BaostockSource:主力日线,真实价 + 后复权因子,自带停牌/ST 字段;
- ``akshare``  → AkshareSource:AKShare A 股日线(东财→新浪降级);
- ``etf``      → EtfSource:AKShare ETF 双源自动降级;
- ``synthetic``→ SyntheticSource:确定性合成数据(测试/演示)。

真实源默认开启跨源价格交叉校验:raw/hfq close 超差时按覆盖度评分自动切换到
数据最全的源整段回测(不混源),报告写入 bundle/cross_check.json。
``--no-cross-check`` 可关闭(不推荐),``--cross-check-tolerance`` 调整容差。
K 线默认写入 DuckDB 缓存(~/.aquant/cache/duckdb),``--cache-max-mb`` 控制单源上限,
``--cache-retention-days`` 控制保留期,超出时淘汰最久未用标的。

构建完成后打印产物清单并自检:用 rqalpha  storages 真实加载 instruments 与
stocks.h5 / funds.h5 首行,确保 bundle 可被 BaseDataSource 直接消费。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from aquant.data.bundle import build_bundle

_SOURCES = ("baostock", "akshare", "etf", "synthetic")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m aquant.data.build_bundle",
        description="构建 RQAlpha 6.5.0 数据 bundle(默认 baostock 主力 + AKShare 交叉校验)",
    )
    parser.add_argument("--source", choices=_SOURCES, default="baostock",
                        help="数据源(默认 baostock):baostock=主力日线;akshare=对比源;etf=ETF;synthetic=合成")
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
    parser.add_argument("--no-cross-check", action="store_true",
                        help="关闭回测前跨源价格交叉校验(不推荐)")
    parser.add_argument("--cross-check-tolerance", type=float, default=0.005,
                        help="跨源 raw/hfq close 最大相对偏差容差(默认 0.005)")
    parser.add_argument("--cache", choices=("duckdb", "parquet", "none"), default="duckdb",
                        help="K线本地缓存(默认 duckdb):duckdb=容量可控;parquet=旧版;none=关闭")
    parser.add_argument("--cache-dir", default=None,
                        help="缓存根目录(默认 ~/.aquant/cache)")
    parser.add_argument("--cache-max-mb", type=float, default=512.0,
                        help="[duckdb] 单源缓存上限 MB,超出淘汰最久未用标的(默认 512)")
    parser.add_argument("--cache-retention-days", type=int, default=None,
                        help="[duckdb] 仅保留最近 N 天日线(默认不过期)")
    return parser


def _parse_args(parser: argparse.ArgumentParser, argv: list[str] | None) -> argparse.Namespace:
    return parser.parse_args(argv)


def _make_sources(args: argparse.Namespace, parser: argparse.ArgumentParser):
    """返回 (sources 字典, primary 名称)。真实源会同时构造对比源供交叉校验。"""
    codes = [c.strip() for c in args.codes.split(",") if c.strip()] if args.codes else None
    if args.source == "synthetic":
        from aquant.data.akshare_source import AkshareSource
        from aquant.data.synthetic_source import SyntheticSource
        if codes is not None:
            codes = [AkshareSource._normalize_code(c) if "." not in c else c for c in codes]
        if codes is not None and len(codes) < args.n:
            codes = None
        return {"synthetic": SyntheticSource(
            n_instruments=args.n, start=args.start, end=args.end,
            seed=args.seed, codes=codes,
        )}, "synthetic"

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
        return {"etf": EtfSource(codes=codes, start=args.start, end=args.end, names=names)}, "etf"

    if not codes:
        parser.error(f"--source {args.source} 需要 --codes 000001,600519")

    sources: dict = {}
    if args.source == "baostock":
        try:
            from aquant.data.baostock_source import BaostockSource
            sources["baostock"] = BaostockSource(codes, args.start, args.end)
        except ImportError as exc:
            parser.error(str(exc))
        try:
            from aquant.data.akshare_source import AkshareSource
            sources["akshare"] = AkshareSource(codes, args.start, args.end)
        except ImportError:
            print("[cross-check] AKShare 未安装,跳过对比源", file=sys.stderr)
        return sources, "baostock"

    from aquant.data.akshare_source import AkshareSource
    sources["akshare"] = AkshareSource(codes, args.start, args.end)
    try:
        from aquant.data.baostock_source import BaostockSource
        sources["baostock"] = BaostockSource(codes, args.start, args.end)
    except ImportError:
        print("[cross-check] Baostock 未安装,跳过对比源", file=sys.stderr)
    return sources, "akshare"


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
    import json
    from aquant.data.cross_check import choose_source

    parser = _build_parser()
    args = _parse_args(parser, argv)
    try:
        sources, primary = _make_sources(args, parser)
    except ImportError as e:
        print(f"错误:{e}", file=sys.stderr)
        return 2

    chosen, report = primary, None
    if not args.no_cross_check and len(sources) > 1:
        check_codes = getattr(sources[primary], "oids", None) or getattr(sources[primary], "_oids", [])
        chosen, report = choose_source(
            sources, list(check_codes),
            primary=primary, tolerance=args.cross_check_tolerance,
        )
        print(f"[cross-check] {report.reason}", file=sys.stderr)

    selected = sources[chosen]
    if args.cache != "none":
        cache_dir = Path(args.cache_dir) if args.cache_dir else Path.home() / ".aquant" / "cache"
        key = f"{chosen}_{args.codes or 'all'}_{args.start}_{args.end}"
        if args.cache == "duckdb":
            from aquant.data.duckdb_cache import DuckDBCache
            selected = DuckDBCache(
                selected, cache_dir / "duckdb", key,
                max_size_mb=args.cache_max_mb, retention_days=args.cache_retention_days,
            )
        else:
            from aquant.data.cache import wrap as wrap_parquet
            selected = wrap_parquet(selected, cache_dir / "parquet" / key)
        print(f"[cache] {args.cache} 已启用", file=sys.stderr)

    out = build_bundle([selected], Path(args.out), benchmark_oid=args.benchmark)
    if hasattr(selected, "stats"):
        print(f"[cache] {selected.stats()}", file=sys.stderr)
    if hasattr(selected, "close"):
        selected.close()
    if report is not None:
        (out / "cross_check.json").write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    print(f"bundle 构建完成:{out}")
    files = sorted(p.name for p in out.iterdir() if p.is_file())
    for name in files:
        size_kb = (out / name).stat().st_size / 1024
        print(f"  - {name}({size_kb:.1f} KB)")
    _self_check(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
