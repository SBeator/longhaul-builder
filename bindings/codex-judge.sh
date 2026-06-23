#!/usr/bin/env bash
# longhaul-builder JUDGE binding → Codex (codex exec, read-mostly).
# review.py invokes: codex-judge.sh <prompt_file> <evidence_dir>
# Codex 读 rubric+证据、输出 VERDICT 块。用 --output-last-message 拿干净的最终答案给 review.py 解析。
set -u
PROMPT_FILE="${1:?prompt_file}"; EVID_DIR="${2:-}"
MARG=(); [ -n "${LONGHAUL_CODEX_MODEL:-}" ] && MARG=(-m "$LONGHAUL_CODEX_MODEL")
TO="${LONGHAUL_JUDGE_TIMEOUT:-600}"
OUT="$(mktemp)"
timeout "$TO" codex exec --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check "${MARG[@]}" \
  --output-last-message "$OUT" < "$PROMPT_FILE" >/dev/null 2>&1
rc=$?
cat "$OUT" 2>/dev/null; rm -f "$OUT"
exit $rc
