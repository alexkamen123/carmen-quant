#!/bin/bash
# 卡门智投 自动迭代 Loop · 单轮 headless 触发器（launchd 每晚调用一次）
# ⚠️ 实验性·未验证：2026-06-25 首验发现 nested headless 多agent cycle 未干净跑通；plist 勿 load，
#    待在真实 launchd 上下文（非嵌套）+ 工具白名单 验证通过、用户在场见证后再启用。
# 验证用法：DRY_RUN=1 bash scripts/auto_iter/run_cycle.sh   （只策划不写码不合并）
set -uo pipefail

REPO="${CARMEN_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
PROMPT="$REPO/scripts/auto_iter/cycle_runner_prompt.md"
LOG_DIR="$REPO/logs/auto_iter"
mkdir -p "$LOG_DIR"
TS=$(date +%Y%m%d-%H%M%S)
LOG="$LOG_DIR/cycle-$TS.log"

# Kill 开关
if [ -f "$REPO/STOP_LOOP" ]; then
  echo "[$(date)] STOP_LOOP 存在，跳过本轮" | tee -a "$LOG"; exit 0
fi

# 代理注入（launchd 不继承 shell 的 HTTPS_PROXY；卡门 CLI 直连 api.anthropic.com 会被 GFW 拦）
export HTTPS_PROXY="http://127.0.0.1:8118"
export HTTP_PROXY="http://127.0.0.1:8118"
export NO_PROXY="localhost,127.0.0.1,.feishu.cn,.larksuite.com,.moonshot.cn,.kimi.com,.minimaxi.com"

cd "$REPO" || { echo "cd 失败" | tee -a "$LOG"; exit 1; }

echo "[$(date)] 自动迭代单轮开始（DRY_RUN=${DRY_RUN:-0}）" | tee -a "$LOG"
# headless：-p 打印模式；--dangerously-skip-permissions 让其无人值守跑 bash/git/subagent
DRY_RUN="${DRY_RUN:-0}" claude -p "$(cat "$PROMPT")" \
  --allowedTools 'Bash(git:*)' 'Bash(uv:*)' 'Bash(gh:*)' 'Bash(python3:*)' Read Write Edit Task Grep Glob \
  2>&1 | tee -a "$LOG"
echo "[$(date)] 自动迭代单轮结束，日志：$LOG" | tee -a "$LOG"
