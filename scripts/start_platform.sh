#!/usr/bin/env bash
# 启动完整平台:前端(API 托管 dist)+ 后端 API + 真实数据 bundle
# 用法: scripts/start_platform.sh [port] [bundle_path]
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${1:-8000}"
BUNDLE="${2:-$HOME/.aquant/bundle-real}"

if [ ! -f "$BUNDLE/instruments.pk" ]; then
    echo "bundle 不存在: $BUNDLE"
    echo "先用合成数据快速起步: .venv/bin/python -m aquant.data.build_bundle --source synthetic --out $HOME/.aquant/bundle"
    exit 1
fi

if [ ! -f frontend/dist/index.html ]; then
    echo "前端未构建,先执行: cd frontend && pnpm install && pnpm build"
    exit 1
fi

export AQUANT_BUNDLE_PATH="$BUNDLE"
# 加载 .env(LLM 凭据等;密钥不入源码与日志)
if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi
echo "启动 AQ-Platform @ http://127.0.0.1:$PORT (bundle: $BUNDLE, LLM: ${AQUANT_LLM_MODEL:-deterministic-fallback})"
exec .venv/bin/python -m uvicorn aquant.api.main:app --host 127.0.0.1 --port "$PORT"
