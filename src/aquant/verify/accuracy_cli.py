"""回测准确性验证 CLI:python -m aquant.verify.accuracy_cli [--bundle ...] [--cash ...]。

用法(或直接 scripts/verify_backtest_accuracy.sh):
    python -m aquant.verify.accuracy_cli --bundle ~/.aquant/bundle-baostock --shares 100
退出码:全部通过 0;存在不一致 1(可用于 CI)。
"""

from __future__ import annotations

import argparse
import sys

from aquant.verify.accuracy import run_accuracy_checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m aquant.verify.accuracy_cli",
        description="回测准确性验证:闭式可对照策略,手算期望 vs 平台输出(精确到分)",
    )
    parser.add_argument("--bundle", default=None, help="bundle 目录(缺省 AQUANT_BUNDLE_PATH 或 ~/.aquant/bundle)")
    parser.add_argument("--cash", type=float, default=1_000_000.0, help="初始资金(默认 100 万)")
    parser.add_argument("--shares", type=int, default=100, help="买卖股数(默认 100,A 股一手)")
    args = parser.parse_args(argv)

    result = run_accuracy_checks(bundle_path=args.bundle, cash=args.cash, shares=args.shares)
    print(result["report_md"])
    return 0 if result["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
