#!/usr/bin/env bash
# 回测准确性验证(CLI):闭式可对照策略,手算期望 vs 平台输出,精确到分。
# 用法:
#   scripts/verify_backtest_accuracy.sh [--bundle PATH] [--cash N] [--shares N] [--instrument OID]
set -euo pipefail
cd "$(dirname "$0")/.."

exec .venv/bin/python -m aquant.verify.accuracy_cli "$@"
