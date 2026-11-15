"""平台 CLI:python -m aquant <command>

- validate: 只做 Layer 1 校验(AST + 配置 lint),输出 findings
- backtest: 校验 + 单次回测,输出核心指标
- verify  : 分层验证(IS→OOS 对比 + 模拟盘清单)
- serve   : 启动 FastAPI(Agent 平台入口)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from aquant.config.schema import build_run_config
from aquant.runner.backtest import (
    BacktestExecutionError,
    DEFAULT_BUNDLE_PATH,
    StrategyValidationError,
    key_metrics,
    run_backtest,
)
from aquant.validation.findings import Verdict
from aquant.validation.validator import validate_strategy
from aquant.verify.pipeline import layered_verification


def _read_code(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def _load_overrides(spec: str | None) -> dict | None:
    """--overrides 支持:内联 JSON 字符串 或 JSON 文件路径。"""
    if not spec:
        return None
    text = spec.strip()
    if text.startswith("{"):
        return json.loads(text)
    return json.loads(Path(text).read_text(encoding="utf-8"))


def _print_verdict(verdict: "Verdict") -> None:
    click.echo(f"校验结果:{'✗ 存在阻断性错误' if verdict.has_error else '✓ 通过'}"
               f"(error={verdict.error_count}, warning={verdict.warning_count})")
    for f in verdict.findings:
        mark = {"error": "✗", "warning": "⚠", "info": "ℹ"}[f.severity.value]
        click.echo(f"  [{mark}][{f.rule.value}] {f.message}")


@click.group()
def main() -> None:
    """AQ-Platform:A 股回测正确性平台(RQAlpha + 四层校验)。"""


@main.command()
@click.argument("strategy_path")
@click.option("--start", default="2024-01-01", show_default=True)
@click.option("--end", default="2024-12-31", show_default=True)
@click.option("--overrides", default=None, help="用户 mod 覆盖(JSON 文件路径或内联 JSON)")
def validate(strategy_path: str, start: str, end: str, overrides: str | None) -> None:
    """Layer 1 校验:AST 未来函数检查 + 配置保守性 lint。"""
    code = _read_code(strategy_path)
    config = build_run_config(start, end, user_mod_overrides=_load_overrides(overrides))
    verdict = validate_strategy(code, config)
    _print_verdict(verdict)
    sys.exit(1 if verdict.has_error else 0)


@main.command()
@click.argument("strategy_path")
@click.option("--start", required=True)
@click.option("--end", required=True)
@click.option("--bundle", default=None, help="RQAlpha bundle 目录(默认 ~/.aquant/bundle)")
@click.option("--cash", type=float, default=None)
@click.option("--benchmark", default=None, help="基准指数 order_book_id(如 000300.XSHG)")
@click.option("--overrides", default=None, help="用户 mod 覆盖(JSON)")
def backtest(strategy_path: str, start: str, end: str, bundle: str | None, cash: float | None,
             benchmark: str | None, overrides: str | None) -> None:
    """Layer 1 校验通过后执行单次回测。"""
    code = _read_code(strategy_path)
    try:
        result = run_backtest(
            code, start, end, bundle_path=bundle or DEFAULT_BUNDLE_PATH,
            cash=cash, benchmark=benchmark, mod_overrides=_load_overrides(overrides),
        )
    except StrategyValidationError as e:
        click.secho(str(e), fg="red")
        sys.exit(1)
    except BacktestExecutionError as e:
        click.secho(str(e), fg="red")
        sys.exit(2)
    _print_verdict(result.verdict)
    click.echo(json.dumps(key_metrics(result), ensure_ascii=False, indent=2, default=str))


@main.command()
@click.argument("strategy_path")
@click.option("--start", required=True)
@click.option("--end", required=True)
@click.option("--bundle", default=None)
@click.option("--cash", type=float, default=None)
@click.option("--benchmark", default=None)
@click.option("--is-ratio", type=float, default=0.7, show_default=True, help="样本内占比")
@click.option("--out", default=None, help="报告输出文件(默认打印)")
def verify(strategy_path: str, start: str, end: str, bundle: str | None, cash: float | None,
           benchmark: str | None, is_ratio: float, out: str | None) -> None:
    """Layer 4 分层验证:样本内 → 样本外 → 模拟盘清单。"""
    code = _read_code(strategy_path)
    try:
        lv = layered_verification(
            code, start, end, bundle_path=bundle or DEFAULT_BUNDLE_PATH,
            is_ratio=is_ratio, cash=cash, benchmark=benchmark,
        )
    except StrategyValidationError as e:
        click.secho(str(e), fg="red")
        sys.exit(1)
    report = lv.to_report()
    if out:
        Path(out).write_text(report, encoding="utf-8")
        click.echo(f"报告已写入 {out}")
    else:
        click.echo(report)


@main.command()
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", type=int, default=8000, show_default=True)
def serve(host: str, port: int) -> None:
    """启动 Agent 平台 API(FastAPI)。"""
    import uvicorn

    uvicorn.run("aquant.api.main:app", host=host, port=port)


if __name__ == "__main__":
    main()
