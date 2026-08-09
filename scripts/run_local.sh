#!/bin/bash
# scripts/run_local.sh — 本地定时任务 wrapper
# 用法：run_local.sh <finance-agent 子命令及参数>
# 例：run_local.sh run
#     run_local.sh weekly-report --force

# ── 代理端口探测（2026-08-09 注入）__PROXY_DETECT_INJECTED__ ──────────────
# 为什么不从 launchd plist 读死端口：本机代理端口会变，写死之后本任务会
# 每天静默失败，而「没有告警」和「一切正常」长得一模一样。
# 候选表按优先级排列，探到谁在监听就用谁。
# 注：本块内的 export 会覆盖 launchd 传入的值，这正是目的。
_detect_proxy() {
  local p
  for p in 7890 8118 7897 1087; do
    if nc -z -G 1 127.0.0.1 "${p}" 2>/dev/null; then
      echo "http://127.0.0.1:${p}"; return 0
    fi
  done
  return 1
}
_PROXY_URL="$(_detect_proxy || echo '')"
if [ -n "${_PROXY_URL}" ]; then
  export HTTP_PROXY="${_PROXY_URL}" HTTPS_PROXY="${_PROXY_URL}"
  export http_proxy="${_PROXY_URL}" https_proxy="${_PROXY_URL}"
fi
export NO_PROXY="localhost,127.0.0.1,.feishu.cn,.larksuite.com,.moonshot.cn,.kimi.com,.minimaxi.com,.deepseek.com"
export no_proxy="${NO_PROXY}"
# ─────────────────────────────────────────────────────────────────────

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
