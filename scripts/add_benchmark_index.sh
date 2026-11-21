#!/usr/bin/env bash
# 向既有 RQAlpha bundle 注入真实 A 股基准指数日线(Baostock)。
# 用法:
#   scripts/add_benchmark_index.sh [bundle_path] [指数oid列表(逗号分隔)]
# 示例:
#   scripts/add_benchmark_index.sh ~/.aquant/bundle-baostock 000001.XSHG,000300.XSHG
# 产物:覆盖 indexes.h5 对应 dataset、注册 INDX 合约、写 benchmark_meta.json。
set -euo pipefail
cd "$(dirname "$0")/.."

BUNDLE="${1:-${AQUANT_BUNDLE_PATH:-$HOME/.aquant/bundle-baostock}}"
INDEXES="${2:-000001.XSHG,000300.XSHG}"

if [ ! -f "$BUNDLE/indexes.h5" ]; then
    echo "bundle 不存在或缺少 indexes.h5: $BUNDLE" >&2
    exit 1
fi

echo "注入真实基准指数 → $BUNDLE ($INDEXES)"
exec .venv/bin/python -m aquant.data.benchmark --bundle "$BUNDLE" --indexes "$INDEXES"
