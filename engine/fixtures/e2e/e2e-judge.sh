#!/usr/bin/env bash
# e2e 确定性 judge stub（签名同真 codex-judge.sh：<prompt_file> <evidence_dir>）。
# 读 prompt 判 kind(plan_review/impl_review)；记录角色到 $E2E_LOG；按场景钩子触发返工。
set -u
PF="${1:?}"; EVD="${2:-}"; LOG="${E2E_LOG:-/dev/null}"; ROLE="${E2E_ROLE:-judge}"
MID="$(basename "$EVD")"
KIND=impl_review; grep -q "PLAN REVIEW" "$PF" 2>/dev/null && KIND=plan_review
echo "JUDGE role=$ROLE mid=$MID kind=$KIND" >> "$LOG"
if [ "$KIND" = "plan_review" ]; then
  # 钩子③返工：指定 milestone 的 plan_review 首次 REVISE，之后 APPROVE（验返工回 plan 路径）
  if [ "$MID" = "${E2E_REWORK_MID:-__none__}" ] && [ ! -f "$EVD/.reworked" ]; then
    : > "$EVD/.reworked"; echo "REASON: 方向需改(e2e 返工钩子)"; echo "VERDICT: REVISE"; exit 0
  fi
  echo "REASON: 方案放行(e2e)"; echo "VERDICT: APPROVE"
else
  echo "REASON: 实现满足探针、证据真实(e2e)"; echo "VERDICT: PASS"
fi
exit 0
