#!/usr/bin/env bash
# longhaul-builder DRIVER binding → Codex (codex exec, headless full-access).
# loop.py invokes: codex-driver.sh <prompt_file> <state_dir> <milestone_id> <mode>
# 在被建项目 dir 里跑 `codex exec`，让它按渲染好的提示词干活（plan-only/implement）。
# 用 Codex 自己的 read/write/shell。exit 0 = 跑了；非零 = infra fail（不烧 attempt）。
set -u
. "$(cd "$(dirname "$0")" && pwd)/compat.sh"   # 跨平台 lhb_timeout（Linux 用原生 timeout）
PROMPT_FILE="${1:?prompt_file}"; STATE_DIR="${2:?state_dir}"; MID="${3:?mid}"; MODE="${4:-implement}"
PROJECT="$(cd "$(dirname "$STATE_DIR")" 2>/dev/null && pwd)" || exit 2
cd "$PROJECT" || exit 2
MARG=(); [ -n "${LONGHAUL_CODEX_MODEL:-}" ] && MARG=(-m "$LONGHAUL_CODEX_MODEL")
TO="${LONGHAUL_DRIVER_TIMEOUT:-900}"
# --dangerously-bypass-approvals-and-sandbox = 无确认、不沙箱（无人值守全权，等价 claude 的 skip-permissions）
lhb_timeout "$TO" codex exec --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check ${MARG[@]+"${MARG[@]}"} < "$PROMPT_FILE"
