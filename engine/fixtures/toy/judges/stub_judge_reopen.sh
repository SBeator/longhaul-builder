#!/usr/bin/env bash
# 逃生口 judge stub（测 reopen-plan 软上限 = livelock 防护）：
#   plan_review → APPROVE（让流程进 impl）
#   impl_review → FAIL + 显式 REOPEN_PLAN 标记（判「是方案本身错」逃生口）→ loop 回 plan，**不烧 attempt**。
# 这会绕过 attempt 与 infra 两个熔断，构成真 livelock；loop 的 max_replans 软上限必须把它关掉。
set -eu
PF="${1:?prompt_file}"
if grep -q "PLAN REVIEW" "$PF" 2>/dev/null; then
  echo "REASON: 放行进实施（stub）。"
  echo "VERDICT: APPROVE"
else
  echo "REASON: 实现暴露出方案本身有误，需回门1重开方案。"
  echo "REOPEN_PLAN: true"
  echo "VERDICT: FAIL"
fi
exit 0
