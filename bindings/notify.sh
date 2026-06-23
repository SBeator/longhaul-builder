#!/usr/bin/env bash
# longhaul-builder 通知绑定（可选，适配层）。给 lhb 的 LONGHAUL_NOTIFY_CMD 用：
#   export LONGHAUL_NOTIFY_CMD="bash <root>/bindings/notify.sh {event} {message} {state_dir}"
# 自驱循环在 done / blocked（要澄清/熔断）时调它，把进度推给人。
# 想接你自己的渠道（IM/webhook/邮件…）改这里即可：
#   - 设 $LONGHAUL_NOTIFY_WEBHOOK → POST 一个 JSON 文本消息到该 URL；
#   - 设 $LONGHAUL_NOTIFY_SHELL   → 把消息作为 $1 传给这条命令（你自己的发送脚本）；
#   - 都没设 → 写 <state_dir>/notify.log 兜底。
set -u
EVENT="${LONGHAUL_NOTIFY_EVENT:-${1:-event}}"
MESSAGE="${LONGHAUL_NOTIFY_MESSAGE:-${2:-}}"
SD="${LONGHAUL_NOTIFY_STATE_DIR:-${3:-.}}"
TEXT="[longhaul:$EVENT] ${MESSAGE}"
if [ -n "${LONGHAUL_NOTIFY_SHELL:-}" ]; then
  "$LONGHAUL_NOTIFY_SHELL" "$TEXT" >/dev/null 2>&1 && exit 0
fi
if [ -n "${LONGHAUL_NOTIFY_WEBHOOK:-}" ]; then
  BODY=$(python3 -c 'import json,sys;print(json.dumps({"text":sys.argv[1]}))' "$TEXT")
  curl -sf -X POST "$LONGHAUL_NOTIFY_WEBHOOK" -H 'Content-Type: application/json' -d "$BODY" >/dev/null 2>&1 && exit 0
fi
echo "[$(date +%FT%T)] $EVENT: $MESSAGE" >> "$SD/notify.log"
