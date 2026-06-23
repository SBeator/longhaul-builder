#!/usr/bin/env bash
# 永过 judge stub（确定性）：读 prompt_file 判 kind，输出对应放行裁定的 VERDICT 块。
#   plan_review (含 "PLAN REVIEW") → VERDICT: APPROVE
#   impl_review (含 "IMPL REVIEW") → VERDICT: PASS
# 注意：必须只在**最后**输出一行真裁定（review.parse_verdict 取最后一个合法 VERDICT 行；
# rubric 模板自己含 "VERDICT: APPROVE | ... | REVISE" 这种示例行，会被白名单+取最后规则正确略过）。
set -eu
PF="${1:?prompt_file}"
if grep -q "PLAN REVIEW" "$PF" 2>/dev/null; then
  echo "REASON: 玩具方案可行，放行（stub）。"
  echo "VERDICT: APPROVE"
else
  echo "REASON: 玩具实现满足探针，证据真实（stub）。"
  echo "VERDICT: PASS"
fi
exit 0
