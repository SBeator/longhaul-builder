#!/usr/bin/env bash
# longhaul SPEC PROPOSER binding → Claude 就地改 spec。
# converge.py 调：claude-spec-revise.sh <artifact> <feedback_file> <context_file>
set -u
. "$(cd "$(dirname "$0")" && pwd)/compat.sh"   # 跨平台 lhb_timeout（macOS 无原生 timeout）
ART="${1:?artifact}"; FB="${2:-}"; CTX="${3:-}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"; TPL="$HERE/engine/prompts/spec_revise.md"
PROJ="$(cd "$(dirname "$ART")" 2>/dev/null && pwd)" || exit 2; cd "$PROJ" || exit 2
MARG=(); [ -n "${LONGHAUL_CLAUDE_MODEL:-}" ] && MARG=(--model "$LONGHAUL_CLAUDE_MODEL")
TO="${LONGHAUL_DRIVER_TIMEOUT:-900}"
{ cat "$TPL"; echo; echo "## 要就地编辑的 spec 文件：$ART"
  echo "## 评审反馈："; [ -f "$FB" ] && cat "$FB" || echo "(无)"
  echo "## 背景："; [ -f "$CTX" ] && cat "$CTX" || echo "(无)"
  echo "## 当前 spec 内容："; cat "$ART" 2>/dev/null; } \
  | lhb_timeout "$TO" claude -p --dangerously-skip-permissions ${MARG[@]+"${MARG[@]}"}
