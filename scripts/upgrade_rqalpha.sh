#!/usr/bin/env bash
# 无缝升级 RQAlpha:隔离验证 → 契约门禁 → 全量回归 → 锁文件 → (可选)应用到主环境
# 用法:
#   scripts/upgrade_rqalpha.sh                 # 解析 PyPI 最新版,只在隔离环境验证
#   scripts/upgrade_rqalpha.sh 6.6.0           # 指定版本验证
#   scripts/upgrade_rqalpha.sh 6.6.0 --apply   # 验证通过后安装到当前 .venv
set -euo pipefail
cd "$(dirname "$0")/.."

TARGET="${1:-latest}"
APPLY="${2:-}"
PY="${PYTHON:-python3}"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/aquant-rqalpha.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

if [ "$APPLY" = "--apply" ] && [ -f requirements.lock.txt ] && [ ! -f requirements.lock.txt.bak ]; then
    cp requirements.lock.txt requirements.lock.txt.bak
    echo "旧锁文件已备份为 requirements.lock.txt.bak"
fi

echo "[1/5] 创建隔离验证环境: $WORK"
"$PY" -m venv "$WORK/venv"
VENV_PY="$WORK/venv/bin/python"
"$VENV_PY" -m pip install -q --upgrade pip

if [ "$TARGET" = "latest" ]; then
    TARGET="$("$VENV_PY" -m pip index versions rqalpha 2>/dev/null \
        | sed -n 's/^rqalpha (\([^)]*\)).*/\1/p' | head -n1 || true)"
    if [ -z "$TARGET" ]; then
        echo "无法自动解析最新版,请显式传入版本号: $0 <version> [--apply]"
        exit 1
    fi
fi
echo "目标 RQAlpha 版本: $TARGET"

echo "[2/5] 安装依赖(隔离环境,不触碰当前 .venv)"
"$VENV_PY" -m pip install -q -e ".[dev]" "rqalpha==$TARGET"

echo "[3/5] 版本闸门:确认兼容层能识别该版本"
AQUANT_RQALPHA_ALLOW=1 "$VENV_PY" -m aquant.compat --report

echo "[4/5] 契约快照 + 关键回归(失败即停止)"
AQUANT_RQALPHA_ALLOW=1 "$VENV_PY" -m pytest -q \
    tests/contracts tests/validation/test_config_rules.py \
    tests/data/test_bundle.py tests/test_e2e.py

echo "[5/5] 全量回归"
AQUANT_RQALPHA_ALLOW=1 "$VENV_PY" -m pytest -q tests/

"$VENV_PY" -m pip freeze | grep -v '^-e ' > requirements.lock.txt
echo "锁文件已写入 requirements.lock.txt (rqalpha==$TARGET)"

if [ "$APPLY" = "--apply" ]; then
    if [ ! -x .venv/bin/python ]; then
        echo "未找到 .venv/bin/python,请先按 README 创建主环境"
        exit 1
    fi
    .venv/bin/python -m pip install -q -r requirements.lock.txt
    .venv/bin/python -m pip install -q -e . --no-deps
    echo "已应用到主环境。回滚命令:"
    echo "  .venv/bin/python -m pip install -r requirements.lock.txt.bak"
    echo "  .venv/bin/python -m pip install -e . --no-deps"
else
    echo "验证通过。应用到主环境请执行:"
    echo "  .venv/bin/python -m pip install -r requirements.lock.txt"
    echo "  .venv/bin/python -m pip install -e . --no-deps"
fi
echo "版本变更后必须重建 bundle(新旧并存),再用 AQUANT_BUNDLE_PATH 切换并对比关键指标。"
