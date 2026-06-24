#!/usr/bin/env python3
"""F2 两道门相位机 + 精确 enter-impl 计数 + 熔断 + 显式 gate 动词 的 dogfood 自测。

证据优先、四列证据表、没证据不许声称通过。
运行：python3 engine/test_state_phases.py  → 退出码 0 全过 / 1 有不一致。

覆盖 plan.md §5 TC1–TC15 + plan-review.md REQUIRED_CHANGES 2/3/4：
- gate-fail(impl)/fail 留 IN_PROGRESS+phase=impl 且 _next_todo 仍重发（活循环重驱路径）。
- complete 从非 impl_review 相位仍 →DONE + 推进 cursor。
- 自托管现状：status=IN_PROGRESS, attempt_count=1, phase=None 的 F2 形状能 complete →DONE。
- 向后兼容：载入 旧式形状（无 phase, 混合 DONE/TODO）→ next/status 不报错。
- cursor.active_phase 镜像跟踪 milestone.phase。
"""
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout, redirect_stderr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import state  # noqa: E402

rows = []  # (用例, 输入, 期望, 实际, 一致?)


def run(*argv):
    """跑一条 state CLI，返回 (exit_code, stdout, stderr)。"""
    out, err = io.StringIO(), io.StringIO()
    code = 1
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = state.main(list(argv))
        except SystemExit as e:  # argparse 错误
            code = int(e.code) if e.code else 0
    return code, out.getvalue().strip(), err.getvalue().strip()


def check(case, inp, expected, actual):
    ok = (expected == actual)
    rows.append((case, inp, str(expected), str(actual), "✅" if ok else "❌"))
    return ok


def _seed(d, milestones):
    """写一份 milestones 文件并 set-milestones 进 run_dir d。"""
    run("init", d, "--one-liner", "F2 相位机测试")
    f = os.path.join(d, "_ms.json")
    with open(f, "w", encoding="utf-8") as fh:
        json.dump({"milestones": milestones}, fh)
    run("set-milestones", d, "--file", f)


def _m(d, mid):
    return state._find(state.load_milestones(d), mid)


def main():
    # ---------- 主线：单 milestone 走两道门一圈 ----------
    d = tempfile.mkdtemp(prefix="lhb-ph-")
    _seed(d, [{"id": "M1", "goal": "核心算法", "max_attempts": 3}])

    # TC1 claim 进 plan，不再 +1
    run("claim", d, "M1")
    m = _m(d, "M1")
    check("TC1 claim 进 plan phase", "claim M1", "plan", m.get("phase"))
    check("TC1 claim 不再+1 A", "claim M1", 0, m["attempt_count"])
    check("TC1 claim status", "claim M1", "IN_PROGRESS", m["status"])
    check("TC1 cursor.active_phase 镜像", "claim M1", "plan",
          state.load_cursor(d).get("active_phase"))

    # TC2 出方案进门1
    run("advance-phase", d, "M1")
    check("TC2 advance plan→plan_review", "advance-phase", "plan_review", _m(d, "M1").get("phase"))

    # TC3 门1 REVISE 打回，不+1
    run("reopen-plan", d, "M1", "--error", "方案不贴 spec")
    m = _m(d, "M1")
    check("TC3 reopen-plan 回 plan", "reopen-plan", "plan", m.get("phase"))
    check("TC3 方案修订不+1", "reopen-plan", 0, m["attempt_count"])
    # 等价：gate-fail --gate plan 也回 plan、不+1
    run("advance-phase", d, "M1")
    run("gate-fail", d, "M1", "--gate", "plan", "--error", "再改")
    m = _m(d, "M1")
    check("TC3' gate-fail plan 回 plan", "gate-fail plan", "plan", m.get("phase"))
    check("TC3' gate-fail plan 不+1", "gate-fail plan", 0, m["attempt_count"])

    # TC4 门1 APPROVE 进实施，首次 +1
    run("advance-phase", d, "M1")  # plan→plan_review
    run("gate-pass", d, "M1", "--gate", "plan")
    m = _m(d, "M1")
    check("TC4 gate-pass plan→impl", "gate-pass plan", "impl", m.get("phase"))
    check("TC4 首次进 impl A==1", "gate-pass plan", 1, m["attempt_count"])
    check("TC4 cursor.active_phase 镜像 impl", "gate-pass plan", "impl",
          state.load_cursor(d).get("active_phase"))

    # TC5 实现就绪进门2
    run("advance-phase", d, "M1")  # impl→impl_review
    check("TC5 advance impl→impl_review", "advance-phase", "impl_review", _m(d, "M1").get("phase"))

    # TC6 门2 FAIL 回实施，重试 +1，且留 IN_PROGRESS + _next_todo 仍重发（活循环重驱）
    run("gate-fail", d, "M1", "--gate", "impl", "--error", "测试没过")
    m = _m(d, "M1")
    check("TC6 gate-fail impl 回 impl", "gate-fail impl", "impl", m.get("phase"))
    check("TC6 实现重试 A==2", "gate-fail impl", 2, m["attempt_count"])
    check("TC6 留 IN_PROGRESS", "gate-fail impl", "IN_PROGRESS", m["status"])
    nxt = state._next_todo(state.load_milestones(d))
    check("TC6 _next_todo 仍重发该 milestone", "gate-fail impl", "M1", nxt["id"] if nxt else None)

    # TC7 门2 REOPEN_PLAN 逃生口，不+1
    run("advance-phase", d, "M1")  # impl→impl_review
    run("reopen-plan", d, "M1", "--error", "方案本身错了")
    m = _m(d, "M1")
    check("TC7 reopen-plan(从 impl_review) 回 plan", "reopen-plan", "plan", m.get("phase"))
    check("TC7 方案级回退不+1", "reopen-plan", 2, m["attempt_count"])

    # TC8 门2 PASS 完成 → DONE + 推进（gate-pass --gate impl = complete）。
    # 用独立 milestone（M1 已被前面用例反复重进 impl，A 接近上限，不适合再测 happy path）。
    d8a = tempfile.mkdtemp(prefix="lhb-pass-")
    _seed(d8a, [{"id": "P1", "goal": "一遍过", "max_attempts": 3}])
    run("claim", d8a, "P1")
    run("advance-phase", d8a, "P1")              # plan→plan_review
    run("gate-pass", d8a, "P1", "--gate", "plan")  # →impl A==1
    run("advance-phase", d8a, "P1")              # impl→impl_review
    run("gate-pass", d8a, "P1", "--gate", "impl")  # =complete
    m = _m(d8a, "P1")
    check("TC8 gate-pass impl status DONE", "gate-pass impl", "DONE", m["status"])
    check("TC8 phase done", "gate-pass impl", "done", m.get("phase"))
    _, out, _ = run("next", d8a)
    check("TC8 next 推进到 done", "next", "done", json.loads(out).get("state"))

    # ---------- TC9 熔断：max=2，gate-pass plan(A1)→gate-fail impl(A2 达上限) ----------
    d2 = tempfile.mkdtemp(prefix="lhb-cb-")
    _seed(d2, [{"id": "B1", "goal": "会卡住", "max_attempts": 2}])
    run("claim", d2, "B1")
    run("advance-phase", d2, "B1")               # plan→plan_review
    run("gate-pass", d2, "B1", "--gate", "plan")  # →impl, A==1
    run("advance-phase", d2, "B1")               # impl→impl_review
    code_sr, _, _ = run("gate-fail", d2, "B1", "--gate", "impl", "--error", "死结")  # A==2 撞上限 → 先自救一次（item5）
    m = _m(d2, "B1")
    check("TC9 撞上限先自救（退 0、不直接熔断）", "gate-fail 到上限", 0, code_sr)
    check("TC9 自救留 IN_PROGRESS@impl", "self-recovery", "IN_PROGRESS", m["status"])
    check("TC9 自救标记 self_recovery_used", "self-recovery", True, m.get("self_recovery_used"))
    run("advance-phase", d2, "B1")               # impl→impl_review（自救重试也失败）
    code_cb, _, _ = run("gate-fail", d2, "B1", "--gate", "impl", "--error", "自救也失败")  # 再撞上限 → 真熔断
    m = _m(d2, "B1")
    check("TC9 自救后再失败 → BLOCKED", "gate-fail 到上限", "BLOCKED", m["status"])
    check("TC9 熔断 phase blocked", "gate-fail 到上限", "blocked", m.get("phase"))
    check("TC9 熔断退出码 3", "gate-fail 到上限", 3, code_cb)

    # TC10 BLOCKED 后 claim 退出码 3（兼容旧契约）
    code_bc, _, _ = run("claim", d2, "B1")
    check("TC10 BLOCKED 后 claim 退出码", "claim blocked", 3, code_bc)

    # ---------- TC11 幂等 re-claim 不双计 ----------
    d3 = tempfile.mkdtemp(prefix="lhb-idem-")
    _seed(d3, [{"id": "I1", "goal": "幂等", "max_attempts": 3}])
    run("claim", d3, "I1")
    run("advance-phase", d3, "I1")
    run("gate-pass", d3, "I1", "--gate", "plan")  # →impl A==1
    a_before = _m(d3, "I1")["attempt_count"]
    run("claim", d3, "I1")  # 幂等 re-claim（已 IN_PROGRESS）
    m = _m(d3, "I1")
    check("TC11 re-claim A 不双计", "re-claim", a_before, m["attempt_count"])
    check("TC11 re-claim phase 不回退", "re-claim", "impl", m.get("phase"))

    # ---------- TC12 兼容：载入 旧式形状（无 phase, 混合 DONE/TODO）→ next/status ----------
    d4 = tempfile.mkdtemp(prefix="lhb-compat-")
    # 直接写无 phase 字段、混合状态的 milestones.json（复刻 旧式形状，不经 set-milestones 归一化）
    with open(state._p(d4, "milestones"), "w", encoding="utf-8") as fh:
        json.dump({"milestones": [
            {"id": "A1", "goal": "骨架", "acceptance": {"type": "cli-golden", "probe": "npm test"},
             "status": "DONE", "attempt_count": 1, "max_attempts": 3, "last_error": None},
            {"id": "A2", "goal": "发牌", "acceptance": {"type": "tdd", "probe": "单测"},
             "status": "DONE", "attempt_count": 1, "max_attempts": 3, "last_error": None},
            {"id": "A3", "goal": "待办", "acceptance": {"type": "tdd", "probe": "单测"},
             "status": "TODO", "attempt_count": 0, "max_attempts": 3, "last_error": None},
        ]}, fh)
    with open(state._p(d4, "cursor"), "w", encoding="utf-8") as fh:
        json.dump({"phase": "done", "active_milestone": None, "active_task": None,
                   "next_action": "x", "updated_at": "x"}, fh)
    cn, out_n, en = run("next", d4)
    cs, out_s, es = run("status", d4)
    check("TC12 旧形状 next 不报错(退出码0)", "旧式形状 next", 0, cn)
    check("TC12 旧形状 next 推导出 A3", "旧式形状 next", "A3", json.loads(out_n).get("milestone", {}).get("id"))
    check("TC12 旧形状 next phase 推导 plan", "旧式 TODO→plan", "plan",
          json.loads(out_n).get("milestone", {}).get("phase"))
    check("TC12 旧形状 status 不报错(退出码0)", "旧式形状 status", 0, cs)

    # ---------- TC13 兼容：旧动词序列 init→set→claim→fail→claim→complete ----------
    # 注：新 fail 留 IN_PROGRESS+impl（不回 TODO），但旧序列最终仍能 complete→DONE。
    d5 = tempfile.mkdtemp(prefix="lhb-oldseq-")
    _seed(d5, [{"id": "S1", "goal": "旧序列", "max_attempts": 3}])
    run("claim", d5, "S1")
    run("fail", d5, "S1", "--error", "一次失败")  # 新语义：留 IN_PROGRESS+impl, A==1
    run("claim", d5, "S1")                          # 幂等 re-claim
    run("complete", d5, "S1")                       # 非 impl_review 也能 complete→DONE
    m = _m(d5, "S1")
    check("TC13 旧序列最终 DONE", "claim→fail→claim→complete", "DONE", m["status"])
    _, out, _ = run("next", d5)
    check("TC13 旧序列 next done", "全完成", "done", json.loads(out).get("state"))

    # ---------- TC14 旧熔断序列退出码不变：claim→fail→claim→fail(max=2)→BLOCKED+3 ----------
    d6 = tempfile.mkdtemp(prefix="lhb-oldcb-")
    _seed(d6, [{"id": "C1", "goal": "旧熔断", "max_attempts": 2}])
    run("claim", d6, "C1")
    run("fail", d6, "C1", "--error", "死结1")        # A==1
    run("claim", d6, "C1")                            # 幂等
    run("fail", d6, "C1", "--error", "死结2")        # A==2 撞上限 → 先自救一次（item5，不熔断）
    m = _m(d6, "C1")
    check("TC14 撞上限先自救（不直接熔断）", "claim→fail×2", "IN_PROGRESS", m["status"])
    run("claim", d6, "C1")
    code_f, _, _ = run("fail", d6, "C1", "--error", "死结3")  # 自救后再撞 → 真熔断
    m = _m(d6, "C1")
    check("TC14 自救后再失败 BLOCKED", "claim→fail×3", "BLOCKED", m["status"])
    check("TC14 自救后熔断退出码 3", "claim→fail×3", 3, code_f)

    # ---------- TC15 非法相位转移干净报错（退出码 2）----------
    d7 = tempfile.mkdtemp(prefix="lhb-illegal-")
    _seed(d7, [{"id": "X1", "goal": "非法", "max_attempts": 3}])
    run("claim", d7, "X1")  # phase=plan
    code_il, _, err_il = run("gate-pass", d7, "X1", "--gate", "impl")  # 在 plan 上调 gate impl 非法
    check("TC15 非法相位转移退出码 2", "gate-pass impl@plan", 2, code_il)
    check("TC15 非法相位有 stderr", "gate-pass impl@plan", True, len(err_il) > 0)

    # ---------- 自托管现状 TC（plan-review REQUIRED_CHANGE #4）----------
    # F2 当前确切形状：status=IN_PROGRESS, attempt_count=1, phase=None（无 phase 字段）
    # → 新 state.py 必须能载入并允许编排者 complete →DONE。
    d8 = tempfile.mkdtemp(prefix="lhb-selfhost-")
    with open(state._p(d8, "milestones"), "w", encoding="utf-8") as fh:
        json.dump({"milestones": [
            {"id": "F2", "goal": "状态机升级", "acceptance": {"type": "tdd", "probe": "TDD"},
             "status": "IN_PROGRESS", "attempt_count": 1, "max_attempts": 3, "last_error": None},
            {"id": "F3", "goal": "verify", "acceptance": {"type": "tdd", "probe": "TDD"},
             "status": "TODO", "attempt_count": 0, "max_attempts": 3, "last_error": None},
        ]}, fh)
    with open(state._p(d8, "cursor"), "w", encoding="utf-8") as fh:
        json.dump({"phase": "build", "active_milestone": "F2", "active_task": None,
                   "next_action": "F2 方案→TDD→实现→验收（attempt 1）", "updated_at": "x"}, fh)
    cn2, _, _ = run("next", d8)   # 必须不报错
    check("SELFHOST next(无phase F2) 不报错", "F2 现状 next", 0, cn2)
    cc, _, _ = run("complete", d8, "F2")  # 编排者升级后能 complete F2
    m = _m(d8, "F2")
    check("SELFHOST complete F2 退出码 0", "complete F2", 0, cc)
    check("SELFHOST F2 →DONE", "complete F2", "DONE", m["status"])
    check("SELFHOST F2 phase done", "complete F2", "done", m.get("phase"))
    _, out, _ = run("next", d8)
    check("SELFHOST complete 后 next 推进 F3", "complete F2", "F3",
          json.loads(out).get("milestone", {}).get("id"))

    # 打印四列证据表
    print("\n用例 | 输入 | 期望 | 实际 | 一致")
    print("--- | --- | --- | --- | ---")
    allok = True
    for c, i, e, a, ok in rows:
        print(f"{c} | {i} | {e} | {a} | {ok}")
        allok = allok and ok == "✅"
    print(f"\n{'ALL PASS ✅' if allok else 'FAIL ❌'} ({sum(1 for r in rows if r[4]=='✅')}/{len(rows)})")
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
