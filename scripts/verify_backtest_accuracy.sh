#!/usr/bin/env bash
# 回测准确性验证(CLI):闭式可对照策略,手算期望 vs 平台输出,精确到分。
# 用法:
#   scripts/verify_backtest_accuracy.sh [--bundle PATH] [--cash N] [--shares N]
# 退出码:全部通过 0;存在不一致 1(可用于 CI 门禁)。
set -euo pipefail
cd "$(dirname "$0")/.."

exec .venv/bin/python -m aquant.verify.accuracy_cli "$@"
