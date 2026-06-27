#!/usr/bin/env bash
# longhaul-builder DRIVER binding → real Claude Code headless.
# loop.py invokes: claude-driver.sh <prompt_file> <state_dir> <milestone_id> <mode>
# Runs `claude -p` headless in the被建项目 dir; the rendered driver prompt tells it what to do
# (plan-only writes plan.md; implement does TDD red→green + writes evidence). It uses Claude's
# own Read/Write/Bash tools. Exit 0 = ran; nonzero = infra fail (loop treats as infra, no attempt burn).
set -u
. "$(cd "$(dirname "$0")" && pwd)/compat.sh"   # 跨平台 lhb_timeout（Linux 用原生 timeout）
PROMPT_FILE="${1:?prompt_file}"; STATE_DIR="${2:?state_dir}"; MID="${3:?mid}"; MODE="${4:-implement}"
PROJECT="$(cd "$(dirname "$STATE_DIR")" 2>/dev/null && pwd)" || exit 2
cd "$PROJECT" || exit 2
MARG=(); [ -n "${LONGHAUL_CLAUDE_MODEL:-}" ] && MARG=(--model "$LONGHAUL_CLAUDE_MODEL")
TO="${LONGHAUL_DRIVER_TIMEOUT:-900}"
# ${MARG[@]+"${MARG[@]}"}：空数组安全展开（兼容 bash 3.2 的 set -u，Linux 现代 bash 行为一致）
lhb_timeout "$TO" claude -p --dangerously-skip-permissions ${MARG[@]+"${MARG[@]}"} < "$PROMPT_FILE"
