#!/usr/bin/env bash
# e2e_full.sh —— longhaul 端到端：mock 项目 + 确定性 stub agent（扮 Claude/Codex）跑**真实完整链路**，
# 覆盖各核心能力，确保整条链路没断。零网络零真 LLM（可进 CI）；用真 bin/lhb + loop.sh + engine。
#   用法：bash engine/e2e_full.sh
# stub 签名同真绑定：driver={prompt_file} {state_dir} {milestone_id} {mode}；judge={prompt_file} {evidence_dir}。
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; ENG="$ROOT/engine"; LHB="$ROOT/bin/lhb"; PY="${PYTHON:-python3}"
FIX="$ENG/fixtures/e2e"
PASS=0; FAIL=0
ok(){ if [ "$2" = "$3" ]; then echo "  ✓ $1"; PASS=$((PASS+1)); else echo "  ✗ $1 (期望:$2 实际:$3)"; FAIL=$((FAIL+1)); fi; }

DRV="bash $FIX/e2e-driver.sh {prompt_file} {state_dir} {milestone_id} {mode}"
JDG="bash $FIX/e2e-judge.sh {prompt_file} {evidence_dir}"
export E2E_LOG="$(mktemp)"
# 分阶段 stub（验 #10a：每阶段记录自己的 role）+ 通用兜底
export LONGHAUL_DRIVER_CMD__plan="E2E_ROLE=planner $DRV"
export LONGHAUL_DRIVER_CMD__impl="E2E_ROLE=implementer $DRV"
export LONGHAUL_JUDGE_CMD__plan_review="E2E_ROLE=plan_reviewer $JDG"
export LONGHAUL_JUDGE_CMD__impl_review="E2E_ROLE=impl_reviewer $JDG"
export LONGHAUL_DRIVER_CMD="$DRV"; export LONGHAUL_JUDGE_CMD="$JDG"
# 小超时窗（卡死检测可在秒级触发）
export LONGHAUL_DRIVER_TIMEOUT=25 LONGHAUL_DRIVER_STUCK_TIMEOUT=3 LONGHAUL_REVIEW_TIMEOUT=15 LONGHAUL_PROBE_TIMEOUT=15 E2E_STUCK_SLEEP=15
export LONGHAUL_AUTOCOMMIT=1

mk_proj(){ # <name> <n_milestones> → 建 mock 项目 + spec + n 个 integration milestone + confirm；echo 项目路径
  local name="$1" n="$2" p sd; p="$(mktemp -d)/$name"; mkdir -p "$p"
  ( cd "$p" && git init -q && git config user.email e2e@x && git config user.name e2e )
  sd="$p/.longhaul"; "$LHB" new "$p" "mock: 造文件证明链路通" >/dev/null 2>&1
  : > "$sd/p0-context.md"
  { echo '{"milestones":['
    local i; for i in $(seq 1 "$n"); do
      [ "$i" -gt 1 ] && echo ','
      printf '{"id":"M%d","goal":"造 done_M%d 文件","acceptance":{"type":"integration","probe":"test -f","probe_cmd":"test -f %s/done_M%d.txt"},"max_attempts":5}' "$i" "$i" "$p" "$i"
    done; echo ']}'; } > "$p/ms.json"
  "$PY" "$ENG/state.py" set-milestones "$sd" --file "$p/ms.json" >/dev/null
  echo "$p"
}
drive(){ # <sd> <max_ticks> —— 真 loop.sh 逐拍推进到终态（含 autocommit）；返回最后 next-state
  local sd="$1" cap="${2:-40}" i=0 st
  while [ $i -lt "$cap" ]; do
    st="$("$PY" "$ENG/loop.py" status "$sd" --next-json 2>/dev/null)"
    case "$st" in *'"state": "done"'*) echo done; return;; *'"state": "blocked"'*) echo blocked; return;;
      *'"state": "needs_confirm"'*) echo needs_confirm; return;; esac
    bash "$ENG/loop.sh" "$sd" >/dev/null 2>&1 || true
    i=$((i+1))
  done; echo "timeout"; }

echo "========== longhaul 端到端（确定性 stub，真实链路）=========="

echo "[E2E-1] P0 spec 双 agent 收敛（reviewer 先 REVISE 一轮、proposer 改后 APPROVE）"
P1="$(mk_proj calc 2)"; SD1="$P1/.longhaul"
echo "（e2e spec draft body）" >> "$SD1/spec.md"
O1="$(LONGHAUL_SPEC_REVIEWER_CMD="bash $FIX/e2e-spec-reviewer.sh {artifact}" \
      LONGHAUL_SPEC_PROPOSER_CMD="bash $FIX/e2e-spec-proposer.sh {artifact}" \
      "$LHB" spec-converge "$P1" 2>&1)"
ok "spec-converge 收敛"            "1" "$(echo "$O1" | grep -q '\"converged\": true' && echo 1 || echo 0)"
ok "spec-converge 报告讨论 2 轮"   "1" "$(echo "$O1" | grep -q '\"rounds\": 2' && echo 1 || echo 0)"
ok "spec 真被 proposer 就地改了"   "1" "$(grep -q E2E_FIXED "$SD1/spec.md" && echo 1 || echo 0)"

echo "[E2E-2] P0 硬门：未 confirm 时 loop 拒跑（exit 6）"
rc=0; bash "$ENG/loop.sh" "$SD1" >/dev/null 2>&1 || rc=$?
ok "未 confirm → P0 门挡住(exit 6)" "6" "$rc"
"$LHB" confirm "$P1" --by e2e --force >/dev/null 2>&1
ok "confirm 后 P0 放行"            "0" "$(grep -q '"p0_confirmed"\|p0' "$SD1/cursor.json" 2>/dev/null; "$PY" "$ENG/loop.py" status "$SD1" --next-json 2>/dev/null | grep -q actionable && echo 0 || echo 1)"

echo "[E2E-3] 真实自驱到 DONE（M2 触发返工）+ 分阶段 agent + 播报详情 + 自动 commit"
export E2E_REWORK_MID=M2
ST="$(timeout 150 bash -c "cd '$P1' && '$LHB' run '$P1' --watch >/dev/null 2>&1"; "$PY" "$ENG/loop.py" status "$SD1" --next-json 2>/dev/null)"
ok "跑到全 DONE"                  "1" "$(echo "$ST" | grep -q '\"state\": \"done\"' && echo 1 || echo 0)"
ok "#10a 分阶段:出方案=planner"    "1" "$(grep -qE 'role=planner .*mode=plan-only' "$E2E_LOG" && echo 1 || echo 0)"
ok "#10a 分阶段:实施=implementer"  "1" "$(grep -qE 'role=implementer .*mode=implement' "$E2E_LOG" && echo 1 || echo 0)"
ok "#10a 分阶段:审方案=plan_reviewer" "1" "$(grep -qE 'role=plan_reviewer .*kind=plan_review' "$E2E_LOG" && echo 1 || echo 0)"
ok "#10a 分阶段:审实施=impl_reviewer" "1" "$(grep -qE 'role=impl_reviewer .*kind=impl_review' "$E2E_LOG" && echo 1 || echo 0)"
ok "返工路径:M2 触发 reopen_plan"  "1" "$(grep -q 'reopen_plan' "$SD1/events.jsonl" 2>/dev/null && echo 1 || echo 0)"
ok "#9 播报带「做了什么」详情"     "1" "$(grep -q '完成 —' "$SD1/notify.log" 2>/dev/null && echo 1 || echo 0)"
ok "自动 commit:每 milestone 有提交" "1" "$(git -C "$P1" log --oneline 2>/dev/null | grep -qE 'milestone M1|milestone M2' && echo 1 || echo 0)"
ok "运行报告归档(report.md + INDEX)" "1" "$([ -f "$(ls -d "$P1"/docs/iterations/*/report.md 2>/dev/null | head -1)" ] && [ -f "$P1/docs/iterations/INDEX.md" ] && echo 1 || echo 0)"
ok "#9 运行报告 §2 详情列非空"     "1" "$(R="$(ls "$P1"/docs/iterations/*/report.md 2>/dev/null | head -1)"; [ -n "$R" ] && grep -q '做了什么' "$R" && echo 1 || echo 0)"
ok "#11 token 记账:token_usage 落账" "1" "$(grep -q token_usage "$SD1/events.jsonl" 2>/dev/null && echo 1 || echo 0)"
ok "#11 运行报告含 token 列 + 分析"  "1" "$(R="$(ls "$P1"/docs/iterations/*/report.md 2>/dev/null | head -1)"; [ -n "$R" ] && grep -q '| token |' "$R" && grep -q 'token 结构' "$R" && echo 1 || echo 0)"

echo "[E2E-4] #2 走偏前移：driver 在 plan 期举旗 → milestone NEEDS_CONFIRM + 播报举旗"
P4="$(mk_proj drift 1)"; SD4="$P4/.longhaul"; "$LHB" confirm "$P4" --by e2e --force >/dev/null 2>&1
E2E_DRIFT_MID=M1 bash "$ENG/loop.sh" "$SD4" >/dev/null 2>&1 || true   # plan 期写 flag.json
E2E_DRIFT_MID=M1 bash "$ENG/loop.sh" "$SD4" >/dev/null 2>&1 || true   # 消费 flag → NEEDS_CONFIRM
ok "走偏→M1 NEEDS_CONFIRM"        "NEEDS_CONFIRM" "$("$PY" - "$SD4" <<'PY'
import json,sys; ms=json.load(open(sys.argv[1]+"/milestones.json"))["milestones"]; print(ms[0]["status"])
PY
)"
ok "flag_raised 事件落账"          "1" "$(grep -q 'flag_raised' "$SD4/events.jsonl" 2>/dev/null && echo 1 || echo 0)"

echo "[E2E-5] #1 超时进度感知：impl 卡死被判死 → 续跑后完成（真 loop）"
P5="$(mk_proj stuck 1)"; SD5="$P5/.longhaul"; "$LHB" confirm "$P5" --by e2e --force >/dev/null 2>&1
export E2E_STUCK_MID=M1
ST5="$(timeout 90 bash -c "while ! '$PY' '$ENG/loop.py' status '$SD5' --next-json 2>/dev/null | grep -q '\"state\": \"done\"'; do bash '$ENG/loop.sh' '$SD5' >/dev/null 2>&1 || true; done; echo ok" 2>/dev/null || echo timeout)"
unset E2E_STUCK_MID
ok "卡死被判死(infra_retry timed out)" "1" "$(grep -q 'timed out\|stuck' "$SD5/events.jsonl" 2>/dev/null && echo 1 || echo 0)"
ok "续跑后 M1 跑完 DONE"          "1" "$("$PY" "$ENG/loop.py" status "$SD5" --next-json 2>/dev/null | grep -q '\"state\": \"done\"' && echo 1 || echo 0)"

echo "[E2E-6] SKIPPED 终态：被 split 替换的 milestone 不把整体卡在「进行中」"
P6="$(mk_proj split 1)"; SD6="$P6/.longhaul"
"$PY" "$ENG/state.py" split "$SD6" M1 --into "M1a 子步;M1b 子步" >/dev/null 2>&1 || true
"$PY" - "$SD6" <<'PY'
import json,sys
sd=sys.argv[1]; p=sd+"/milestones.json"; d=json.load(open(p))
for m in d["milestones"]:
    if m["id"]!="M1": m["status"]="DONE"
json.dump(d,open(p,"w"),ensure_ascii=False)
PY
ok "split 后原 M1=SKIPPED"        "1" "$("$PY" -c "import json;ms=json.load(open('$SD6/milestones.json'))['milestones'];print(1 if any(m['id']=='M1' and m['status']=='SKIPPED' for m in ms) else 0)")"
ok "整体判完成(SKIPPED 当终态)"    "✅ 完成" "$("$PY" -c "import sys;sys.path.insert(0,'$ENG');import iterations,json;print(iterations._overall_status(json.load(open('$SD6/milestones.json'))['milestones']))")"

echo "[E2E-7] #10b plan 多 agent panel：plan_review 聚合 N 人裁定（真 loop 经 panel）"
P7="$(mk_proj panel 1)"; SD7="$P7/.longhaul"; "$LHB" confirm "$P7" --by e2e --force >/dev/null 2>&1
# 配 2 人 panel（都 stub APPROVE）；先把 milestone 推到 plan_review
PANEL_J="bash $FIX/e2e-judge.sh {artifact} {context}"   # panel 走 review_panel，judge_cmd 各自跑
LONGHAUL_PLAN_PANEL="$JDG ||| $JDG" bash "$ENG/loop.sh" "$SD7" >/dev/null 2>&1 || true   # plan
LONGHAUL_PLAN_PANEL="$JDG ||| $JDG" bash "$ENG/loop.sh" "$SD7" >/dev/null 2>&1 || true   # plan_review(panel)
ok "plan_review 走了 panel 聚合"  "1" "$(grep -q '"panel": true' "$SD7/evidence/M1/review-plan_review.json" 2>/dev/null && echo 1 || echo 0)"
ok "panel 留了各 panelist 审计文件" "1" "$(ls "$SD7"/evidence/M1/review-plan_review.panel-*.json >/dev/null 2>&1 && echo 1 || echo 0)"

echo ""
echo "========== 覆盖矩阵 =========="
echo "  ① spec 双 agent 收敛  ② P0 硬门  ③ 全相位循环到 DONE  ④ 分阶段 agent(#10a)"
echo "  ⑤ plan panel(#10b)  ⑥ 超时卡死检测(#1)  ⑦ 走偏前移举旗(#2)  ⑧ 返工 reopen-plan"
echo "  ⑨ SKIPPED 终态  ⑩ 播报详情+运行报告(#9)  ⑪ 自动 commit + iterations 归档"
echo "e2e_full：$PASS 绿 / $FAIL 红"
[ "$FAIL" = 0 ]
