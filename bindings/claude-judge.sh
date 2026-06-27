#!/usr/bin/env bash
# longhaul-builder JUDGE binding → real Claude Code headless (read-mostly review).
# review.py invokes: claude-judge.sh <prompt_file> <evidence_dir>
# Renders rubric+evidence-index in <prompt_file>; Claude reads the evidence and prints a VERDICT block.
set -u
. "$(cd "$(dirname "$0")" && pwd)/compat.sh"   # 跨平台 lhb_timeout（Linux 用原生 timeout）
PROMPT_FILE="${1:?prompt_file}"; EVID_DIR="${2:-}"
MARG=(); [ -n "${LONGHAUL_CLAUDE_MODEL:-}" ] && MARG=(--model "$LONGHAUL_CLAUDE_MODEL")
TO="${LONGHAUL_JUDGE_TIMEOUT:-600}"
lhb_timeout "$TO" claude -p --dangerously-skip-permissions ${MARG[@]+"${MARG[@]}"} < "$PROMPT_FILE"
