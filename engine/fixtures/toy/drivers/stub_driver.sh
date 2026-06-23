#!/usr/bin/env bash
# 确定性 driver stub（玩具靶子，零网络零 LLM）。
# 用法：stub_driver.sh <mode> <state_dir> <milestone_id> <project_dir>
#   plan-only  → 覆盖式写 <state_dir>/evidence/<mid>/plan.md（一句话方案）
#   implement  → 造目标文件 <project>/t1_done.txt（=被建产物）+ 写 green.txt 证据
# 关键：所有写都是**覆盖式**（> 而非 >>），保证 crash 后重跑覆盖半成品（AC5 幂等续跑）。
set -eu
MODE="${1:?mode}"; STATE_DIR="${2:?state_dir}"; MID="${3:?mid}"; PROJECT="${4:?project}"
EVD="$STATE_DIR/evidence/$MID"
mkdir -p "$EVD"

case "$MODE" in
  plan-only)
    # 覆盖式写整份 plan.md（不追加）——半成品被这次重跑整体覆盖
    cat > "$EVD/plan.md" <<EOF
# PLAN $MID (toy stub, deterministic)
做法：implement 时 touch \$PROJECT/t1_done.txt 即满足 probe (test -f)。
测试策略：probe_cmd = test -f t1_done.txt（确定性 shell 检查）。
范围：只造该文件，不碰别的 milestone。
EOF
    ;;
  implement)
    # 造被建产物（让 probe test -f 真过）+ 覆盖式写 green 证据
    : > "$PROJECT/t1_done.txt"
    cat > "$EVD/green.txt" <<EOF
=== TOY DRIVER implement $MID ===
\$ test -f $PROJECT/t1_done.txt
EXIT_CODE=0
EOF
    ;;
  *)
    echo "stub_driver: unknown mode '$MODE'" >&2; exit 2 ;;
esac
exit 0
