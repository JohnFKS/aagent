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
from aquant.runner.progress import ProgressWriter

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

    # 实时进度通道:进度事件文件路径由 run_backtest 注入(未启用时 writer 静默)
    progress_file = (config.get("runner", {}) or {}).get("progress_file")
    progress = ProgressWriter(progress_file)
    progress.emit("stage", "回测子进程启动", data={"stage": "starting", "label": "启动回测引擎", "percent": 0})

    check_rqalpha_version()  # 版本不受支持时先失败,避免进入撮合后才崩
    progress.emit("stage", "版本校验通过", data={"stage": "initializing", "label": "初始化回测环境", "percent": 5})

    # ---- 平台提速(结果恒一致):plot=False 时桩化 matplotlib --------------
    # rqalpha_mod_sys_analyser/mod.py 顶层 import .plot.consts → matplotlib 全链
    # (0.66s/次),而平台强制 plot=False 且策略 import 白名单禁止 matplotlib,
    # 此时以轻量桩替代,跳过重导入;若启用绘图(或环境开关)则走真实 matplotlib。
    if (os.environ.get("AQUANT_DISABLE_MPL_STUB") != "1"
            and not (config.get("mod", {}).get("sys_analyser", {}) or {}).get("plot")
            and "matplotlib" not in sys.modules):
        try:
            import importlib.abc
            import importlib.machinery
            import types

            class _MplStub:
                """惰性桩对象:任意属性/调用返回新桩(ploy: plt.style.use(...) 链可用)。"""

                def __init__(self, name: str) -> None:
                    self._name = name

                def __getattr__(self, attr: str):
                    if attr.startswith("__"):
                        raise AttributeError(attr)
                    return _MplStub(f"{self._name}.{attr}")

                def __call__(self, *a, **k):
                    return _MplStub(f"{self._name}()")

                def __iter__(self):
                    return iter(())

                def __repr__(self):
                    return f"<mpl-stub {self._name}>"

            class _RCParams(dict):
                """dict 桩:支持 consts.py 的项赋值/读取与属性式访问。"""

                def __getattr__(self, attr):
                    return _MplStub(f"rcParams.{attr}")

            class _MplStubLoader(importlib.abc.Loader):
                def create_module(self, spec):
                    mod = types.ModuleType(spec.name)
                    mod.__path__ = []
                    mod.rcParams = _RCParams({
                        "font.family": ["sans-serif"],
                        "font.sans-serif": [],
                        "axes.unicode_minus": True,
                    })
                    mod.__dict__["__getattr__"] = lambda attr: _MplStub(f"{spec.name}.{attr}")
                    return mod

                def exec_module(self, module) -> None:
                    return None

            class _MplStubFinder(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if fullname == "matplotlib" or fullname.startswith("matplotlib."):
                        return importlib.machinery.ModuleSpec(fullname, _MplStubLoader())
                    return None

            sys.meta_path.insert(0, _MplStubFinder())
        except Exception:
            pass  # 桩化失败退化为真实导入

    from rqalpha import run_file

    # ---- 平台提速(结果恒一致):history_bars 底层缓存 --------------------
    # BaseDataSource._all_day_bars_of 每次 get_bars 都重开 h5 全量读取;
    # bundle 在回测运行期不可变(auto-extend 原子换目录发生在 worker 启动前),
    # 因此按 (self, instrument) 做 lru 缓存结果恒等。CS 类本就走已缓存的
    # _filtered_day_bars;此缓存修复 ETF 等非 CS 类型的重读热路径。
    # 与 rqalpha 自身对 _filtered_day_bars 的 @lru_cache(None) 同一模式;
    # 安全性:下游(adjust_bars/字段提取)均返回新数组,不改写缓存内容。
    if os.environ.get("AQUANT_DISABLE_BAR_CACHE") != "1":
        try:
            from rqalpha.data.base_data_source.data_source import BaseDataSource
            from rqalpha.utils.functools import lru_cache as _rq_lru

            BaseDataSource._all_day_bars_of = _rq_lru(None)(BaseDataSource._all_day_bars_of)
        except Exception:
            pass  # 缓存装配失败不阻塞回测(退化为原直读行为)

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
            line = f"{t} - {record.level_name} - {record.message}"
            user_lines.append(line)
            # 策略 print / logger 实时推送(前端日志面板可即时看到)
            progress.emit("log", line, level=record.level_name.upper())

    _orig_init_logger = rq_logger.init_logger

    def _init_logger_with_capture() -> None:
        _orig_init_logger()
        handler = _UserLogCapture()
        rq_logger.user_log.handlers.append(handler)
        rq_logger.user_system_log.handlers.append(handler)

    rq_logger.init_logger = _init_logger_with_capture

    try:
        progress.emit("stage", "开始逐日撮合", data={"stage": "running", "label": "逐日回测中", "percent": 10})
        run_file(strategy_file, config)
        progress.emit("stage", "回测完成", data={"stage": "finalizing", "label": "汇总回测结果", "percent": 95})
    except BaseException as exc:
        progress.emit("error", f"回测异常:{type(exc).__name__}: {exc}", level="ERROR")
        raise
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
