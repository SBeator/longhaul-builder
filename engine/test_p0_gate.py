#!/usr/bin/env python3
"""F8 P0 硬门自测：放行 build 前必须人确认 P0 清零（§1.5 必停门之一 / spec D3）。

机制：cursor.p0_confirmed（默认未设）+ state.py `p0-confirm` 动词 + loop.tick 在派 build 活前查
is_p0_confirmed，未确认即拒绝派活、退 6（P0_GATE_EXIT），不调 driver。

核心断言：
  TC1 全新 run（未确认）→ loop tick 拒绝派 build、退 6、driver 一次都没被调（哨兵文件不存在）。
  TC2 `state.py p0-confirm` 后 → 同一 run loop tick 正常推进一相位（退 0），driver 被调（哨兵在）。
  TC3 向后兼容：旧 .longhaul（无 p0_confirmed flag、但已进 build/已起步）→ is_p0_confirmed=True、
      loop tick 不被 P0 门挡（不破坏 已有项目 续跑）。
  TC4 is_p0_confirmed 纯逻辑：显式 flag 优先；旧 run 按「是否已起步」惰性默认。
  TC5 p0-confirm 幂等：重复确认仍 True，事件 already=True。

红线：只在 tempdir 造 state，绝不碰 LIVE .longhaul。四列证据表口径。
运行：python3 engine/test_p0_gate.py  → 退出码 0 全过 / 1 有不一致。
"""
import io
import json
import os
import stat
import sys
import tempfile
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import state  # noqa: E402
import loop    # noqa: E402

rows = []


def check(case, inp, expected, actual):
    ok = (expected == actual)
    rows.append((case, inp, str(expected), str(actual), "✅" if ok else "❌"))
    return ok


class _NS:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def run_loop(*argv):
    buf = io.StringIO()
    code = 1
    with redirect_stdout(buf):
        try:
            code = loop.main(list(argv))
        except SystemExit as e:
            code = int(e.code) if e.code else 0
    return code, buf.getvalue()


def run_state(*argv):
    buf = io.StringIO()
    code = 1
    with redirect_stdout(buf):
        try:
            code = state.main(list(argv))
        except SystemExit as e:
            code = int(e.code) if e.code else 0
    return code, buf.getvalue()


# A driver stub that touches a sentinel so we can prove it was (not) invoked.
_SENTINEL_DRIVER = """#!/usr/bin/env bash
# args: <mode> <state_dir> <mid> <project> (we pass {mode} {state_dir} {milestone_id} via template)
echo "DRIVER RAN" >> "$SENTINEL"
exit 0
"""


def _fresh_run(milestones, confirm=False):
    """造一个**全新、未起步**的 run：init（phase=age）+ 直接写 milestones.json（不经 set-milestones，
    保 phase 不被推到 build）→ 模拟"刚拆完 milestone、还没过 P0 门"。返回 (project, state_dir)。"""
    project = tempfile.mkdtemp(prefix="lhb-p0-")
    state_dir = os.path.join(project, ".longhaul")
    run_state("init", state_dir, "--one-liner", "p0 gate test")  # phase=age
    state.save_milestones(state_dir, milestones)
    if confirm:
        run_state("p0-confirm", state_dir, "--by", "tester")
    return project, state_dir


def _seed_ms():
    return [{"id": "M1", "goal": "demo", "acceptance": {"type": "integration", "probe": "x"},
             "status": "TODO", "phase": "plan", "attempt_count": 0, "max_attempts": 3,
             "last_error": None}]


def _mk_sentinel_driver(project):
    p = os.path.join(project, "driver.sh")
    with open(p, "w", encoding="utf-8") as f:
        f.write(_SENTINEL_DRIVER)
    os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return p


def tc1_unconfirmed_blocks():
    project, state_dir = _fresh_run(_seed_ms(), confirm=False)
    sentinel = os.path.join(project, "sentinel.txt")
    driver = _mk_sentinel_driver(project)
    cmd = "SENTINEL=%s %s {mode} {state_dir} {milestone_id}" % (sentinel, driver)
    code, out = run_loop("tick", state_dir, "--driver-cmd", cmd, "--judge-cmd", "/bin/true")
    check("TC1 未确认 P0 → loop tick 退 6", "fresh run, no p0-confirm", loop.P0_GATE_EXIT, code)
    check("TC1 driver 一次都没被调（哨兵不存在）", "sentinel", False, os.path.exists(sentinel))
    # 事件里有 p0_gate_block
    events = open(os.path.join(state_dir, "events.jsonl")).read()
    check("TC1 events 记 p0_gate_block", "events.jsonl", True, "p0_gate_block" in events)


def tc2_confirmed_dispatches():
    project, state_dir = _fresh_run(_seed_ms(), confirm=True)
    sentinel = os.path.join(project, "sentinel.txt")
    driver = _mk_sentinel_driver(project)
    cmd = "SENTINEL=%s %s {mode} {state_dir} {milestone_id}" % (sentinel, driver)
    code, out = run_loop("tick", state_dir, "--driver-cmd", cmd, "--judge-cmd", "/bin/true")
    # plan phase → driver(plan-only) ran → advance-phase → tick returns 0
    check("TC2 已确认 P0 → loop tick 推进退 0", "p0-confirmed run", 0, code)
    check("TC2 driver 被调（哨兵存在）", "sentinel", True, os.path.exists(sentinel))


def tc3_setmilestones_requires_confirm():
    """🔒 set-milestones（=lhb plan）后默认未确认：必须显式 p0-confirm 才放行——堵"plan 把 phase 推到
    build → 跳过 confirm 直接 run 也能派活"的必停门击穿（2026-06-23 review）。同时验证向后兼容：
    genuinely 旧的 keyless cursor（phase=build、无 p0_confirmed key）仍隐式放行、不被误挡。"""
    project = tempfile.mkdtemp(prefix="lhb-p0-bc-")
    state_dir = os.path.join(project, ".longhaul")
    run_state("init", state_dir, "--one-liner", "fresh run")
    ms_file = os.path.join(project, "ms.json")
    with open(ms_file, "w") as f:
        json.dump({"milestones": _seed_ms()}, f)
    run_state("set-milestones", state_dir, "--file", ms_file)  # 推 phase→build，但默认未确认
    cur = state.load_cursor(state_dir)
    check("TC3 set-milestones 写显式 p0_confirmed=False", "cursor", False, cur.get("p0_confirmed"))
    check("TC3 新拆解未确认 → is_p0_confirmed False", "fresh plan", False,
          state.is_p0_confirmed(state_dir))
    code, _ = run_loop("tick", state_dir, "--dry-run")
    check("TC3 跳过 confirm 直接 tick → 被 P0 门挡（退 6）", "fresh dispatch blocked",
          loop.P0_GATE_EXIT, code)
    # 显式 confirm 后放行
    run_state("p0-confirm", state_dir, "--by", "tester")
    check("TC3 p0-confirm 后 is_p0_confirmed True", "after confirm", True,
          state.is_p0_confirmed(state_dir))
    code2, _ = run_loop("tick", state_dir, "--dry-run")
    check("TC3 confirm 后 tick 不再被 P0 门挡", "after confirm", True, code2 != loop.P0_GATE_EXIT)
    # 向后兼容：genuinely 旧 keyless cursor（phase=build、无 p0_confirmed key）仍隐式确认（不破坏已有项目）
    check("TC3 旧 keyless cursor(phase=build) 仍隐式确认", "backward-compat", True,
          state.is_p0_confirmed("/nonexistent", milestones=[{"status": "DONE", "phase": "done"}],
                                cursor={"phase": "build"}))


def tc4_is_p0_confirmed_logic():
    # 显式 flag 优先
    check("TC4 显式 p0_confirmed=True → True", "{p0_confirmed:true}", True,
          state.is_p0_confirmed("/nonexistent", milestones=[], cursor={"p0_confirmed": True}))
    check("TC4 显式 p0_confirmed=False → False", "{p0_confirmed:false}", False,
          state.is_p0_confirmed("/nonexistent", milestones=[], cursor={"p0_confirmed": False}))
    # 无 flag + phase=build → True
    check("TC4 无 flag + phase=build → True", "{phase:build}", True,
          state.is_p0_confirmed("/nonexistent", milestones=[], cursor={"phase": "build"}))
    # 无 flag + phase=age + 所有 milestone TODO@plan → False（须显式确认）
    fresh_ms = [{"status": "TODO", "phase": "plan"}]
    check("TC4 无 flag + 全新未起步 → False", "{phase:age},TODO@plan", False,
          state.is_p0_confirmed("/nonexistent", milestones=fresh_ms, cursor={"phase": "age"}))
    # 无 flag + phase=age 但有 milestone 已 DONE → True（已起步）
    started_ms = [{"status": "DONE", "phase": "done"}]
    check("TC4 无 flag + 已有 DONE milestone → True", "{phase:age},DONE", True,
          state.is_p0_confirmed("/nonexistent", milestones=started_ms, cursor={"phase": "age"}))


def tc5_p0_confirm_idempotent():
    project, state_dir = _fresh_run(_seed_ms(), confirm=False)
    code, _ = run_state("p0-confirm", state_dir, "--by", "alice")
    check("TC5 首次 p0-confirm 退 0", "p0-confirm", 0, code)
    cur = state.load_cursor(state_dir)
    check("TC5 cursor.p0_confirmed=True", "cursor", True, cur.get("p0_confirmed"))
    check("TC5 记录 p0_confirmed_by", "by", "alice", cur.get("p0_confirmed_by"))
    code2, _ = run_state("p0-confirm", state_dir)  # 重复确认
    check("TC5 重复 p0-confirm 幂等退 0", "p0-confirm again", 0, code2)
    check("TC5 重复后仍 True", "cursor", True, state.load_cursor(state_dir).get("p0_confirmed"))


def main():
    tc1_unconfirmed_blocks()
    tc2_confirmed_dispatches()
    tc3_setmilestones_requires_confirm()
    tc4_is_p0_confirmed_logic()
    tc5_p0_confirm_idempotent()
    npass = sum(1 for r in rows if r[4] == "✅")
    print("\n%-50s | %-26s | %-12s | %-12s | %s" % ("用例", "输入", "期望", "实际", "一致?"))
    print("-" * 122)
    for r in rows:
        print("%-50s | %-26s | %-12s | %-12s | %s" % r)
    ok = npass == len(rows)
    print("\n%s (%d/%d)" % ("ALL PASS ✅" if ok else "SOME FAIL ❌", npass, len(rows)))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
