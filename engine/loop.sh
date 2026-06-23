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

# flock：拿不到锁（上一个 tick 还在跑）就**安静退出 0**——cron 每 N min 唤起、上个没跑完时不重叠、不双写。
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "[loop.sh] another tick holds $LOCK; skip" >&2
  exit 0
fi

# 收口守卫（REQUIRED gate-1）：grep 的是 state.py `next`（loop.py status --next-json 委托它）输出的
# {"state": "done"|"blocked"...}。全 DONE/BLOCKED 就别再 tick（while 形态据此停；cron 空跑也无害）。
NEXT=$("$PY" "$ENGINE_DIR/loop.py" status "$STATE_DIR" --next-json 2>/dev/null || true)
case "$NEXT" in
  *'"state": "done"'*)    echo "[loop.sh] all DONE; nothing to do"; exit 0 ;;
  *'"state": "blocked"'*) echo "[loop.sh] all remaining BLOCKED; escalate"; exit 0 ;;
esac

# 跑恰好一个 tick。退出码透传（见 loop.py 契约）：0 推进/no-op；2 用法错；3 产品熔断；
# 4 基建/replan 熔断升级；5 人工 abort；6 P0 未确认（等人 `state.py p0-confirm`）。
# while/cron 包装应在 6（P0 门）/3/4/5 上**停下来喊人**，不要空转重试。
"$PY" "$ENGINE_DIR/loop.py" tick "$STATE_DIR" "$@"
# flock 在 fd 9 关闭（脚本退出）时自动释放。
