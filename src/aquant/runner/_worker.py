"""回测子进程 worker:仅做一件事——在独立进程中执行 rqalpha.run_file。

用法:python -m aquant.runner._worker <strategy_file> <config_json>

平台主进程永不 import 用户策略代码;本模块是唯一加载用户代码的位置,
崩溃/超时都隔离在子进程内。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys

from aquant.compat import check_rqalpha_version

# 哨兵行前缀:主进程 run_backtest 从 stdout 解析本行,拿回运行日志与策略 print
RUN_META_SENTINEL = "@@AQUANT_RUN_META@@"


def _emit_run_meta(log_lines: list[str], strategy_prints: list[str],
                   daily_holdings: list[dict] | None = None) -> None:
    """把运行日志 + 策略 print + 每日持仓以哨兵行回传主进程(单行 JSON)。"""
    meta = {
        "run_log": log_lines,
        "strategy_prints": strategy_prints,
    }
    if daily_holdings is not None:
        meta["daily_holdings"] = daily_holdings
    print(RUN_META_SENTINEL + json.dumps(meta, ensure_ascii=False), file=sys.__stdout__, flush=True)


def main() -> None:
    if len(sys.argv) != 3:
        print("usage: python -m aquant.runner._worker <strategy_file> <config_json>", file=sys.stderr)
        raise SystemExit(2)
    strategy_file, config_file = sys.argv[1], sys.argv[2]
    with open(config_file, encoding="utf-8") as f:
        config = json.load(f)

    check_rqalpha_version()  # 版本不受支持时先失败,避免进入撮合后才崩

    from rqalpha import run_file

    # 策略 print / logger 捕获:rqalpha 把策略 print 重定向到 user_log(logbook,
    # stderr),通过包一层 init_logger 追加捕获 handler;时间戳为回测交易时间
    import logbook

    from rqalpha.utils import logger as rq_logger

    user_lines: list[str] = []

    class _UserLogCapture(logbook.Handler):
        def __init__(self) -> None:
            super().__init__(bubble=True)

        def emit(self, record) -> None:
            t = record.time.strftime("%Y-%m-%d %H:%M:%S") if getattr(record, "time", None) else ""
            user_lines.append(f"{t} - {record.level_name} - {record.message}")

    _orig_init_logger = rq_logger.init_logger

    def _init_logger_with_capture() -> None:
        _orig_init_logger()
        handler = _UserLogCapture()
        rq_logger.user_log.handlers.append(handler)
        rq_logger.user_system_log.handlers.append(handler)

    rq_logger.init_logger = _init_logger_with_capture

    try:
        run_file(strategy_file, config)
    finally:
        captured = user_lines[-500:]
        # 平台日志 mod 的落盘产物(log_file/holdings_file 由 run_backtest 注入 mod 配置)
        log_lines: list[str] = []
        daily_holdings: list[dict] | None = None
        try:
            mod_cfg = (config.get("mod", {}).get("platform_log", {}) or {})
            log_file = mod_cfg.get("log_file")
            if log_file and os.path.exists(log_file):
                with open(log_file, encoding="utf-8") as f:
                    log_lines = [ln.rstrip("\n") for ln in f if ln.strip()]
            holdings_file = mod_cfg.get("holdings_file")
            if holdings_file and os.path.exists(holdings_file):
                with open(holdings_file, encoding="utf-8") as f:
                    daily_holdings = json.load(f)
        except Exception:
            pass
        _emit_run_meta(log_lines, captured[:500], daily_holdings)


if __name__ == "__main__":
    main()
