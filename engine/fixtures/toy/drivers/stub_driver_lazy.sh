#!/usr/bin/env bash
# 不可满足 driver stub：plan-only 正常出方案，但 implement 时**故意不造目标文件** →
# probe (test -f ...) 永 FAIL → 验证确定性闸真能挡住（产品熔断 / verify 否决）。
# 用法：stub_driver_lazy.sh <mode> <state_dir> <milestone_id> <project_dir>
set -eu
MODE="${1:?mode}"; STATE_DIR="${2:?state_dir}"; MID="${3:?mid}"; PROJECT="${4:?project}"
EVD="$STATE_DIR/evidence/$MID"
mkdir -p "$EVD"

case "$MODE" in
  plan-only)
    cat > "$EVD/plan.md" <<EOF
# PLAN $MID (lazy stub)
方案声称会造文件，但 implement 故意不造 → probe 必 FAIL（测确定性闸）。
EOF
    ;;
  implement)
    # 故意什么都不造（写一个无用 green.txt，但不满足 probe）
    cat > "$EVD/green.txt" <<EOF
=== LAZY DRIVER（故意不造目标文件）$MID ===
EXIT_CODE=0
EOF
    ;;
  *)
    echo "stub_driver_lazy: unknown mode '$MODE'" >&2; exit 2 ;;
esac
exit 0
