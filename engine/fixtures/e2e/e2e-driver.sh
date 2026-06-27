#!/usr/bin/env bash
# e2e 确定性 driver stub（签名同真 claude-driver.sh：<prompt_file> <state_dir> <mid> <mode>）。
# 记录"哪个角色被哪个阶段调用"到 $E2E_LOG（验 #10a 分阶段 agent）；按场景钩子触发 drift/stuck。
set -u
PF="${1:?}"; SD="${2:?}"; MID="${3:?}"; MODE="${4:-implement}"
PROJ="$(cd "$(dirname "$SD")" 2>/dev/null && pwd)" || exit 2
LOG="${E2E_LOG:-/dev/null}"; ROLE="${E2E_ROLE:-driver}"
echo "DRIVER role=$ROLE mid=$MID mode=$MODE" >> "$LOG"
echo "LHB_TOKENS in=600 out=300"   # #11 token 标记
EVD="$SD/evidence/$MID"; mkdir -p "$EVD"
# 钩子①走偏：指定 milestone 的 plan 期写 flag.json（验 #2 走偏前移 → NEEDS_CONFIRM）
if [ "$MODE" = "plan-only" ] && [ "$MID" = "${E2E_DRIFT_MID:-__none__}" ] && [ ! -f "$EVD/.flagged" ]; then
  : > "$EVD/.flagged"
  printf '{"kind":"spec-divergence","summary":"spec 引用路径对不上，plan 期举旗(e2e)","detail":"x"}' > "$EVD/flag.json"
  exit 0
fi
case "$MODE" in
  plan-only)
    cat > "$EVD/plan.md" <<PEOF
# PLAN $MID (e2e stub)
做法：implement 时 touch $PROJ/done_$MID.txt 满足 probe(test -f)。测试：probe_cmd 确定性。
PEOF
    ;;
  implement)
    # 钩子②卡死：指定 milestone 首次 impl 卡死（无项目文件改动、无输出）→ 触发 #1 stuck 检测被杀；
    # 标记落 .longhaul（被 stuck 进度遍历剪掉，不算进展），续跑时跳过 → 完成。
    if [ "$MID" = "${E2E_STUCK_MID:-__none__}" ] && [ ! -f "$EVD/.stuck_seen" ]; then
      : > "$EVD/.stuck_seen"
      sleep "${E2E_STUCK_SLEEP:-30}"
      exit 0
    fi
    : > "$PROJ/done_$MID.txt"
    printf '$ test -f %s/done_%s.txt\nEXIT_CODE=0\n' "$PROJ" "$MID" > "$EVD/green.txt"
    ;;
  *) echo "e2e-driver: bad mode $MODE" >&2; exit 2 ;;
esac
exit 0
