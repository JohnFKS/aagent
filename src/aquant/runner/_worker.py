"""回测子进程 worker:仅做一件事——在独立进程中执行 rqalpha.run_file。

用法:python -m aquant.runner._worker <strategy_file> <config_json>

平台主进程永不 import 用户策略代码;本模块是唯一加载用户代码的位置,
崩溃/超时都隔离在子进程内。
"""

from __future__ import annotations

import json
import sys

from aquant.compat import check_rqalpha_version


def main() -> None:
    if len(sys.argv) != 3:
        print("usage: python -m aquant.runner._worker <strategy_file> <config_json>", file=sys.stderr)
        raise SystemExit(2)
    strategy_file, config_file = sys.argv[1], sys.argv[2]
    with open(config_file, encoding="utf-8") as f:
        config = json.load(f)

    check_rqalpha_version()  # 版本不受支持时先失败,避免进入撮合后才崩

    from rqalpha import run_file

    run_file(strategy_file, config)


if __name__ == "__main__":
    main()
