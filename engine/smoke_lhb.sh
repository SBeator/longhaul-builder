#!/usr/bin/env bash
# smoke_lhb.sh —— bin/lhb + loop.sh 的 bash 行为冒烟（python 测试套件覆盖不到的薄绑定层）。
# 守 2026-06-23 review 修复：abort 后 run 不空转、全卡确认门 loop.sh 停下、loop.sh 异常码不空转、
# notify 对含空格/$() 的路径做 shell 引用不被注入。可移植：从脚本位置自定位仓库根，clone 里也能跑。
#   用法：bash engine/smoke_lhb.sh
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; ENG="$ROOT/engine"; LHB="$ROOT/bin/lhb"; PY="${PYTHON:-python3}"
PASS=0; FAIL=0
ok(){ if [ "$2" = "$3" ]; then echo "  ✓ $1"; PASS=$((PASS+1)); else echo "  ✗ $1 (期望:$2 实际:$3)"; FAIL=$((FAIL+1)); fi; }

# 建一个最小"已确认"项目（直接用 state.py 造状态，不跑真 driver）
mk(){ local p; p="$(mktemp -d)/proj"; mkdir -p "$p"; ( cd "$p" && git init -q ); local sd="$p/.longhaul"
  "$PY" "$ENG/state.py" init "$sd" --one-liner "smoke" >/dev/null
  local mf="$p/ms.json"; printf '{"milestones":[{"id":"M1","goal":"x","acceptance":{"type":"tdd"}}]}' > "$mf"
  "$PY" "$ENG/state.py" set-milestones "$sd" --file "$mf" >/dev/null
  "$PY" "$ENG/state.py" p0-confirm "$sd" --by smoke >/dev/null
  echo "$p"; }

echo "[T1] abort 后 lhb run 立刻 break、不空转（P0-4）"
P="$(mk)"; "$PY" "$ENG/loop.py" inbox "$P/.longhaul" abort >/dev/null 2>&1
export LONGHAUL_DRIVER_CMD='echo driver' LONGHAUL_JUDGE_CMD='echo judge'
OUT="$(cd "$P" && timeout 20 bash "$LHB" run "$P" 2>&1)"; RC=$?
ok "abort 后 run 超时前自行退出（不 hang）" "0" "$([ $RC -eq 124 ] && echo 1 || echo 0)"
ok "abort 后输出含 ABORTED" "1" "$(echo "$OUT" | grep -q 'ABORTED' && echo 1 || echo 0)"
unset LONGHAUL_DRIVER_CMD LONGHAUL_JUDGE_CMD

echo "[T2] 全部 milestone=NEEDS_CONFIRM → loop.sh 停下退 0（P1-1）"
P2="$(mk)"; SD2="$P2/.longhaul"
"$PY" - "$SD2" <<'PYIN'
import sys,json,os
sd=sys.argv[1]; p=os.path.join(sd,"milestones.json"); d=json.load(open(p))
for m in d["milestones"]: m["status"]="NEEDS_CONFIRM"; m["phase"]="impl"
json.dump(d,open(p,"w"),ensure_ascii=False)
PYIN
OUT2="$(bash "$ENG/loop.sh" "$SD2" 2>&1)"; RC2=$?
ok "loop.sh 全 NEEDS_CONFIRM 退 0" "0" "$RC2"
ok "loop.sh 输出含 NEEDS_CONFIRM 提示" "1" "$(echo "$OUT2" | grep -q 'NEEDS_CONFIRM' && echo 1 || echo 0)"

echo "[T3] run 在边角项目（无 milestones）上必终止、不空转（done-guard 兜底；catch-all 是防御）"
# 注：rc=2(缺 milestones.json) 经 lhb run 实际被 done-guard 遮蔽（零 milestone→done→顶部 break），
# 不可达；catch-all `*)` 是让"0 才继续、其余都停"契约显式化的防御。这里测真正的安全属性：会终止。
P3="$(mktemp -d)/proj"; mkdir -p "$P3/.longhaul"
OUT3="$(cd "$P3" && timeout 15 bash "$LHB" run "$P3" 2>&1)"; RC3=$?
ok "无 milestones 项目 run 超时前终止（不空转）" "0" "$([ $RC3 -eq 124 ] && echo 1 || echo 0)"
ok "且明确发出终态信号（非静默空转）" "1" \
   "$(echo "$OUT3" | grep -qE 'DONE|退出码|BLOCKED|ABORTED' && echo 1 || echo 0)"

echo "[T4] notify 对含空格/\$() 的 state_dir 做 shell 引用、不注入（bonus 安全）"
eval "$(awk '/^notify\(\)\{/,/^}$/' "$LHB")"   # 抽真实 notify() 函数体直测
SENT="$(mktemp -d)/PWNED"
export LONGHAUL_NOTIFY_CMD='printf "got:%s\n" {state_dir} >> '"$(mktemp -d)/out"
notify abort "含逗号, 和空格 的消息" "/tmp/a \$(touch $SENT) b/.longhaul"
ok "notify 未被 \$() 注入（哨兵未创建）" "0" "$([ -e "$SENT" ] && echo 1 || echo 0)"
unset LONGHAUL_NOTIFY_CMD

echo "[T5] lhb new 脚手架自动产 AGENTS.md + docs/iterations/INDEX.md（item8/9 + 文档收敛）"
NP="$(mktemp -d)/newproj"
bash "$LHB" new "$NP" "做个示例工具" >/dev/null 2>&1
ok "lhb new 产出 AGENTS.md" "1" "$([ -f "$NP/AGENTS.md" ] && echo 1 || echo 0)"
ok "AGENTS.md 指向不复制(含 .longhaul/spec.md + 不复制)" "1" \
   "$(grep -q '.longhaul/spec.md' "$NP/AGENTS.md" 2>/dev/null && grep -q '不复制' "$NP/AGENTS.md" 2>/dev/null && echo 1 || echo 0)"
ok "lhb new 产出结构化 docs/iterations/INDEX.md" "1" \
   "$([ -f "$NP/docs/iterations/INDEX.md" ] && grep -q '迭代历史' "$NP/docs/iterations/INDEX.md" && echo 1 || echo 0)"

echo "[T6] lhb archive-iteration 收敛归档进 docs/iterations/<序号>-<日期>-<slug>/（文档收敛）"
P6="$(mk)"
LONGHAUL_NO_PUBLISH=1 bash "$LHB" archive-iteration "$P6" >/dev/null 2>&1   # 测试不真发飞书（免 junk 文档）
ITDIR="$(ls -d "$P6/docs/iterations/"[0-9][0-9]-* 2>/dev/null | head -1)"
ok "归档出 <序号>-<日期>-<slug>/ 目录" "1" "$([ -n "$ITDIR" ] && [ -d "$ITDIR" ] && echo 1 || echo 0)"
ok "目录内有 report.md（v2 四段式）" "1" "$([ -f "$ITDIR/report.md" ] && grep -q '运行报告' "$ITDIR/report.md" && echo 1 || echo 0)"
ok "目录内有 report.html + state 证据快照 + meta" "1" \
   "$([ -f "$ITDIR/report.html" ] && [ -f "$ITDIR/state/milestones.json" ] && [ -f "$ITDIR/meta.json" ] && echo 1 || echo 0)"
ok "重建了 INDEX.md 结构化列表(最新置顶)" "1" \
   "$(grep -q '⭐ 最新' "$P6/docs/iterations/INDEX.md" 2>/dev/null && echo 1 || echo 0)"
ok "report-doc 仍是别名(复用同目录不新建序号)" "1" \
   "$(LONGHAUL_NO_PUBLISH=1 bash "$LHB" report-doc "$P6" >/dev/null 2>&1; [ "$(ls -d "$P6/docs/iterations/"[0-9][0-9]-* 2>/dev/null | wc -l)" = "1" ] && echo 1 || echo 0)"

echo "[T7] 自动 commit：每个 DONE milestone 的改动被自动提交（commit-milestone 绑定）"
P7="$(mk)"; SD7="$P7/.longhaul"
git -C "$P7" config user.email "t@local" >/dev/null 2>&1; git -C "$P7" config user.name "t" >/dev/null 2>&1
echo "print('feature')" > "$P7/feature.py"
"$PY" - "$SD7" <<'PYIN'
import sys, json, os
p = os.path.join(sys.argv[1], "milestones.json"); d = json.load(open(p))
d["milestones"][0]["status"] = "DONE"
json.dump(d, open(p, "w"))
PYIN
bash "$ROOT/bindings/commit-milestone.sh" "$P7" "$SD7" >/dev/null 2>&1
ok "DONE milestone 产生 'milestone M1' 提交" "1" "$(git -C "$P7" log --oneline 2>/dev/null | grep -q 'milestone M1' && echo 1 || echo 0)"
ok "改动文件 feature.py 进了提交" "1" "$(git -C "$P7" log -1 --name-only 2>/dev/null | grep -q 'feature.py' && echo 1 || echo 0)"
ok ".longhaul 未进代码提交(历史干净)" "0" "$(git -C "$P7" log -1 --name-only 2>/dev/null | grep -q '.longhaul/' && echo 1 || echo 0)"
ok "committed.json 记账了 M1" "1" "$(grep -q 'M1' "$SD7/committed.json" 2>/dev/null && echo 1 || echo 0)"
N1="$(git -C "$P7" rev-list --count HEAD 2>/dev/null)"
bash "$ROOT/bindings/commit-milestone.sh" "$P7" "$SD7" >/dev/null 2>&1
ok "再跑不重复提交(幂等)" "$N1" "$(git -C "$P7" rev-list --count HEAD 2>/dev/null)"
PNG="$(mktemp -d)"; mkdir -p "$PNG/.longhaul"; printf '{"milestones":[{"id":"X","status":"DONE"}]}' > "$PNG/.longhaul/milestones.json"
bash "$ROOT/bindings/commit-milestone.sh" "$PNG" "$PNG/.longhaul" >/dev/null 2>&1
ok "非 git 仓 graceful 跳过(exit 0)" "0" "$?"
ok "LONGHAUL_AUTOCOMMIT=0 时 loop.sh 不调自动 commit" "1" \
   "$(grep -q 'LONGHAUL_AUTOCOMMIT' "$ENG/loop.sh" && echo 1 || echo 0)"

echo "[T8] lhb agents 分阶段角色（默认 14=claude / 23=codex；可单阶段覆盖）"
PA="$(mktemp -d)"; mkdir -p "$PA/.longhaul"; bash "$LHB" agents "$PA" >/dev/null 2>&1
AE="$PA/.longhaul/agents.env"
ok "出方案 plan=claude"        "1" "$(grep -q "DRIVER_CMD__plan=.*claude-driver" "$AE" && echo 1 || echo 0)"
ok "审方案 plan_review=codex"  "1" "$(grep -q "JUDGE_CMD__plan_review=.*codex-judge" "$AE" && echo 1 || echo 0)"
ok "实施 impl=codex"           "1" "$(grep -q "DRIVER_CMD__impl=.*codex-driver" "$AE" && echo 1 || echo 0)"
ok "审实施 impl_review=claude" "1" "$(grep -q "JUDGE_CMD__impl_review=.*claude-judge" "$AE" && echo 1 || echo 0)"
PB="$(mktemp -d)"; mkdir -p "$PB/.longhaul"; bash "$LHB" agents "$PB" --impl claude >/dev/null 2>&1
ok "单阶段覆盖 --impl claude 生效" "1" "$(grep -q "DRIVER_CMD__impl=.*claude-driver" "$PB/.longhaul/agents.env" && echo 1 || echo 0)"

echo ""
echo "smoke_lhb：$PASS 绿 / $FAIL 红"
[ "$FAIL" = 0 ]
