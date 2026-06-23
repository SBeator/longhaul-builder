#!/usr/bin/env python3
"""F7 看门狗（watchdog: lease + heartbeat + TTL sweep + rerun）的 dogfood 自测（TDD · AC7 · DESIGN §2.5）。

立场（与 test_loop.py / test_inbox.py 同骨架）：
- driver/judge 全用**本地 shell stub 脚本**（零网络、零 LLM、确定性）；loop 不关心命令内部。
- **红线**：所有测试一律在 `tempfile.mkdtemp()` 里造 state_dir，绝不碰 LIVE 的
  `本框架仓自己的 .longhaul`（那是本次构建的 cursor）。
- **模拟"取 lease 后死亡"绝不真杀进程**：spawn 一个短命子进程（`true` / `sleep 0`）等它退出，
  确认 `os.kill(pid,0)` 已抛 ProcessLookupError（pid 确死）后，把该 pid 手写进一把 stale lease。
  这等价于"上个 tick 取了 lease 然后崩了"，确定性、CI 友好、绝不误伤无关进程。
- 四列证据表口径（用例｜输入｜期望｜实际｜是否一致）：表没填满不许标通过。

覆盖（plan §7 TC1–TC8 + gate-1 ADOPT 边界）：
  TC1  取 lease 后死亡（dead pid + 过期心跳）→ 下一 tick reclaim + rerun → 续到 DONE，attempt 守恒
  TC2  活 lease 不被误回收（心跳新鲜）
  TC3  活 lease 不被误回收（心跳过期但 pid=os.getpid() 在世）
  TC4  ⭐ADOPT-1：FOREIGN 活 pid（仍在跑的 spawned 子进程，非测试自身）+ 过期心跳 → NOT 回收
  TC5  reclaim-loop 软上限 → 升级（exit 4、reclaim_break、infra_blocked）
  TC6  paused → 不被 sweep（F6 carry-forward：故意空转的 driver 绝不回收）
  TC7  正常完成释放 lease（finally release：跑完 cursor.lease 被清）
  TC8  向后兼容（无 lease 段 → sweep 零成本跳过、F5/F6 行为不变）
  TC9  跨机 lease 判死（host≠本机 + 过期心跳 → dead → reclaim；跨机 pid 无意义不兜活）
  TC10 ADOPT-3：成功推进相位后 reclaim_count 被清零（reclaim 不跨成功累积、不误升级）

运行：python3 engine/test_watchdog.py  → 退出码 0 全过 / 1 有不一致。
"""
import io
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import state  # noqa: E402
import loop    # noqa: E402  (RED 阶段缺 sweep_stale_lease/acquire_lease 等 → 预期的红)

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


class _NS:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _mk_project(seed_milestones):
    """造最小被建项目 + .longhaul state_dir，写 seed milestones。返回 (project, state_dir)。"""
    project = tempfile.mkdtemp(prefix="lhb-wd-proj-")
    state_dir = os.path.join(project, ".longhaul")
    state.cmd_init(_NS(run_dir=state_dir, one_liner="玩具靶子（watchdog 测试）"))
    msfile = os.path.join(project, "seed.json")
    with open(msfile, "w", encoding="utf-8") as f:
        json.dump({"milestones": seed_milestones}, f)
    state.cmd_set_milestones(_NS(run_dir=state_dir, file=msfile))
    return project, state_dir


def _copy_fixture_stubs(dest_dir):
    os.makedirs(dest_dir, exist_ok=True)
    for sub in ("drivers", "judges"):
        src = os.path.join(FIXTURE_SRC, sub)
        for fn in os.listdir(src):
            dst = os.path.join(dest_dir, fn)
            shutil.copy2(os.path.join(src, fn), dst)
            os.chmod(dst, os.stat(dst).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return dest_dir


def _toy_milestone(mid, probe_cmd, max_attempts=3):
    return {
        "id": mid,
        "goal": "造个文件证明 driver 跑过（%s）" % mid,
        "acceptance": {"type": "integration", "probe": "造文件 + test -f",
                       "probe_cmd": probe_cmd},
        "max_attempts": max_attempts,
    }


def _events(state_dir):
    p = os.path.join(state_dir, "events.jsonl")
    out = []
    if os.path.exists(p):
        for ln in open(p, encoding="utf-8").read().splitlines():
            ln = ln.strip()
            if ln:
                out.append(json.loads(ln))
    return out


def _patch_probe(state_dir, mid, probe_cmd):
    ms = state.load_milestones(state_dir)
    m = state._find(ms, mid)
    m["acceptance"]["probe_cmd"] = probe_cmd
    state.save_milestones(state_dir, ms)


def _set_phase(state_dir, mid, status, phase):
    ms = state.load_milestones(state_dir)
    m = state._find(ms, mid)
    m["status"] = status
    m["phase"] = phase
    state.save_milestones(state_dir, ms)


def _toy_cmds(stubs, project, driver="stub_driver.sh", judge="stub_judge_pass.sh"):
    return ("bash %s/%s {mode} {state_dir} {milestone_id} %s" % (stubs, driver, project),
            "bash %s/%s {prompt_file}" % (stubs, judge))


def _stub_opts(stubs, project, **over):
    o = {
        "driver_cmd": "bash %s/stub_driver.sh {mode} {state_dir} {milestone_id} %s" % (stubs, project),
        "judge_cmd": "bash %s/stub_judge_pass.sh {prompt_file}" % stubs,
        "driver_timeout": 600, "probe_timeout": 600, "review_timeout": 600,
        "max_infra_retries": 5, "max_replans": 5, "max_reclaims": 3,
        "lease_ttl": 1800, "dry_run": False,
    }
    o.update(over)
    return o


def _dead_pid():
    """拿一个**确定不在世**的 pid：spawn 一个短命子进程，等它退出 + 回收，确认 os.kill 抛错。

    绝不 kill 真实无关进程：只 spawn 自己的子进程并等它自然退出，再复用其（已回收的）pid。
    """
    p = subprocess.Popen(["true"])
    p.wait()  # 等它退出并回收（避免僵尸；该 pid 自此不再属于活进程）
    pid = p.pid
    # 二次确认：os.kill(pid,0) 必须抛 ProcessLookupError，否则该 pid 偶发被别人占用 → 换一个
    for _ in range(5):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return pid              # 确死
        except PermissionError:
            pass                    # 被别人占用了，换一个
        p = subprocess.Popen(["true"])
        p.wait()
        pid = p.pid
    return pid


def _write_lease(state_dir, mid, phase, pid, host=None, age_over_ttl=True, ttl=1800):
    """手写一把 lease 进 cursor（模拟"上个 tick 取了 lease"）。

    age_over_ttl=True → heartbeat 设成 now-ttl-60（过期）；False → heartbeat=now（新鲜）。
    """
    cur = state.load_cursor(state_dir)
    if age_over_ttl:
        hb = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                           time.gmtime(time.time() - ttl - 60))
    else:
        hb = state._now()
    cur["lease"] = {
        "owner": "loop-tick",
        "pid": pid,
        "host": host if host is not None else socket.gethostname(),
        "milestone": mid,
        "phase": phase,
        "acquired": hb,
        "heartbeat": hb,
        "ttl": ttl,
    }
    state.save_cursor(state_dir, cur)


# =========================== 测试用例 ===========================

def tc1_dead_pid_reclaim_rerun():
    """TC1：取 lease 后死亡（dead pid + 过期心跳）、milestone IN_PROGRESS@impl →
    下一 tick：lease 被清、记 watchdog_reclaim、driver 重跑、attempt 守恒、最终续到 DONE。"""
    stubs = _copy_fixture_stubs(tempfile.mkdtemp(prefix="lhb-wd-stubs-"))
    project, sd = _mk_project([_toy_milestone("T1", "x")])
    _patch_probe(sd, "T1", "test -f %s/t1_done.txt" % project)
    driver, judge = _toy_cmds(stubs, project)

    # 把 T1 置 IN_PROGRESS@impl（claim 后、driver 跑到一半被 kill），写一把死 lease。
    _set_phase(sd, "T1", "IN_PROGRESS", "impl")
    a0 = state._find(state.load_milestones(sd), "T1")["attempt_count"]
    dead = _dead_pid()
    _write_lease(sd, "T1", "impl", dead, age_over_ttl=True)

    # 下一 tick：sweep 应判孤儿 → reclaim
    run_loop("tick", sd, "--driver-cmd", driver, "--judge-cmd", judge)
    cur = state.load_cursor(sd)
    evs = [e for e in _events(sd) if e.get("ev") == "watchdog_reclaim"]
    check("TC1 reclaim 后 cursor.lease 被清", "dead lease swept", None, cur.get("lease"))
    check("TC1 记 watchdog_reclaim 事件（审计）", "reclaim event", True, len(evs) >= 1)
    a1 = state._find(state.load_milestones(sd), "T1")["attempt_count"]
    check("TC1 reclaim 不烧 product attempt_count（守恒）", "no attempt burn", a0, a1)

    # 续跑到底：应能正常到 DONE（rerun 把 impl 相位重跑通）
    for _ in range(20):
        run_loop("tick", sd, "--driver-cmd", driver, "--judge-cmd", judge)
        if state._find(state.load_milestones(sd), "T1")["status"] == "DONE":
            break
    check("TC1 reclaim+rerun 后最终续到 DONE", "rerun→DONE", "DONE",
          state._find(state.load_milestones(sd), "T1")["status"])


def tc2_fresh_heartbeat_not_reclaimed():
    """TC2：活 lease（心跳新鲜、pid=本进程）→ sweep 不 reclaim：lease 仍在、无 watchdog_reclaim。"""
    stubs = _copy_fixture_stubs(tempfile.mkdtemp(prefix="lhb-wd-stubs-"))
    project, sd = _mk_project([_toy_milestone("T1", "x")])
    _set_phase(sd, "T1", "IN_PROGRESS", "impl")
    _write_lease(sd, "T1", "impl", os.getpid(), age_over_ttl=False)  # 心跳=now（新鲜）

    sig = loop.sweep_stale_lease(sd, _stub_opts(stubs, project))
    cur = state.load_cursor(sd)
    evs = [e for e in _events(sd) if e.get("ev") == "watchdog_reclaim"]
    check("TC2 心跳新鲜 → sweep 不回收（返回 None）", "fresh→live", None, sig)
    check("TC2 lease 保留（未被清）", "lease kept", True, cur.get("lease") is not None)
    check("TC2 无 watchdog_reclaim 事件", "no reclaim", 0, len(evs))


def tc3_expired_hb_own_pid_alive_not_reclaimed():
    """TC3：心跳过期、但 pid=os.getpid()（本进程必在世）、host=本机 → pid 兜底，不回收（防误杀慢 step）。"""
    stubs = _copy_fixture_stubs(tempfile.mkdtemp(prefix="lhb-wd-stubs-"))
    project, sd = _mk_project([_toy_milestone("T1", "x")])
    _set_phase(sd, "T1", "IN_PROGRESS", "impl")
    _write_lease(sd, "T1", "impl", os.getpid(), age_over_ttl=True)  # 心跳过期、pid 本进程在世

    sig = loop.sweep_stale_lease(sd, _stub_opts(stubs, project))
    cur = state.load_cursor(sd)
    evs = [e for e in _events(sd) if e.get("ev") == "watchdog_reclaim"]
    check("TC3 心跳过期但 pid 在世 → 不回收（pid 兜底）", "alive pid", None, sig)
    check("TC3 lease 保留（防误杀慢 step）", "lease kept", True, cur.get("lease") is not None)
    check("TC3 无 watchdog_reclaim 事件", "no reclaim", 0, len(evs))


def tc4_foreign_live_pid_not_reclaimed():
    """TC4 ⭐ADOPT-1：lease pid = 一个**仍在跑的 spawned 子进程**（非测试自身 pid）+ 过期心跳 →
    NOT 回收（更可信地证"误杀慢但活"被防住，是真外部活 pid 不是 os.getpid()）。跑完 reap 子进程。"""
    stubs = _copy_fixture_stubs(tempfile.mkdtemp(prefix="lhb-wd-stubs-"))
    project, sd = _mk_project([_toy_milestone("T1", "x")])
    _set_phase(sd, "T1", "IN_PROGRESS", "impl")

    child = subprocess.Popen(["sleep", "30"])  # 真外部活进程（非测试自身）
    try:
        # 防御：确认它确实在世
        os.kill(child.pid, 0)
        _write_lease(sd, "T1", "impl", child.pid, age_over_ttl=True)  # 外部活 pid + 过期心跳
        sig = loop.sweep_stale_lease(sd, _stub_opts(stubs, project))
        cur = state.load_cursor(sd)
        evs = [e for e in _events(sd) if e.get("ev") == "watchdog_reclaim"]
        check("TC4 ⭐外部活 pid + 过期心跳 → 不回收", "foreign live pid", None, sig)
        check("TC4 lease 保留（真外部活 step 不被误杀）", "lease kept",
              True, cur.get("lease") is not None)
        check("TC4 无 watchdog_reclaim 事件", "no reclaim", 0, len(evs))
    finally:
        child.terminate()
        child.wait()  # reap 子进程，绝不留孤儿


def tc5_reclaim_loop_escalates():
    """TC5：可复现崩溃 = 每次 rerun 没推进成功就又崩（sweep 连判孤儿、其间无成功相位清零）→
    撞 max_reclaims → 升级（escalate、reclaim_break、infra_blocked、tick 退 4）。

    用 `--driver-cmd` 指向坏 driver（exit 127）：每拍 driver 一启动就崩（rerun 不会推进成功 →
    reclaim_count 不被 _reset_infra 清零，真累积到上限）。每拍开头手写一把新的死 lease 模拟"上拍崩了"。
    """
    stubs = _copy_fixture_stubs(tempfile.mkdtemp(prefix="lhb-wd-stubs-"))
    project, sd = _mk_project([_toy_milestone("T1", "x", max_attempts=99)])
    _patch_probe(sd, "T1", "test -f %s/t1_done.txt" % project)
    # 坏 driver：rerun 永崩（exit 127）→ 这拍 driver 步走 infra（不推进成功），reclaim_count 不被清零。
    driver, judge = _toy_cmds(stubs, project, driver="stub_driver_broken.sh")
    MAXR = 3

    # 直接驱动 sweep（精确建模"每次 rerun 没成功推进就又崩"——每拍写新死 lease → sweep 判孤儿 +1）。
    opts = _stub_opts(stubs, project, max_reclaims=MAXR, driver_cmd=driver, judge_cmd=judge)
    last_sig = None
    for i in range(MAXR + 1):
        _set_phase(sd, "T1", "IN_PROGRESS", "impl")
        _write_lease(sd, "T1", "impl", _dead_pid(), age_over_ttl=True)
        last_sig = loop.sweep_stale_lease(sd, opts)
        if "T1" in (state.load_cursor(sd).get("infra_blocked") or []):
            break
    cur = state.load_cursor(sd)
    check("TC5 reclaim_count 累到软上限", "≥%d" % MAXR, True,
          cur.get("reclaim_count", {}).get("T1", 0) >= MAXR)
    check("TC5 撞上限 → mid 进 infra_blocked（升级）", "escalate", True,
          "T1" in (cur.get("infra_blocked") or []))
    check("TC5 撞上限那拍 sweep 返回 'escalate'", "sweep escalate", "escalate", last_sig)
    evs = [e["ev"] for e in _events(sd)]
    check("TC5 记 reclaim_break 事件", "审计", True, "reclaim_break" in evs)

    # 端到端补证：撞上限后写一把新死 lease + 真 tick → tick 端到端退升级码 4（escalate 路由到退出码）。
    _set_phase(sd, "T1", "IN_PROGRESS", "impl")
    _write_lease(sd, "T1", "impl", _dead_pid(), age_over_ttl=True)
    # 把 mid 先从 infra_blocked 拿掉、reclaim_count 顶到上限-不足，确保这拍 sweep 自己判 escalate
    cur = state.load_cursor(sd)
    cur["infra_blocked"] = []
    cur["reclaim_count"] = {"T1": MAXR - 1}  # 这拍 +1 = MAXR → 升级
    state.save_cursor(sd, cur)
    rc, _ = run_loop("tick", sd, "--max-reclaims", str(MAXR),
                     "--driver-cmd", driver, "--judge-cmd", judge)
    check("TC5 撞上限那拍 tick 端到端退升级码 4", "exit 4", 4, rc)


def tc6_paused_not_swept():
    """TC6（F6 carry-forward · 题面核心约束）：paused tick 的故意空转 driver 绝不被 watchdog 回收。

    两道防线：① 端到端——drop pause 消息 + 写死 lease → consume_inbox 返回 paused、tick 在 sweep 之前
    早退（no-op、lease 保留、无 reclaim）；② 直接调 sweep + cur.paused=True → sweep 内显式短路（belt-
    and-suspenders），即便顺序被未来改动也不回收。"""
    stubs = _copy_fixture_stubs(tempfile.mkdtemp(prefix="lhb-wd-stubs-"))
    project, sd = _mk_project([_toy_milestone("T1", "x")])
    _set_phase(sd, "T1", "IN_PROGRESS", "impl")
    _write_lease(sd, "T1", "impl", _dead_pid(), age_over_ttl=True)  # 即便 lease 看着像死的
    driver, judge = _toy_cmds(stubs, project)

    # 防线①：drop pause 消息 → 端到端 tick：consume_inbox 返回 paused → tick 在 sweep 之前早退
    loop._drop_message(sd, "pause")
    rc, _ = run_loop("tick", sd, "--driver-cmd", driver, "--judge-cmd", judge)
    cur = state.load_cursor(sd)
    evs = [e for e in _events(sd) if e.get("ev") == "watchdog_reclaim"]
    check("TC6 paused tick 退 0（no-op，sweep 之前早退）", "paused no-op", 0, rc)
    check("TC6 paused 下死 lease **保留**（不回收故意空转）", "lease kept under pause",
          True, cur.get("lease") is not None)
    check("TC6 paused 下无 watchdog_reclaim 事件", "no reclaim under pause", 0, len(evs))

    # 防线②：直接调 sweep（cur.paused 仍 True）→ sweep 内显式 paused 短路（belt-and-suspenders）
    sig = loop.sweep_stale_lease(sd, _stub_opts(stubs, project))
    check("TC6 sweep 内显式 paused 短路（返回 None）", "belt-and-suspenders", None, sig)
    check("TC6 第二道防线后 lease 仍保留", "still kept",
          True, state.load_cursor(sd).get("lease") is not None)


def tc7_release_on_completion():
    """TC7：正常跑 toy milestone → 每个推进相位结束 finally release lease；DONE 后无残留 lease。"""
    stubs = _copy_fixture_stubs(tempfile.mkdtemp(prefix="lhb-wd-stubs-"))
    project, sd = _mk_project([_toy_milestone("T1", "x")])
    _patch_probe(sd, "T1", "test -f %s/t1_done.txt" % project)
    driver, judge = _toy_cmds(stubs, project)

    # 跑一个 driver 相位（plan）：acquire→派活→finally release。
    rc, _ = run_loop("tick", sd, "--driver-cmd", driver, "--judge-cmd", judge)
    cur = state.load_cursor(sd)
    check("TC7 一个正常 tick 后 lease 被清（finally release）", "release on tick",
          None, cur.get("lease"))

    # 续跑到底：到 DONE 全程不应残留 lease
    for _ in range(20):
        run_loop("tick", sd, "--driver-cmd", driver, "--judge-cmd", judge)
        if state._find(state.load_milestones(sd), "T1")["status"] == "DONE":
            break
    cur = state.load_cursor(sd)
    check("TC7 跑到 DONE 后无残留 lease", "no residual lease", None, cur.get("lease"))
    check("TC7 T1 真到 DONE（释放路径没卡推进）", "still advances", "DONE",
          state._find(state.load_milestones(sd), "T1")["status"])


def tc8_backward_compat_no_lease():
    """TC8：旧式 cursor（无 lease 键，模拟旧式）→ sweep 零成本跳过、F5/F6 行为完全不变、能正常推进。"""
    stubs = _copy_fixture_stubs(tempfile.mkdtemp(prefix="lhb-wd-stubs-"))
    project, sd = _mk_project([_toy_milestone("T1", "x")])
    _patch_probe(sd, "T1", "test -f %s/t1_done.txt" % project)
    driver, judge = _toy_cmds(stubs, project)

    cur = state.load_cursor(sd)
    check("TC8 起始 cursor 无 lease 键", "no lease key", False, "lease" in cur)
    # sweep 直接调：无 lease → 零成本 None、不报错
    sig = loop.sweep_stale_lease(sd, _stub_opts(stubs, project))
    check("TC8 无 lease → sweep 返回 None（零成本跳过）", "no-op sweep", None, sig)

    # 一个普通 tick 仍正常推进（plan→plan_review），与不开 F7 时一致
    rc, _ = run_loop("tick", sd, "--driver-cmd", driver, "--judge-cmd", judge)
    m = state._find(state.load_milestones(sd), "T1")
    check("TC8 无 lease 历史下 tick 正常推进（plan→plan_review）", "F5/F6 不变",
          "plan_review", m["phase"])
    check("TC8 tick 退 0", "稳定态", 0, rc)


def tc9_cross_host_lease_dead():
    """TC9：lease host=other-host + 过期心跳 → 跨机 pid 无意义 → dead → reclaim（不靠 pid 兜活）。"""
    stubs = _copy_fixture_stubs(tempfile.mkdtemp(prefix="lhb-wd-stubs-"))
    project, sd = _mk_project([_toy_milestone("T1", "x")])
    _set_phase(sd, "T1", "IN_PROGRESS", "impl")
    # host 设成别的机器；pid 故意用 os.getpid()（本机在世）证明"跨机就不信 pid"
    _write_lease(sd, "T1", "impl", os.getpid(), host="some-other-host-xyz", age_over_ttl=True)

    sig = loop.sweep_stale_lease(sd, _stub_opts(stubs, project))
    cur = state.load_cursor(sd)
    evs = [e for e in _events(sd) if e.get("ev") == "watchdog_reclaim"]
    check("TC9 跨机 + 过期心跳 → 判死回收（lease 被清）", "cross-host dead",
          None, cur.get("lease"))
    check("TC9 跨机不靠本机 pid 兜活（记 reclaim 事件）", "no pid trust cross-host",
          True, len(evs) >= 1)
    check("TC9 sweep 返回 None（reclaim 后正常继续，非升级）", "reclaim not escalate",
          None, sig)


def tc10_reclaim_count_reset_on_advance():
    """TC10（ADOPT-3）：成功推进相位后 reclaim_count[mid] 被清零——reclaim 不跨成功累积、不误升级。

    构造：先人为给 T1 一个 reclaim_count，再正常跑一个推进相位（driver advance / gate-pass），
    断言推进成功后该计数归 0（与 _reset_infra 同款"成功即清瞬态计数"）。"""
    stubs = _copy_fixture_stubs(tempfile.mkdtemp(prefix="lhb-wd-stubs-"))
    project, sd = _mk_project([_toy_milestone("T1", "x")])
    _patch_probe(sd, "T1", "test -f %s/t1_done.txt" % project)
    driver, judge = _toy_cmds(stubs, project)

    # 人为塞一个 reclaim_count[T1]=2（模拟之前崩过两次被回收）
    cur = state.load_cursor(sd)
    cur["reclaim_count"] = {"T1": 2}
    state.save_cursor(sd, cur)

    # 正常跑一个推进相位（plan：driver 成功 → advance-phase）
    run_loop("tick", sd, "--driver-cmd", driver, "--judge-cmd", judge)
    rc_after = state.load_cursor(sd).get("reclaim_count", {}).get("T1", 0)
    check("TC10 成功推进后 reclaim_count[T1] 清零（不跨成功累积）", "reset on advance",
          0, rc_after)


def main():
    if not os.path.isdir(FIXTURE_SRC):
        print("FIXTURE MISSING: %s" % FIXTURE_SRC, file=sys.stderr)

    tcs = [tc1_dead_pid_reclaim_rerun, tc2_fresh_heartbeat_not_reclaimed,
           tc3_expired_hb_own_pid_alive_not_reclaimed, tc4_foreign_live_pid_not_reclaimed,
           tc5_reclaim_loop_escalates, tc6_paused_not_swept, tc7_release_on_completion,
           tc8_backward_compat_no_lease, tc9_cross_host_lease_dead,
           tc10_reclaim_count_reset_on_advance]
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
