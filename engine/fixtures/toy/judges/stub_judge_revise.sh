#!/usr/bin/env bash
# 门1打回 judge stub：plan_review 永 REVISE（测 reopen-plan 不烧 attempt）；
# 若被用在 impl_review 上则 FAIL（保持域内合法）。
set -eu
PF="${1:?prompt_file}"
if grep -q "PLAN REVIEW" "$PF" 2>/dev/null; then
  echo "REASON: 方案需修订（stub REVISE）。"
  echo "VERDICT: REVISE"
else
  echo "REASON: 实现需返工（stub FAIL）。"
  echo "VERDICT: FAIL"
fi
exit 0
