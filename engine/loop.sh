#!/usr/bin/env bash
# longhaul-builder — loop.sh：cron/flock 包装（F5 / DESIGN §2.6）。
# 极小 shell：用 flock 防并发重叠，跑**一个** loop.py tick 退出。**不**在 loop.py 内造常驻进程。
#
# 用法：  loop.sh <state_dir> [extra args passed to loop.py tick]
# cron：  */5 * * * *  /path/engine/loop.sh /path/to/proj/.longhaul  >> /path/loop.log 2>&1
# while： while :; do /path/engine/loop.sh <state_dir>; sleep 5; done
#   （driver/judge 命令由 env 注入：$LONGHAUL_DRIVER_CMD / $LONGHAUL_JUDGE_CMD）
set -euo pipefail
STATE_DIR="${1:?usage: loop.sh <state_dir> [extra loop.py tick args]}"; shift || true
ENGINE_DIR="$(cd "$(dirname "$0")" && pwd)"
LOCK="$STATE_DIR/.loop.lock"
PY="${PYTHON:-python3}"

# 并发互斥：拿不到锁（上一个 tick 还在跑）就**安静退出 0**——cron 每 N min 唤起、上个没跑完时不重叠、不双写。
# Linux 用 flock（原逻辑零改动）；macOS/BSD 无 flock 时回退到 mkdir 原子锁（带 stale-pid 回收 + 退出清理）。
if command -v flock >/dev/null 2>&1; then
  exec 9>"$LOCK"
  if ! flock -n 9; then
    echo "[loop.sh] another tick holds $LOCK; skip" >&2
    exit 0
  fi
else
  LOCKDIR="$LOCK.d"
  if ! mkdir "$LOCKDIR" 2>/dev/null; then
    _holder="$(cat "$LOCKDIR/pid" 2>/dev/null || true)"
    if [ -n "${_holder:-}" ] && ! kill -0 "$_holder" 2>/dev/null; then
      rm -rf "$LOCKDIR" 2>/dev/null || true   # 持有者已死 → 回收 stale 锁
    fi
    if ! mkdir "$LOCKDIR" 2>/dev/null; then
      echo "[loop.sh] another tick holds $LOCK; skip" >&2
      exit 0
    fi
  fi
  echo "$$" > "$LOCKDIR/pid"
  trap 'rm -rf "$LOCKDIR" 2>/dev/null || true' EXIT
fi

# 收口守卫（REQUIRED gate-1）：grep 的是 state.py `next`（loop.py status --next-json 委托它）输出的
# {"state": "done"|"blocked"...}。全 DONE/BLOCKED 就别再 tick（while 形态据此停；cron 空跑也无害）。
NEXT=$("$PY" "$ENGINE_DIR/loop.py" status "$STATE_DIR" --next-json 2>/dev/null || true)
case "$NEXT" in
  *'"state": "done"'*)    echo "[loop.sh] all DONE; nothing to do"; exit 0 ;;
  *'"state": "blocked"'*) echo "[loop.sh] all remaining BLOCKED; escalate"; exit 0 ;;
  # 2026-06-23 review：全部剩余在等人确认举旗(NEEDS_CONFIRM)→ 停下等 confirm/reject/resolve，
  # 别空跑（与 done/blocked 一致）。修复"全卡确认门时 cron/while 永远空转、永不停"。
  *'"state": "needs_confirm"'*) echo "[loop.sh] 全部剩余在等人确认举旗(NEEDS_CONFIRM)；停下等回插"; exit 0 ;;
esac

# 跑恰好一个 tick。退出码透传（见 loop.py 契约）：0 推进/no-op；2 用法错；3 产品熔断；
# 4 基建/replan 熔断升级；5 人工 abort；6 P0 未确认（等人 `state.py p0-confirm`）。
# while/cron 包装应在 6（P0 门）/3/4/5 上**停下来喊人**，不要空转重试。
RC=0
"$PY" "$ENGINE_DIR/loop.py" tick "$STATE_DIR" "$@" || RC=$?

# 自动 commit：tick 成功推进后(RC=0)，把刚完成的 milestone 改动各提交一笔（best-effort，不改退出码）。
# LONGHAUL_AUTOCOMMIT=0 可关；非 git 仓 / 没装绑定都安静跳过。绑定自己检测「DONE 但未提交」。
if [ "$RC" = "0" ] && [ "${LONGHAUL_AUTOCOMMIT:-1}" != "0" ]; then
  BIND="$(cd "$ENGINE_DIR/../bindings" 2>/dev/null && pwd || true)"
  if [ -n "${BIND:-}" ] && [ -f "$BIND/commit-milestone.sh" ]; then
    bash "$BIND/commit-milestone.sh" "$(dirname "$STATE_DIR")" "$STATE_DIR" >/dev/null 2>&1 || true
  fi
fi
exit "$RC"
# flock 在 fd 9 关闭（脚本退出）时自动释放。
