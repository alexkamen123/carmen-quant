#!/bin/bash
# scripts/run_local.sh — 本地定时任务 wrapper
# 用法：run_local.sh <finance-agent 子命令及参数>
# 例：run_local.sh run
#     run_local.sh weekly-report --force

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_ROOT"

# 加载 .env（忽略注释行和空行）
if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi

# 激活虚拟环境
if [ -f .venv/bin/activate ]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
else
    echo "[run_local.sh] ERROR: .venv not found. Run: uv sync" >&2
    exit 1
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 启动: finance-agent $*"
exec finance-agent "$@"
