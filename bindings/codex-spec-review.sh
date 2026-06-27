#!/usr/bin/env bash
# longhaul SPEC REVIEWER binding → Codex 审 spec、吐 VERDICT。
# converge.py 调：codex-spec-review.sh <artifact> <context_file>
set -u
ART="${1:?artifact}"; CTX="${2:-}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"; TPL="$HERE/engine/prompts/spec_review.md"
PROJ="$(cd "$(dirname "$ART")" 2>/dev/null && pwd)" || exit 2; cd "$PROJ" || exit 2
MARG=(); [ -n "${LONGHAUL_CODEX_MODEL:-}" ] && MARG=(-m "$LONGHAUL_CODEX_MODEL")
TO="${LONGHAUL_REVIEW_TIMEOUT:-600}"
PF="$(mktemp)"; OUT="$(mktemp)"
{ cat "$TPL"; echo; echo "===== 待审 spec（$ART）====="; cat "$ART" 2>/dev/null
  echo; echo "===== 背景（需求 + 澄清问答）====="; [ -f "$CTX" ] && cat "$CTX" || echo "(无)"; } > "$PF"
timeout "$TO" codex exec --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check "${MARG[@]}" \
  --output-last-message "$OUT" < "$PF" >/dev/null 2>&1
rc=$?
cat "$OUT" 2>/dev/null; rm -f "$PF" "$OUT"
exit $rc
