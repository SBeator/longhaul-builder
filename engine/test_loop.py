#!/usr/bin/env python3
"""loop.py 的 dogfood 自测：确定性自驱 tick + cron/flock 包装的集成验收（F5 / AC5）。

立场（DESIGN §2.3/§2.6 / spec AC5 / P0-2 / 红线）：
- driver/judge 全用**本地 shell stub 脚本**（零网络、零 LLM、确定性）；loop.py 不关心命令内部。
- **红线**：测试一律在 `tempfile.mkdtemp()` 里 `state.py init` + 写 seed milestones + 注入 stub cmd，
  **绝不**碰 LIVE 的 `本框架仓自己的 .longhaul`（那是本次构建的 cursor）。
- 四列证据表口径（用例｜输入｜loop 行为/真实结论｜是否一致）：表没填满不许标通过。

覆盖（plan §7.2 + gate-1 强制项）：
  TC1 端到端跑到全 DONE（经 loop.sh + flock + while，无人值守）
  TC2 不可满足 milestone（probe 永 FAIL）→ 烧 attempt 到上限 → BLOCKED
  TC3 中途 kill → 续跑（+ ADOPT：driver 覆盖式写断言，不双 +1/不双 advance）
  TC4 driver 坏（exit 127）→ infra 第二维熔断（attempt_count 不变，退出码 4）
  TC5 门1 REVISE → 回 plan，attempt **不变**（reopen-plan 不烧）
  TC6 verify 先挡（probe FAIL，judge 根本不被调）
  TC7 dry-run（打印路由，不改状态、不跑 driver）
  TC8 reopen-plan 软上限（judge 一直 REOPEN_PLAN → 绕过两熔断 = livelock → max_replans 升级）
  TC9 status --next-json 委托 state.py `next`（emits {"state":...}），不是 status

运行：python3 engine/test_loop.py  → 退出码 0 全过 / 1 有不一致。
"""
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import state  # noqa: E402
import loop    # noqa: E402  (RED 阶段此 import 失败 = 预期的红)

ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
FIXTURE_SRC = os.path.join(ENGINE_DIR, "fixtures", "toy")

rows = []  # (用例, 输入, 期望, 实际, 一致?)


def check(case, inp, expected, actual):
    ok = (expected == actual)
    rows.append((case, inp, str(expected), str(actual), "✅" if ok else "❌"))
    return ok


def run_loop(*argv):
    """跑一条 loop CLI，返回 (exit_code, stdout)。"""
    buf = io.StringIO()
    code = 1
    with redirect_stdout(buf):
        try:
            code = loop.main(list(argv))
        except SystemExit as e:
            code = int(e.code) if e.code else 0
    return code, buf.getvalue().strip()


# ---- tempdir 脚手架（红线：只在 mkdtemp 里造 state；绝不碰 LIVE .longhaul）----

def _mk_project(seed_milestones):
    """造一个最小被建项目 + .longhaul state_dir，写 seed milestones。返回 (project, state_dir)。"""
    project = tempfile.mkdtemp(prefix="lhb-loop-proj-")
    state_dir = os.path.join(project, ".longhaul")
    # state.py init（建 evidence/handoff + spec + 空 milestones + cursor）
    state.cmd_init(_NS(run_dir=state_dir, one_liner="玩具靶子（loop 集成测试）"))
    msfile = os.path.join(project, "seed.json")
    with open(msfile, "w", encoding="utf-8") as f:
        json.dump({"milestones": seed_milestones}, f)
    state.cmd_set_milestones(_NS(run_dir=state_dir, file=msfile))
    state.main(["p0-confirm", state_dir, "--by", "test"])  # set-milestones 后默认未确认，显式放行（P0 门）
    return project, state_dir


class _NS:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _copy_fixture_stubs(dest_dir):
    """把 fixture 的 driver/judge stub copy 到一个 tempdir（绝不在 fixture 原件上跑/改）。"""
    os.makedirs(dest_dir, exist_ok=True)
    for sub in ("drivers", "judges"):
        src = os.path.join(FIXTURE_SRC, sub)
        for fn in os.listdir(src):
            dst = os.path.join(dest_dir, fn)
            shutil.copy2(os.path.join(src, fn), dst)
            os.chmod(dst, os.stat(dst).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return dest_dir


def _toy_milestone(mid, probe_cmd, max_attempts=3):
    """玩具 milestone：integration 验收 + 可执行 probe_cmd（真 shell 检查）。"""
    return {
        "id": mid,
        "goal": "造个文件证明 driver 跑过（%s）" % mid,
        "acceptance": {"type": "integration", "probe": "造文件 + test -f",
                       "probe_cmd": probe_cmd},
        "max_attempts": max_attempts,
    }


def _events(state_dir):
    """读 events.jsonl 全部事件。"""
    p = os.path.join(state_dir, "events.jsonl")
    out = []
    if os.path.exists(p):
        for ln in open(p, encoding="utf-8").read().splitlines():
            ln = ln.strip()
            if ln:
                out.append(json.loads(ln))
    return out


# =========================== 测试用例 ===========================

def tc1_all_done_via_loopsh():
    """TC1：toy（可满足）+ loop.sh+flock+while 反复 tick → 全 DONE（无人值守）。"""
    stubs = _copy_fixture_stubs(tempfile.mkdtemp(prefix="lhb-loop-stubs-"))
    project, sd = _mk_project([
        _toy_milestone("T1", "test -f %s/t1_done.txt" % "{project}"),
    ])
    # probe_cmd 里的 {project} 占位换成真实 project 路径（fixture 探针是真 shell 检查）
    _patch_probe(sd, "T1", "test -f %s/t1_done.txt" % project)

    driver = "bash %s/stub_driver.sh {mode} {state_dir} {milestone_id} %s" % (stubs, project)
    judge = "bash %s/stub_judge_pass.sh {prompt_file}" % stubs
    loop_sh = os.path.join(ENGINE_DIR, "loop.sh")

    env = dict(os.environ)
    env["LONGHAUL_DRIVER_CMD"] = driver
    env["LONGHAUL_JUDGE_CMD"] = judge

    # while + loop.sh + flock：反复 tick 到 state.py next 报 done（无人值守收口守卫）
    done = False
    for _ in range(40):  # 上限保护，避免测试卡死
        r = subprocess.run(["bash", loop_sh, sd], env=env,
                           capture_output=True, text=True, timeout=120)
        # 收口守卫：loop.sh 在全 DONE/BLOCKED 时打印并退出
        nxt = subprocess.run([sys.executable, os.path.join(ENGINE_DIR, "loop.py"),
                              "status", sd, "--next-json"], env=env,
                             capture_output=True, text=True)
        try:
            st = json.loads(nxt.stdout.strip())
        except (ValueError, json.JSONDecodeError):
            st = {}
        if st.get("state") == "done":
            done = True
            break
        if st.get("state") == "blocked":
            break
    check("TC1 while+flock 跑到全 DONE", "loop.sh×while", True, done)

    ms = state.load_milestones(sd)
    t1 = state._find(ms, "T1")
    check("TC1 T1 status=DONE", "全 DONE", "DONE", t1["status"])
    check("TC1 t1_done.txt 真存在（driver 真跑过）", "文件存在性",
          True, os.path.exists(os.path.join(project, "t1_done.txt")))
    evs = [e["ev"] for e in _events(sd)]
    chain_ok = ("phase_advance" in evs and "gate" in evs
                and "step_timing" in evs and "complete" in evs)
    check("TC1 events 有完整 plan→review→complete 链 + step_timing", "events.jsonl",
          True, chain_ok)


def tc2_unsatisfiable_blocked():
    """TC2：probe 永 FAIL → verify 每轮真 FAIL（按退出码）→ 烧 attempt 到上限 → BLOCKED。"""
    stubs = _copy_fixture_stubs(tempfile.mkdtemp(prefix="lhb-loop-stubs-"))
    project, sd = _mk_project([
        _toy_milestone("T2", "test -f /nonexistent/never_xyz.txt", max_attempts=2),
    ])
    # driver_lazy：implement 时**故意不**造目标文件 → probe 永 FAIL
    driver = "bash %s/stub_driver_lazy.sh {mode} {state_dir} {milestone_id} %s" % (stubs, project)
    judge = "bash %s/stub_judge_pass.sh {prompt_file}" % stubs

    last_rc = 0
    for _ in range(30):
        last_rc, _ = run_loop("tick", sd, "--driver-cmd", driver, "--judge-cmd", judge)
        m = state._find(state.load_milestones(sd), "T2")
        if m["status"] == "BLOCKED":
            break
    m = state._find(state.load_milestones(sd), "T2")
    check("TC2 不可满足 → status=BLOCKED", "probe 永 FAIL", "BLOCKED", m["status"])
    check("TC2 熔断退出码 3", "circuit break", 3, last_rc)
    evs = [e["ev"] for e in _events(sd)]
    check("TC2 events 有 circuit_break", "审计", True, "circuit_break" in evs)
    # 反作弊：每轮 verify 都真 FAIL（按真实退出码，不是 judge 救）
    vfails = [e for e in _events(sd) if e.get("ev") == "verify" and e.get("verdict") == "FAIL"]
    check("TC2 verify 真按退出码 FAIL（反作弊）", "verify FAIL≥1", True, len(vfails) >= 1)


def tc3_kill_resume_overwrite():
    """TC3：跑到 impl phase 模拟 kill（留半个含哨兵的 plan.md，不调 advance-phase）→ 再 tick。

    断言：① 不双 +1 / 不双 advance（attempt 正确）② driver 覆盖式写——半个 plan.md 被**整体覆盖**
    非追加（ADOPT：防未来非幂等 driver 静默回归 AC5）③ 最终到 DONE。
    """
    stubs = _copy_fixture_stubs(tempfile.mkdtemp(prefix="lhb-loop-stubs-"))
    project, sd = _mk_project([
        _toy_milestone("T1", "test -f %s/t1_done.txt" % "{project}"),
    ])
    _patch_probe(sd, "T1", "test -f %s/t1_done.txt" % project)
    driver = "bash %s/stub_driver.sh {mode} {state_dir} {milestone_id} %s" % (stubs, project)
    judge = "bash %s/stub_judge_pass.sh {prompt_file}" % stubs

    # —— 模拟 mid-tick kill：手动把 T1 置为 phase=plan（claim 后 driver 跑到一半被杀），
    #    留一个**半截 + 含哨兵内容**的 plan.md（advance-phase 还没调）。
    run_loop("tick", sd, "--driver-cmd", driver, "--judge-cmd", judge)  # tick1: plan（driver 写 plan.md）
    m = state._find(state.load_milestones(sd), "T1")
    a_before = m["attempt_count"]
    # 把状态强制回退到 phase=plan（模拟 advance-phase 之前被 kill），并写一个含哨兵的半截 plan.md
    ms = state.load_milestones(sd)
    mm = state._find(ms, "T1")
    mm["phase"] = "plan"
    mm["status"] = "IN_PROGRESS"
    state.save_milestones(sd, ms)
    plan_path = os.path.join(sd, "evidence", "T1", "plan.md")
    os.makedirs(os.path.dirname(plan_path), exist_ok=True)
    SENTINEL = "<<<HALF-WRITTEN-SENTINEL-DO-NOT-KEEP>>>"
    with open(plan_path, "w", encoding="utf-8") as f:
        f.write("HALF PLAN\n" + SENTINEL + "\n" + ("x" * 4000) + "\n")

    # —— 再 tick：应重跑 driver（覆盖半成品），不双 +1
    run_loop("tick", sd, "--driver-cmd", driver, "--judge-cmd", judge)
    plan_after = open(plan_path, encoding="utf-8").read()
    check("TC3 driver 覆盖式写：哨兵被整体覆盖（非追加）", "overwrite",
          False, SENTINEL in plan_after)
    m = state._find(state.load_milestones(sd), "T1")
    # 重跑 driver（落在 plan/driver 步，不在 gate 动词）→ attempt 不应被双增
    check("TC3 重跑不双 +1（attempt 守住）", "no double +1",
          a_before, m["attempt_count"])

    # —— 续跑到底：应能正常到 DONE
    for _ in range(20):
        run_loop("tick", sd, "--driver-cmd", driver, "--judge-cmd", judge)
        m = state._find(state.load_milestones(sd), "T1")
        if m["status"] == "DONE":
            break
    check("TC3 kill 后最终仍到 DONE", "续跑收口", "DONE",
          state._find(state.load_milestones(sd), "T1")["status"])


def tc4_infra_breaker_no_burn():
    """TC4：driver 坏（exit 127）→ infra 第二维熔断；attempt_count **不变**；退出码 4。"""
    stubs = _copy_fixture_stubs(tempfile.mkdtemp(prefix="lhb-loop-stubs-"))
    project, sd = _mk_project([
        _toy_milestone("T1", "test -f {project}/t1_done.txt"),
    ])
    _patch_probe(sd, "T1", "test -f %s/t1_done.txt" % project)
    driver = "bash %s/stub_driver_broken.sh {mode} {state_dir} {milestone_id} %s" % (stubs, project)
    judge = "bash %s/stub_judge_pass.sh {prompt_file}" % stubs

    a0 = state._find(state.load_milestones(sd), "T1")["attempt_count"]
    last_rc = 0
    for _ in range(12):
        last_rc, _ = run_loop("tick", sd, "--driver-cmd", driver, "--judge-cmd", judge,
                              "--max-infra-retries", "5")
        cur = state.load_cursor(sd)
        if "T1" in (cur.get("infra_blocked") or []):
            break
    a1 = state._find(state.load_milestones(sd), "T1")["attempt_count"]
    check("TC4 infra 熔断退出码 4", "infra breaker", 4, last_rc)
    check("TC4 attempt_count 不变（基建不烧产品维度）", "no burn", a0, a1)
    cur = state.load_cursor(sd)
    check("TC4 infra_retries 累加到上限", "≥5",
          True, cur.get("infra_retries", {}).get("T1", 0) >= 5)
    check("TC4 T1 进 infra_blocked（不活锁）", "blocked list",
          True, "T1" in (cur.get("infra_blocked") or []))
    evs = [e for e in _events(sd) if e.get("ev") == "infra_retry"]
    check("TC4 events 有 infra_retry×N", "审计", True, len(evs) >= 1)


def tc5_revise_no_attempt_burn():
    """TC5：门1 REVISE → 回 plan，attempt_count **不变**（reopen-plan 不烧）。"""
    stubs = _copy_fixture_stubs(tempfile.mkdtemp(prefix="lhb-loop-stubs-"))
    project, sd = _mk_project([
        _toy_milestone("T1", "test -f {project}/t1_done.txt"),
    ])
    _patch_probe(sd, "T1", "test -f %s/t1_done.txt" % project)
    driver = "bash %s/stub_driver.sh {mode} {state_dir} {milestone_id} %s" % (stubs, project)
    judge_revise = "bash %s/stub_judge_revise.sh {prompt_file}" % stubs

    a0 = state._find(state.load_milestones(sd), "T1")["attempt_count"]
    run_loop("tick", sd, "--driver-cmd", driver, "--judge-cmd", judge_revise)  # plan: driver→advance
    m = state._find(state.load_milestones(sd), "T1")
    check("TC5 tick1 后 phase=plan_review", "advance", "plan_review", m["phase"])
    run_loop("tick", sd, "--driver-cmd", driver, "--judge-cmd", judge_revise)  # plan_review: REVISE
    m = state._find(state.load_milestones(sd), "T1")
    check("TC5 REVISE 后回 phase=plan", "reopen-plan", "plan", m["phase"])
    check("TC5 REVISE 后 attempt 不变", "no burn", a0, m["attempt_count"])


def tc6_verify_veto():
    """TC6：probe FAIL → gate-fail impl（烧 attempt），judge **根本不被调**（verify 先否决）。"""
    stubs = _copy_fixture_stubs(tempfile.mkdtemp(prefix="lhb-loop-stubs-"))
    project, sd = _mk_project([
        _toy_milestone("T2", "test -f /nonexistent/never_xyz.txt", max_attempts=5),
    ])
    # driver_lazy：implement 不造文件 → probe FAIL
    driver = "bash %s/stub_driver_lazy.sh {mode} {state_dir} {milestone_id} %s" % (stubs, project)
    # judge 会 PASS——但 verify 先挡住就根本不该调到它
    judge = "bash %s/stub_judge_pass.sh {prompt_file}" % stubs

    # 推到 impl_review：tick1 plan, tick2 plan_review(approve), tick3 impl, tick4 impl_review
    for _ in range(4):
        run_loop("tick", sd, "--driver-cmd", driver, "--judge-cmd", judge)
    evs = _events(sd)
    has_verify_fail = any(e.get("ev") == "verify" and e.get("verdict") == "FAIL" for e in evs)
    # impl_review 这轮**不应**有 impl_review 的 review 事件（verify FAIL 即否决，省 judge）
    impl_reviews = [e for e in evs if e.get("ev") == "review" and e.get("kind") == "impl_review"]
    check("TC6 verify 真 FAIL", "probe FAIL", True, has_verify_fail)
    check("TC6 verify FAIL 时 judge 没被调（省 judge）", "no impl_review",
          0, len(impl_reviews))


def tc7_dry_run():
    """TC7：--dry-run 打印将派的命令 + 将调的 gate 动词，**不改状态、不跑 driver**。"""
    stubs = _copy_fixture_stubs(tempfile.mkdtemp(prefix="lhb-loop-stubs-"))
    project, sd = _mk_project([
        _toy_milestone("T1", "test -f {project}/t1_done.txt"),
    ])
    _patch_probe(sd, "T1", "test -f %s/t1_done.txt" % project)
    driver = "bash %s/stub_driver.sh {mode} {state_dir} {milestone_id} %s" % (stubs, project)
    judge = "bash %s/stub_judge_pass.sh {prompt_file}" % stubs

    ms_before = json.dumps(state.load_milestones(sd), sort_keys=True)
    rc, out = run_loop("tick", sd, "--driver-cmd", driver, "--judge-cmd", judge, "--dry-run")
    ms_after = json.dumps(state.load_milestones(sd), sort_keys=True)
    check("TC7 dry-run 不改状态", "no state change", ms_before, ms_after)
    check("TC7 dry-run 不真造 driver 目标文件", "no side effect",
          False, os.path.exists(os.path.join(project, "t1_done.txt")))
    check("TC7 dry-run 打印路由（含 phase/动词）", "prints plan",
          True, ("plan" in out.lower() or "dry" in out.lower()))


def tc8_reopen_plan_soft_cap():
    """TC8（ADOPT）：judge 一直在门2 REOPEN_PLAN → 绕过两个熔断 = livelock → max_replans 升级。"""
    stubs = _copy_fixture_stubs(tempfile.mkdtemp(prefix="lhb-loop-stubs-"))
    # max_attempts 给很大：证明 reopen-plan 软上限**独立**于 attempt 熔断把 livelock 关掉
    # （reopen-plan 本身不烧 attempt；若只靠 attempt 熔断，软上限存在意义就是它能更早、独立地兜住）。
    project, sd = _mk_project([
        _toy_milestone("T1", "test -f {project}/t1_done.txt", max_attempts=99),
    ])
    _patch_probe(sd, "T1", "test -f %s/t1_done.txt" % project)
    driver = "bash %s/stub_driver.sh {mode} {state_dir} {milestone_id} %s" % (stubs, project)
    # 门1 永 APPROVE，门2 永 REOPEN_PLAN（FAIL + reopen_plan 逃生口）→ 无限回 plan
    judge = "bash %s/stub_judge_reopen.sh {prompt_file}" % stubs

    a0 = state._find(state.load_milestones(sd), "T1")["attempt_count"]
    last_rc = 0
    for _ in range(40):
        last_rc, _ = run_loop("tick", sd, "--driver-cmd", driver, "--judge-cmd", judge,
                              "--max-replans", "3")
        cur = state.load_cursor(sd)
        m = state._find(state.load_milestones(sd), "T1")
        if (cur.get("replan_count", {}).get("T1", 0) >= 3
                and m["status"] != "IN_PROGRESS"):
            break
        if "T1" in (cur.get("infra_blocked") or []) or m["status"] == "BLOCKED":
            break
    cur = state.load_cursor(sd)
    check("TC8 replan_count 累到软上限", "≥3",
          True, cur.get("replan_count", {}).get("T1", 0) >= 3)
    # 超软上限后 loop 必须停止无限回 plan（升级：进 infra_blocked 或 BLOCKED 或退出码 4）
    m = state._find(state.load_milestones(sd), "T1")
    escalated = ("T1" in (cur.get("infra_blocked") or []) or m["status"] == "BLOCKED"
                 or last_rc == 4)
    check("TC8 超软上限后升级（不再无限回 plan）", "livelock 关闭", True, escalated)


def tc10_no_inbox_backward_compat():
    """TC10（F6 回归）：inbox/ 不存在 → tick 行为与 F5 完全一致（向后兼容，零侵入）。

    无 inbox/ 目录时 consume_inbox 提前 return None；一个普通 tick 仍正常 plan→plan_review，
    且**不**创建 inbox/ 目录（不为空跑凭空建目录）。"""
    stubs = _copy_fixture_stubs(tempfile.mkdtemp(prefix="lhb-loop-stubs-"))
    project, sd = _mk_project([
        _toy_milestone("T1", "test -f {project}/t1_done.txt"),
    ])
    _patch_probe(sd, "T1", "test -f %s/t1_done.txt" % project)
    driver = "bash %s/stub_driver.sh {mode} {state_dir} {milestone_id} %s" % (stubs, project)
    judge = "bash %s/stub_judge_pass.sh {prompt_file}" % stubs

    check("TC10 起始无 inbox/ 目录", "no inbox", False,
          os.path.exists(os.path.join(sd, "inbox")))
    rc, _ = run_loop("tick", sd, "--driver-cmd", driver, "--judge-cmd", judge)
    m = state._find(state.load_milestones(sd), "T1")
    check("TC10 无 inbox 时 tick 正常推进（plan→plan_review）", "F5 行为不变",
          "plan_review", m["phase"])
    check("TC10 tick 退 0", "稳定态", 0, rc)
    check("TC10 无 inbox 时 tick 不凭空建 inbox/", "零侵入", False,
          os.path.exists(os.path.join(sd, "inbox")))


def tc9_status_next_json_delegates():
    """TC9（REQUIRED）：loop.py status --next-json 必须委托 state.py `next`（emits {"state":...}），
    不是 status（出 {"phase",by_status}）——loop.sh 收口守卫 grep 的就是 next 的格式。"""
    project, sd = _mk_project([
        _toy_milestone("T1", "test -f {project}/t1_done.txt"),
    ])
    rc, out = run_loop("status", sd, "--next-json")
    try:
        obj = json.loads(out)
    except (ValueError, json.JSONDecodeError):
        obj = {}
    # next 的契约：actionable/done/blocked，有 "state" 键；status 的契约是 "phase"/"by_status"
    check("TC9 --next-json 输出含 state 键（来自 next）", "delegates to next",
          True, "state" in obj)
    check("TC9 --next-json 不是 status 格式（无 by_status）", "not status",
          False, "by_status" in obj)
    check("TC9 初始 T1 → state=actionable", "actionable",
          "actionable", obj.get("state"))

    # 比对真实 next 输出，确认 1:1 委托
    _, real_next = _run_state_next(sd)
    check("TC9 与 state.py next 输出一致", "1:1 delegate",
          json.loads(real_next).get("state"), obj.get("state"))


# ---- 小工具 ----

def _patch_probe(state_dir, mid, probe_cmd):
    """把某 milestone 的 acceptance.probe_cmd 改成真实 shell（替换 {project} 占位）。"""
    ms = state.load_milestones(state_dir)
    m = state._find(ms, mid)
    m["acceptance"]["probe_cmd"] = probe_cmd
    state.save_milestones(state_dir, ms)


def _run_state_next(state_dir):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = state.main(["next", state_dir])
    return code, buf.getvalue().strip()


def main():
    # 先确认 fixture 存在（RED 阶段会缺）
    if not os.path.isdir(FIXTURE_SRC):
        print("FIXTURE MISSING: %s (RED 阶段预期；implement 后应存在)" % FIXTURE_SRC,
              file=sys.stderr)

    tcs = [tc9_status_next_json_delegates, tc7_dry_run, tc5_revise_no_attempt_burn,
           tc6_verify_veto, tc4_infra_breaker_no_burn, tc2_unsatisfiable_blocked,
           tc3_kill_resume_overwrite, tc8_reopen_plan_soft_cap,
           tc10_no_inbox_backward_compat, tc1_all_done_via_loopsh]
    for tc in tcs:
        try:
            tc()
        except Exception as e:
            import traceback
            rows.append((tc.__name__, "EXCEPTION", "no exception",
                         "%s: %s" % (type(e).__name__, e), "❌"))
            traceback.print_exc()

    print("\n用例 | 输入 | 期望 | 实际 | 一致")
    print("--- | --- | --- | --- | ---")
    allok = True
    for c, i, e, a, ok in rows:
        print(f"{c} | {i} | {e} | {a} | {ok}")
        allok = allok and ok == "✅"
    print(f"\n{'ALL PASS ✅' if allok else 'FAIL ❌'} "
          f"({sum(1 for r in rows if r[4]=='✅')}/{len(rows)})")
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
