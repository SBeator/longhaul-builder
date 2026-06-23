#!/usr/bin/env bash
# longhaul-builder JUDGE binding → real Claude Code headless (read-mostly review).
# review.py invokes: claude-judge.sh <prompt_file> <evidence_dir>
# Renders rubric+evidence-index in <prompt_file>; Claude reads the evidence and prints a VERDICT block.
set -u
PROMPT_FILE="${1:?prompt_file}"; EVID_DIR="${2:-}"
MARG=(); [ -n "${LONGHAUL_CLAUDE_MODEL:-}" ] && MARG=(--model "$LONGHAUL_CLAUDE_MODEL")
TO="${LONGHAUL_JUDGE_TIMEOUT:-600}"
timeout "$TO" claude -p --dangerously-skip-permissions "${MARG[@]}" < "$PROMPT_FILE"
