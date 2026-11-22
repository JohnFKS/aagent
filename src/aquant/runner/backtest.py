"""回测执行器:RQAlpha 6.5.0 的安全封装。

执行流程(硬门禁语义):
1. 用 Layer 3 默认参数构造 run 配置(用户覆盖原样并入);
2. **先过 Layer 1 校验**(AST + 配置 lint),含 error 一律拒绝执行;
3. 在**子进程**中调用 rqalpha.run_file(策略代码永不进入平台进程);
4. 解析 sys_analyser 的结果文件为 BacktestResult。

豁免机制:AQUANT_ALLOW_UNSAFE=1 时跳过 error 拦截,结果打 unsafe 标记,
仅供研究对比,平台 API/报告会显著标注。
"""

from __future__ import annotations

import hashlib
import json
import os
import pickle
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from aquant.config.schema import build_run_config, default_bundle_path
from aquant.runner import _worker
from aquant.validation.findings import Finding, RuleId, Severity, Verdict
from aquant.validation.validator import validate_strategy

# 向后兼容的旧名(实现收敛到 config.default_bundle_path,口径唯一)
DEFAULT_BUNDLE_PATH = default_bundle_path()


class StrategyValidationError(RuntimeError):
    """Layer 1 校验未通过,回测被拒绝。"""

    def __init__(self, verdict: Verdict) -> None:
        self.verdict = verdict
        super().__init__("策略校验未通过,拒绝执行回测:\n" + "\n".join(verdict.error_messages()))


class BacktestExecutionError(RuntimeError):
    """回测子进程失败。"""

    def __init__(self, returncode: int, stderr_tail: str) -> None:
        super().__init__(f"回测执行失败(exit={returncode}):\n{stderr_tail}")
        self.returncode = returncode


class BacktestCancelled(RuntimeError):
    """用户主动终止回测(后台任务取消)。"""


@dataclass
class BacktestResult:
    summary: dict
    portfolio: pd.DataFrame
    trades: list
    verdict: Verdict
    start: str
    end: str
    unsafe: bool = False
    elapsed_seconds: float = 0.0
    config_used: dict = field(default_factory=dict)
    config_fingerprint: str = ""   # 运行配置哈希(可复现性审计用)
    strategy_fingerprint: str = ""  # 策略代码哈希(同一指纹 ⇔ 同一代码)
    # 基准净值序列(sys_analyser 配置 base.benchmark 时才有;未配置为空表)
    benchmark_portfolio: pd.DataFrame = field(default_factory=pd.DataFrame)
    # 平台运行日志(聚宽式逐日:初始化/开盘前/成交/撤单/收盘结算)+ 策略 print
    run_log: list[str] = field(default_factory=list)
    strategy_prints: list[str] = field(default_factory=list)
    run_log_truncated: bool = False
    # 结构化每日持仓&收益(每日持仓/可用/收盘价/市值/盈亏 + 现金/总资产/当日盈亏)
    daily_holdings: list[dict] = field(default_factory=list)


def run_backtest(
    code: str,
    start: str,
    end: str,
    bundle_path: str | Path | None = None,
    *,
    cash: float | None = None,
    benchmark: str | None = None,
    mod_overrides: dict | None = None,
    strategy_name: str = "platform_strategy",
    timeout: int = 600,
    cancel_event: threading.Event | None = None,
    progress_id: str | None = None,
) -> BacktestResult:
    """校验并执行一次日线回测。

    :param code: 策略源码(RQAlpha 策略,含 init/handle_bar)
    :param start/end: 'YYYY-MM-DD'
    :param bundle_path: RQAlpha bundle 目录(aquant.data.bundle.build_bundle 产物)
    :param mod_overrides: 用户 mod 覆盖(激进值会被 Layer 1 拦截)
    :param progress_id: 进度追踪 id;None 时读当前 ContextVar(由 API 的 X-Progress-Id 注入),
        仍为 None 表示不追踪。追踪时 worker 实时写事件文件,主进程汇总到 registry。
    """
    t0 = time.time()
    from aquant.runner.progress import (
        ProgressReader, current_progress_id, get_registry,
    )

    pid = progress_id or current_progress_id()
    registry = get_registry()
    state = registry.start(pid) if pid else None
    bundle_path = Path(bundle_path or default_bundle_path())
    if not bundle_path.exists():
        raise FileNotFoundError(
            f"数据 bundle 不存在:{bundle_path}。请先构建:python -m aquant.data.build_bundle --help"
        )

    config = build_run_config(start, end, user_mod_overrides=mod_overrides, cash=cash, benchmark=benchmark)
    config["base"]["data_bundle_path"] = str(bundle_path)
    # 可复现性指纹:同一 config 哈希 ⇒ 同一数据口径与成本假设(数据 bundle 内容
    # 由 Layer 2 构建器版本管理,策略代码指纹另行记录)
    config_fingerprint = hashlib.sha256(
        json.dumps(config, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]

    if state:
        state.add("stage", "策略静态校验", data={"stage": "validating", "label": "校验策略代码", "percent": 2})
    verdict = validate_strategy(code, config)
    unsafe_env = os.environ.get("AQUANT_ALLOW_UNSAFE") == "1"
    if verdict.has_error and not unsafe_env:
        if state:
            registry.finish(pid, "failed", "策略校验未通过")
        raise StrategyValidationError(verdict)
    unsafe = verdict.has_error and unsafe_env
    if state:
        state.add("stage", "校验通过,准备数据", data={"stage": "validated", "label": "校验通过", "percent": 5})

    # 区间内交易日总数(供 mod 计算进度百分比);读取失败不影响回测
    total_days: int | None = None
    try:
        import numpy as np

        cal = np.load(bundle_path / "trading_dates.npy")
        s, e = int(start.replace("-", "")), int(end.replace("-", ""))
        total_days = int(((cal >= s) & (cal <= e)).sum()) or None
    except Exception:
        pass

    with tempfile.TemporaryDirectory(prefix="aquant-run-") as tmp:
        tmp_dir = Path(tmp)
        strategy_file = tmp_dir / f"{strategy_name}.py"
        strategy_file.write_text(code, encoding="utf-8")
        result_file = tmp_dir / "result.pkl"
        config["mod"]["sys_analyser"]["output_file"] = str(result_file)

        # 平台日志 mod(聚宽式逐日日志 + 结构化每日持仓):写入临时文件,
        # worker 收尾经哨兵行回传主进程
        log_file = tmp_dir / "run_log.txt"
        holdings_file = tmp_dir / "daily_holdings.json"
        progress_file = tmp_dir / "progress.jsonl" if state else None
        platform_log_cfg = {
            "enabled": True,
            "lib": "aquant.runner.platform_log_mod",
            "log_file": str(log_file),
            "holdings_file": str(holdings_file),
        }
        if progress_file:
            platform_log_cfg["progress_file"] = str(progress_file)
            if total_days:
                platform_log_cfg["total_trading_days"] = total_days
        config.setdefault("mod", {})["platform_log"] = platform_log_cfg
        if progress_file:
            config.setdefault("runner", {})["progress_file"] = str(progress_file)
        cfg_file = tmp_dir / "config.json"
        cfg_file.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

        cmd = [sys.executable, "-m", "aquant.runner._worker", str(strategy_file), str(cfg_file)]
        # stdout/stderr 写临时文件而非 PIPE:轮询期间不读管道,worker 日志量大时
        # 管道缓冲区满会令其阻塞在 pipe_write(表现为回测永不结束)。临时文件无此问题。
        stdout_file = tmp_dir / "worker_stdout.txt"
        stderr_file = tmp_dir / "worker_stderr.txt"
        reader = ProgressReader(progress_file)
        with open(stdout_file, "wb") as out_f, open(stderr_file, "wb") as err_f:
            proc = subprocess.Popen(cmd, stdout=out_f, stderr=err_f)
            try:
                deadline = time.monotonic() + timeout
                while proc.poll() is None:
                    # 汇总 worker 实时写入的进度/日志事件
                    if state:
                        for ev in reader.read_new():
                            state.add(
                                ev.get("type", "log"), ev.get("message", ""),
                                level=ev.get("level", "INFO"), data=ev.get("data"),
                                ts=ev.get("ts"),
                            )
                    if cancel_event is not None and cancel_event.is_set():
                        proc.terminate()
                        try:
                            proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            proc.wait()
                        if state:
                            registry.finish(pid, "cancelled", "用户终止了回测")
                        raise BacktestCancelled("用户终止了回测")
                    if time.monotonic() >= deadline:
                        proc.kill()
                        proc.wait()
                        if state:
                            registry.finish(pid, "failed", f"回测超时({timeout}s)")
                        raise subprocess.TimeoutExpired(cmd, timeout)
                    time.sleep(0.05)
            except BaseException:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                if state and state.status == "running":
                    registry.finish(pid, "failed", "回测进程异常退出")
                raise
        # 读取进程退出前写入的最后一批事件(完成/错误阶段)
        if state:
            for ev in reader.read_new():
                state.add(
                    ev.get("type", "log"), ev.get("message", ""),
                    level=ev.get("level", "INFO"), data=ev.get("data"),
                    ts=ev.get("ts"),
                )
        stdout = stdout_file.read_text(encoding="utf-8", errors="replace")
        stderr = stderr_file.read_text(encoding="utf-8", errors="replace")
        if proc.returncode != 0 or not result_file.exists():
            stderr_tail = "\n".join((stderr or "").strip().splitlines()[-25:])
            if state:
                registry.finish(pid, "failed", stderr_tail[-500:] or "回测进程失败")
            raise BacktestExecutionError(proc.returncode, stderr_tail)
        if state:
            state.add("stage", "进度汇总完成", data={"stage": "done", "label": "回测完成", "percent": 100})
            registry.finish(pid, "completed")

        with open(result_file, "rb") as f:
            raw = pickle.load(f)

        daily_holdings = []
        for line in (stdout or "").splitlines():
            if line.startswith(_worker.RUN_META_SENTINEL):
                try:
                    meta = json.loads(line[len(_worker.RUN_META_SENTINEL):])
                    run_log = meta.get("run_log") or []
                    strategy_prints = meta.get("strategy_prints") or []
                    daily_holdings = meta.get("daily_holdings") or []
                except Exception:
                    pass
                break
        run_log_truncated = len(run_log) > 2000 or len(strategy_prints) > 500
        run_log = run_log[-2000:]
        strategy_prints = strategy_prints[-500:]
        daily_holdings = daily_holdings[-1000:]

    summary = dict(raw.get("summary", {}))
    portfolio = _normalize_portfolio(raw.get("portfolio"))
    # 基准净值(sys_analyser 配置 base.benchmark 时输出 benchmark_portfolio:
    # index=date, column=unit_net_value);未配置时保持空表
    benchmark_portfolio = _normalize_portfolio(raw.get("benchmark_portfolio"))
    trades = raw.get("trades", [])

    if unsafe:
        summary["unsafe_run"] = True
        verdict = Verdict(
            verdict.findings
            + (
                Finding(
                    RuleId.INJECTED_DEFAULT,
                    Severity.INFO,
                    "本次运行带 unsafe 标记:校验存在 error 但被 AQUANT_ALLOW_UNSAFE 豁免,"
                    "结果仅供研究对比,严禁用于任何实盘决策。",
                ),
            )
        )
    return BacktestResult(
        summary=summary,
        portfolio=portfolio,
        trades=trades,
        verdict=verdict,
        start=start,
        end=end,
        unsafe=unsafe,
        elapsed_seconds=round(time.time() - t0, 2),
        config_used=config,
        config_fingerprint=config_fingerprint,
        strategy_fingerprint=hashlib.sha256(code.encode("utf-8")).hexdigest()[:16],
        benchmark_portfolio=benchmark_portfolio,
        run_log=run_log,
        strategy_prints=strategy_prints,
        run_log_truncated=run_log_truncated,
        daily_holdings=daily_holdings,
    )


def _normalize_portfolio(raw: object) -> pd.DataFrame:
    if isinstance(raw, pd.DataFrame):
        return raw
    if isinstance(raw, list):
        df = pd.DataFrame(raw)
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"].astype(str))
            df = df.set_index("date")
        return df
    return pd.DataFrame()


def key_metrics(result: BacktestResult) -> dict:
    """提取报告常用的核心指标(取自 sys_analyser summary,口径与 RQAlpha 一致)。

    rqalpha 6.5.0 的 summary 不含 total_commission/total_tax/trading_days,
    这三项从成交明细(trades:DataFrame 或 Trade 对象列表)与净值序列推导,
    新旧版本兼容。
    """
    s = result.summary

    def _sum_cost(attr: str, key: str) -> float:
        v = s.get(key)
        if v is not None:
            return float(v)
        if isinstance(result.trades, pd.DataFrame):
            if attr in result.trades.columns:
                return round(float(result.trades[attr].sum()), 2)
            if "transaction_cost" in result.trades.columns:
                return round(float(result.trades["transaction_cost"].sum()), 2)
            return 0.0
        return round(sum(float(getattr(t, attr, 0) or 0) for t in result.trades), 2)

    trading_days = s.get("trading_days")
    if trading_days is None:
        trading_days = len(result.portfolio)
    return {
        "start": result.start,
        "end": result.end,
        "total_returns": s.get("total_returns"),
        "annualized_returns": s.get("annualized_returns"),
        "benchmark_total_returns": s.get("benchmark_total_returns"),
        "sharpe": s.get("sharpe"),
        "max_drawdown": s.get("max_drawdown"),
        "volatility": s.get("volatility"),
        "alpha": s.get("alpha"),
        "beta": s.get("beta"),
        "turnover": s.get("turnover"),
        "win_rate": s.get("win_rate"),
        "total_commission": _sum_cost("commission", "total_commission"),
        "total_tax": _sum_cost("tax", "total_tax"),
        "trading_days": trading_days,
        "unsafe": result.unsafe,
        "elapsed_seconds": result.elapsed_seconds,
    }
