#!/usr/bin/env bash
# longhaul-builder DRIVER binding → real Claude Code headless.
# loop.py invokes: claude-driver.sh <prompt_file> <state_dir> <milestone_id> <mode>
# Runs `claude -p` headless in the被建项目 dir; the rendered driver prompt tells it what to do
# (plan-only writes plan.md; implement does TDD red→green + writes evidence). It uses Claude's
# own Read/Write/Bash tools. Exit 0 = ran; nonzero = infra fail (loop treats as infra, no attempt burn).
#
# #11 token 记账：默认行为不变（纯文本输出，零风险）。设 LONGHAUL_CLAUDE_JSON=1 时改用
# `--output-format json` 取 usage、把 `LHB_TOKENS in=N out=N` 打到 stdout，引擎据此记 token。
# 改默认前需用真 claude 验一次 json usage 字段名；故先 opt-in，验过再默认开。
set -u
PROMPT_FILE="${1:?prompt_file}"; STATE_DIR="${2:?state_dir}"; MID="${3:?mid}"; MODE="${4:-implement}"
PROJECT="$(cd "$(dirname "$STATE_DIR")" 2>/dev/null && pwd)" || exit 2
cd "$PROJECT" || exit 2
MARG=(); [ -n "${LONGHAUL_CLAUDE_MODEL:-}" ] && MARG=(--model "$LONGHAUL_CLAUDE_MODEL")
TO="${LONGHAUL_DRIVER_TIMEOUT:-900}"
if [ "${LONGHAUL_CLAUDE_JSON:-0}" = "1" ]; then
  OUT="$(mktemp)"
  timeout "$TO" claude -p --output-format json --dangerously-skip-permissions "${MARG[@]}" < "$PROMPT_FILE" > "$OUT"; rc=$?
  python3 - "$OUT" 2>/dev/null <<'PY' || cat "$OUT" 2>/dev/null
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    print(d.get("result", "") or "")
    u = d.get("usage") or {}
    i = int(u.get("input_tokens") or 0); o = int(u.get("output_tokens") or 0)
    if i or o:
        print("LHB_TOKENS in=%d out=%d" % (i, o))
except Exception:
    pass
PY
  rm -f "$OUT"; exit $rc
fi
timeout "$TO" claude -p --dangerously-skip-permissions "${MARG[@]}" < "$PROMPT_FILE"
