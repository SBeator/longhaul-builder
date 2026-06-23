#!/usr/bin/env python3
"""F6 干预 inbox 的 dogfood 自测（TDD · AC6 · DESIGN §2.6）。

立场（与 test_loop.py 同骨架）：
- driver/judge 全用**本地 shell stub 脚本**（零网络、零 LLM、确定性）；loop 不关心命令内部。
- **红线**：所有测试一律在 `tempfile.mkdtemp()` 里造 state_dir + drop inbox 文件，
  **绝不**碰 LIVE 的 `本框架仓自己的 .longhaul`（那是本次构建的 cursor）。
- 四列证据表口径（用例｜输入｜loop 行为/真实结论｜是否一致）：表没填满不许标通过。

覆盖（plan §5 TC1–TC13 + prompts 单测）：
  TC1  pause → 下一 tick no-op（相位不前进、cursor.paused）
  TC2  resume → tick 恢复派活（相位前进）
  TC3  redirect 在 impl_review 相位 → reopen(走软上限) + note 写入 + attempt 不变
  TC4  abort → tick 立即停（ABORT_EXIT=5、run_aborted、cursor.aborted）；再 tick 仍停
  TC5  consumed-once / 归档（移到 processed/、inbox 空、不重复 apply）
  TC6  畸形消息隔离（移到 rejected/、不 crash、正常继续）
  TC7  ordering（pause→resume 同批，净效果 resume）
  TC8  redirect on DONE 保守（不自动 reopen；force:true 才退回）
  TC9  respec 留痕生效（spec 决策日志 + 事件 + cursor.respec_pending）
  TC10 半写文件（.tmp 前缀）不被消费
  TC11 ⭐ redirect 真改 driver 方向（录参 stub 断言渲染 prompt 含 instruction）
  TC12 ⭐ redirect 在 IN_PROGRESS@impl → note-append fallback、不 reopen、不退 2、不 crash
  TC13 ⭐ redirect 洪水 → replan cap 升级（exit 4，不 livelock）
  TCP  prompts 单测：note→{{redirect}} 渲染；无 note 渲染干净（无 UNSET / 无残留占位）

运行：python3 engine/test_inbox.py  → 退出码 0 全过 / 1 有不一致。
"""
import io
import json
import os
import shutil
import stat
import sys
import tempfile
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import state    # noqa: E402
import prompts  # noqa: E402
import loop      # noqa: E402  (RED 阶段缺 consume_inbox 等 → 预期的红)

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
    project = tempfile.mkdtemp(prefix="lhb-inbox-proj-")
    state_dir = os.path.join(project, ".longhaul")
    state.cmd_init(_NS(run_dir=state_dir, one_liner="玩具靶子（inbox 测试）"))
    msfile = os.path.join(project, "seed.json")
    with open(msfile, "w", encoding="utf-8") as f:
        json.dump({"milestones": seed_milestones}, f)
    state.cmd_set_milestones(_NS(run_dir=state_dir, file=msfile))
    state.main(["p0-confirm", state_dir, "--by", "test"])  # set-milestones 后默认未确认，显式放行（P0 门）
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


def _drop(state_dir, kind, **kw):
    """投递一条 inbox 消息（走与生产同一条原子写路径）。返回写入的文件名。

    用 loop 的 CLI/helper（生产投递口）保证测的就是真路径——若不存在则 RED。
    """
    return loop._drop_message(state_dir, kind, **kw)


def _set_phase(state_dir, mid, status, phase):
    """直接把某 milestone 置成指定 (status, phase)（测试构造用，不经状态机动词）。"""
    ms = state.load_milestones(state_dir)
    m = state._find(ms, mid)
    m["status"] = status
    m["phase"] = phase
    state.save_milestones(state_dir, ms)


def _stub_opts(stubs, project, judge="stub_judge_pass.sh", driver="stub_driver.sh",
               **over):
    o = {
        "driver_cmd": "bash %s/%s {mode} {state_dir} {milestone_id} %s" % (stubs, driver, project),
        "judge_cmd": "bash %s/%s {prompt_file}" % (stubs, judge),
        "driver_timeout": 600, "probe_timeout": 600, "review_timeout": 600,
        "max_infra_retries": 5, "max_replans": 5, "dry_run": False,
    }
    o.update(over)
    return o


# =========================== 测试用例 ===========================

def tc1_pause_noops():
    """TC1：drop pause → 下一 tick no-op（相位不前进、cursor.paused、tick_paused 事件、退 0）。"""
    stubs = _copy_fixture_stubs(tempfile.mkdtemp(prefix="lhb-inbox-stubs-"))
    project, sd = _mk_project([_toy_milestone("T1", "test -f %s/t1_done.txt" % "{project}")])
    _patch_probe(sd, "T1", "test -f %s/t1_done.txt" % project)
    driver = "bash %s/stub_driver.sh {mode} {state_dir} {milestone_id} %s" % (stubs, project)
    judge = "bash %s/stub_judge_pass.sh {prompt_file}" % stubs

    phase0 = state._find(state.load_milestones(sd), "T1")["phase"]
    _drop(sd, "pause")
    rc, _ = run_loop("tick", sd, "--driver-cmd", driver, "--judge-cmd", judge)
    m = state._find(state.load_milestones(sd), "T1")
    check("TC1 pause tick 退 0（no-op 稳定态）", "drop pause+tick", 0, rc)
    check("TC1 pause 后相位不前进（driver 没被调）", "phase 不动", phase0, m["phase"])
    check("TC1 cursor.paused==True", "paused 位", True,
          bool(state.load_cursor(sd).get("paused")))
    evs = [e["ev"] for e in _events(sd)]
    check("TC1 记 tick_paused 事件", "审计", True, "tick_paused" in evs)


def tc1b_pause_persists_across_ticks():
    """TC1b（回归）：pause → tick(no-op) → 再 tick（无新消息）必须仍 no-op、相位不前进。
    修复前 bug：pause 只挡收到 pause 那一拍，第二拍无新消息时会偷偷恢复派活。"""
    stubs = _copy_fixture_stubs(tempfile.mkdtemp(prefix="lhb-inbox-stubs-"))
    project, sd = _mk_project([_toy_milestone("T1", "test -f %s/t1_done.txt" % "{project}")])
    _patch_probe(sd, "T1", "test -f %s/t1_done.txt" % project)
    driver = "bash %s/stub_driver.sh {mode} {state_dir} {milestone_id} %s" % (stubs, project)
    judge = "bash %s/stub_judge_pass.sh {prompt_file}" % stubs

    phase0 = state._find(state.load_milestones(sd), "T1")["phase"]
    _drop(sd, "pause")
    run_loop("tick", sd, "--driver-cmd", driver, "--judge-cmd", judge)          # 第1拍：消费 pause
    rc2, _ = run_loop("tick", sd, "--driver-cmd", driver, "--judge-cmd", judge)  # 第2拍：无新消息
    m = state._find(state.load_milestones(sd), "T1")
    check("TC1b 第2拍(无新消息)仍 no-op 退 0", "pause→tick→tick", 0, rc2)
    check("TC1b 第2拍相位仍不前进(没偷偷复跑)", "phase 仍不动", phase0, m["phase"])
    check("TC1b cursor.paused 仍为 True（持久暂停）", "持久位", True,
          bool(state.load_cursor(sd).get("paused")))


def tc2_resume_proceeds():
    """TC2：pause→tick(no-op)→resume→tick → 恢复派活、相位前进（plan→plan_review）。"""
    stubs = _copy_fixture_stubs(tempfile.mkdtemp(prefix="lhb-inbox-stubs-"))
    project, sd = _mk_project([_toy_milestone("T1", "test -f %s/t1_done.txt" % "{project}")])
    _patch_probe(sd, "T1", "test -f %s/t1_done.txt" % project)
    driver = "bash %s/stub_driver.sh {mode} {state_dir} {milestone_id} %s" % (stubs, project)
    judge = "bash %s/stub_judge_pass.sh {prompt_file}" % stubs

    _drop(sd, "pause")
    run_loop("tick", sd, "--driver-cmd", driver, "--judge-cmd", judge)
    paused_phase = state._find(state.load_milestones(sd), "T1")["phase"]
    _drop(sd, "resume")
    run_loop("tick", sd, "--driver-cmd", driver, "--judge-cmd", judge)
    m = state._find(state.load_milestones(sd), "T1")
    check("TC2 resume 后相位前进（plan→plan_review）", "resume+tick",
          "plan_review", m["phase"])
    check("TC2 暂停期间相位确实没动过", "pause 真停", "plan", paused_phase)
    check("TC2 cursor.paused 已清", "resume 清位", False,
          bool(state.load_cursor(sd).get("paused")))


def tc3_redirect_reopen_review_phase():
    """TC3：对 IN_PROGRESS@impl_review 的 milestone drop redirect → note 写入 + 回 plan + 软上限 +1。"""
    stubs = _copy_fixture_stubs(tempfile.mkdtemp(prefix="lhb-inbox-stubs-"))
    project, sd = _mk_project([_toy_milestone("T1", "test -f %s/t1_done.txt" % "{project}")])
    _set_phase(sd, "T1", "IN_PROGRESS", "impl_review")
    a0 = state._find(state.load_milestones(sd), "T1")["attempt_count"]

    sig = loop.consume_inbox(sd, _stub_opts(stubs, project)) \
        if _drop(sd, "redirect", milestone="T1", instruction="改用 X 方案") else None
    m = state._find(state.load_milestones(sd), "T1")
    note_text = json.dumps(m.get("note") or [], ensure_ascii=False)
    check("TC3 note 含 instruction", "redirect note", True, "改用 X 方案" in note_text)
    check("TC3 review 相位 redirect → phase 回 plan", "reopen", "plan", m["phase"])
    check("TC3 reopen 不烧 attempt", "no burn", a0, m["attempt_count"])
    check("TC3 replan_count +1（走软上限）", "soft cap",
          1, state.load_cursor(sd).get("replan_count", {}).get("T1", 0))
    check("TC3 未触发升级（consume 返回 None）", "no escalate", None, sig)


def tc4_abort_stops():
    """TC4：drop abort → tick 立即停（ABORT_EXIT=5、run_aborted、cursor.aborted）；再 tick 仍停。"""
    stubs = _copy_fixture_stubs(tempfile.mkdtemp(prefix="lhb-inbox-stubs-"))
    project, sd = _mk_project([_toy_milestone("T1", "test -f %s/t1_done.txt" % "{project}")])
    _patch_probe(sd, "T1", "test -f %s/t1_done.txt" % project)
    driver = "bash %s/stub_driver.sh {mode} {state_dir} {milestone_id} %s" % (stubs, project)
    judge = "bash %s/stub_judge_pass.sh {prompt_file}" % stubs

    _drop(sd, "abort")
    rc, _ = run_loop("tick", sd, "--driver-cmd", driver, "--judge-cmd", judge)
    check("TC4 abort tick 退 ABORT_EXIT=5（独立人工码）", "abort", loop.ABORT_EXIT, rc)
    check("TC4 ABORT_EXIT 确为 5（区别于 infra/replan 的 4）", "码契约", 5, loop.ABORT_EXIT)
    check("TC4 cursor.aborted==True", "aborted 位", True,
          bool(state.load_cursor(sd).get("aborted")))
    evs = [e["ev"] for e in _events(sd)]
    check("TC4 记 run_aborted 独立事件", "审计", True, "run_aborted" in evs)
    # 再 tick：仍立即终止，不派活、相位不前进
    phase_before = state._find(state.load_milestones(sd), "T1")["phase"]
    rc2, _ = run_loop("tick", sd, "--driver-cmd", driver, "--judge-cmd", judge)
    check("TC4 abort 后再 tick 仍退 5（持续停）", "sticky", loop.ABORT_EXIT, rc2)
    check("TC4 abort 后相位仍不前进", "no dispatch", phase_before,
          state._find(state.load_milestones(sd), "T1")["phase"])


def tc5_consumed_once_archived():
    """TC5：drop pause → 消费后文件移到 processed/、inbox/ 空；再 tick 不重复 apply。"""
    stubs = _copy_fixture_stubs(tempfile.mkdtemp(prefix="lhb-inbox-stubs-"))
    project, sd = _mk_project([_toy_milestone("T1", "test -f %s/t1_done.txt" % "{project}")])
    _patch_probe(sd, "T1", "test -f %s/t1_done.txt" % project)
    driver = "bash %s/stub_driver.sh {mode} {state_dir} {milestone_id} %s" % (stubs, project)
    judge = "bash %s/stub_judge_pass.sh {prompt_file}" % stubs

    fn = _drop(sd, "pause")
    run_loop("tick", sd, "--driver-cmd", driver, "--judge-cmd", judge)
    inbox = os.path.join(sd, "inbox")
    processed = os.path.join(inbox, "processed")
    remaining = [f for f in os.listdir(inbox)
                 if os.path.isfile(os.path.join(inbox, f)) and not f.startswith(".")]
    check("TC5 消费后 inbox/ 顶层无待处理消息", "inbox 空", [], remaining)
    check("TC5 消息归档到 processed/", "archived", True,
          os.path.isdir(processed) and len(os.listdir(processed)) >= 1)
    consumed1 = [e for e in _events(sd) if e.get("ev") == "inbox_consumed"]
    # 再 tick：不应再消费一次（文件已移走）
    run_loop("tick", sd, "--driver-cmd", driver, "--judge-cmd", judge)
    consumed2 = [e for e in _events(sd) if e.get("ev") == "inbox_consumed"]
    check("TC5 第二次 tick 不重复消费（consumed-once）", "no re-apply",
          len(consumed1), len(consumed2))
    check("TC5 该 pause 恰被消费 1 次", "exactly once", 1, len(consumed1))


def tc6_malformed_quarantined():
    """TC6：drop 非法 JSON / kind=bogus → 移到 rejected/、记 inbox_rejected、tick 不 crash、继续派活。"""
    stubs = _copy_fixture_stubs(tempfile.mkdtemp(prefix="lhb-inbox-stubs-"))
    project, sd = _mk_project([_toy_milestone("T1", "test -f %s/t1_done.txt" % "{project}")])
    _patch_probe(sd, "T1", "test -f %s/t1_done.txt" % project)
    driver = "bash %s/stub_driver.sh {mode} {state_dir} {milestone_id} %s" % (stubs, project)
    judge = "bash %s/stub_judge_pass.sh {prompt_file}" % stubs

    inbox = os.path.join(sd, "inbox")
    os.makedirs(inbox, exist_ok=True)
    # 非法 JSON 直接落一个文件（不走 helper），名字合法但内容坏
    with open(os.path.join(inbox, "20260623T000000Z-bad-aaaa.json"), "w") as f:
        f.write("{ this is not valid json ::::")
    _drop(sd, "bogus_kind_msg") if False else None
    # kind 非法（合法 JSON 但 kind 不在白名单）
    with open(os.path.join(inbox, "20260623T000001Z-bog-bbbb.json"), "w") as f:
        json.dump({"kind": "bogus"}, f)

    rc, _ = run_loop("tick", sd, "--driver-cmd", driver, "--judge-cmd", judge)
    rejected = os.path.join(inbox, "rejected")
    check("TC6 坏消息不 crash tick（退 0 正常派活）", "no crash", 0, rc)
    check("TC6 畸形消息移到 rejected/", "quarantine", True,
          os.path.isdir(rejected) and len(os.listdir(rejected)) >= 2)
    evs = [e["ev"] for e in _events(sd)]
    check("TC6 记 inbox_rejected 事件", "审计", True, "inbox_rejected" in evs)
    # tick 仍正常派活：T1 应已前进出 plan（plan→plan_review）
    check("TC6 坏消息隔离后 tick 仍正常派活", "still dispatch",
          "plan_review", state._find(state.load_milestones(sd), "T1")["phase"])


def tc7_ordering():
    """TC7：同批 drop pause(t0)+resume(t1) → 两条都消费、按文件名序、净效果 resume（tick 正常跑）。"""
    stubs = _copy_fixture_stubs(tempfile.mkdtemp(prefix="lhb-inbox-stubs-"))
    project, sd = _mk_project([_toy_milestone("T1", "test -f %s/t1_done.txt" % "{project}")])
    _patch_probe(sd, "T1", "test -f %s/t1_done.txt" % project)
    driver = "bash %s/stub_driver.sh {mode} {state_dir} {milestone_id} %s" % (stubs, project)
    judge = "bash %s/stub_judge_pass.sh {prompt_file}" % stubs

    _drop(sd, "pause", _name="20260623T100000Z-pause-aaaa.json")
    _drop(sd, "resume", _name="20260623T100001Z-resume-bbbb.json")
    run_loop("tick", sd, "--driver-cmd", driver, "--judge-cmd", judge)
    m = state._find(state.load_milestones(sd), "T1")
    check("TC7 同批 pause→resume 净效果 resume（tick 正常派活）", "net resume",
          "plan_review", m["phase"])
    check("TC7 cursor.paused 终态为假", "final unpaused", False,
          bool(state.load_cursor(sd).get("paused")))
    # 事件序：pause_consumed 在 resume_consumed 之前
    kinds = [e.get("kind") for e in _events(sd) if e.get("ev") == "inbox_consumed"]
    check("TC7 消费序 pause→resume（按文件名）", "ordering",
          ["pause", "resume"], kinds)


def tc8_redirect_done_conservative():
    """TC8：对 DONE milestone drop redirect（无 force）→ 不自动 reopen；带 force:true 才退回 plan。"""
    stubs = _copy_fixture_stubs(tempfile.mkdtemp(prefix="lhb-inbox-stubs-"))
    project, sd = _mk_project([_toy_milestone("T1", "test -f %s/t1_done.txt" % "{project}")])
    _set_phase(sd, "T1", "DONE", "done")

    _drop(sd, "redirect", milestone="T1", instruction="想重做 T1")
    loop.consume_inbox(sd, _stub_opts(stubs, project))
    m = state._find(state.load_milestones(sd), "T1")
    check("TC8 DONE redirect 无 force → status 仍 DONE", "conservative", "DONE", m["status"])
    note_text = json.dumps(m.get("note") or [], ensure_ascii=False)
    check("TC8 仍 append note（留痕）", "note", True, "想重做 T1" in note_text)
    evs = [e["ev"] for e in _events(sd)]
    check("TC8 记 redirect_on_done 事件", "审计", True, "redirect_on_done" in evs)
    # 带 force:true 再投 → 真退回 plan
    _drop(sd, "redirect", milestone="T1", instruction="强制重做", force=True)
    loop.consume_inbox(sd, _stub_opts(stubs, project))
    m = state._find(state.load_milestones(sd), "T1")
    check("TC8 force:true → DONE 退回 IN_PROGRESS@plan", "force reopen",
          ("IN_PROGRESS", "plan"), (m["status"], m["phase"]))


def tc9_respec_logged():
    """TC9：drop respec{instruction} → spec.md 出现干预记录段含 instruction + respec_requested 事件 +
    cursor.respec_pending；milestone 相位未被强改。"""
    stubs = _copy_fixture_stubs(tempfile.mkdtemp(prefix="lhb-inbox-stubs-"))
    project, sd = _mk_project([_toy_milestone("T1", "test -f %s/t1_done.txt" % "{project}")])
    phase0 = state._find(state.load_milestones(sd), "T1")["phase"]

    _drop(sd, "respec", instruction="把验收探针放宽到 smoke 级")
    loop.consume_inbox(sd, _stub_opts(stubs, project))
    spec_text = open(os.path.join(sd, "spec.md"), encoding="utf-8").read()
    check("TC9 spec.md 出现 instruction（留痕不覆盖）", "spec log", True,
          "把验收探针放宽到 smoke 级" in spec_text)
    evs = [e["ev"] for e in _events(sd)]
    check("TC9 记 respec_requested 事件", "审计", True, "respec_requested" in evs)
    check("TC9 cursor.respec_pending==True", "信号", True,
          bool(state.load_cursor(sd).get("respec_pending")))
    check("TC9 respec 不强改 milestone 相位（非阻塞）", "no phase change",
          phase0, state._find(state.load_milestones(sd), "T1")["phase"])


def tc10_halfwrite_skipped():
    """TC10：inbox/ 里放一个 .tmp 前缀文件（半写）→ listdir 跳过、不当消息、不 crash。"""
    stubs = _copy_fixture_stubs(tempfile.mkdtemp(prefix="lhb-inbox-stubs-"))
    project, sd = _mk_project([_toy_milestone("T1", "test -f %s/t1_done.txt" % "{project}")])
    _patch_probe(sd, "T1", "test -f %s/t1_done.txt" % project)
    driver = "bash %s/stub_driver.sh {mode} {state_dir} {milestone_id} %s" % (stubs, project)
    judge = "bash %s/stub_judge_pass.sh {prompt_file}" % stubs

    inbox = os.path.join(sd, "inbox")
    os.makedirs(inbox, exist_ok=True)
    tmp_path = os.path.join(inbox, ".tmp-half-写到一半.json")
    with open(tmp_path, "w") as f:
        f.write('{"kind": "abort"')   # 半截 + 是 .tmp 前缀
    rc, _ = run_loop("tick", sd, "--driver-cmd", driver, "--judge-cmd", judge)
    check("TC10 半写 .tmp 文件不被消费（tick 不 crash、不 abort）", "skipped", 0, rc)
    check("TC10 .tmp 文件原地保留（没被当消息归档/隔离）", "left in place",
          True, os.path.exists(tmp_path))
    check("TC10 cursor 未被 .tmp 误改成 aborted", "no false abort", False,
          bool(state.load_cursor(sd).get("aborted")))


def tc11_redirect_reaches_driver():
    """TC11 ⭐：redirect 后再驱动一拍，录参 driver stub 把渲染后的 prompt 落 sentinel 文件，
    断言其中**真含** instruction（证明 note→{{redirect}}→driver 闭环，不靠 driver 自述）。"""
    stubs = _copy_fixture_stubs(tempfile.mkdtemp(prefix="lhb-inbox-stubs-"))
    project, sd = _mk_project([_toy_milestone("T1", "test -f %s/t1_done.txt" % "{project}")])
    _patch_probe(sd, "T1", "test -f %s/t1_done.txt" % project)
    # 把 T1 置 IN_PROGRESS@plan（driver 下拍会在 plan 相位被调、读到 redirect）
    _set_phase(sd, "T1", "IN_PROGRESS", "plan")

    # 录参 driver stub：把收到的 {prompt_file} 内容 cat 到 sentinel 文件
    seen = os.path.join(sd, "_driver_seen.txt")
    rec_stub = os.path.join(stubs, "stub_driver_record.sh")
    with open(rec_stub, "w") as f:
        f.write("#!/usr/bin/env bash\nset -eu\ncat \"$1\" > \"%s\"\nexit 0\n" % seen)
    os.chmod(rec_stub, os.stat(rec_stub).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    driver = "bash %s {prompt_file}" % rec_stub
    judge = "bash %s/stub_judge_pass.sh {prompt_file}" % stubs

    INSTR = "改用 X 方案：换成基于事件溯源的实现"
    _drop(sd, "redirect", milestone="T1", instruction=INSTR)
    run_loop("tick", sd, "--driver-cmd", driver, "--judge-cmd", judge)
    seen_text = open(seen, encoding="utf-8").read() if os.path.exists(seen) else ""
    check("TC11 ⭐ driver 渲染后的 prompt 真含 redirect instruction", "note→driver 闭环",
          True, INSTR in seen_text)
    # 反作弊：确认 sentinel 真被这拍写过（driver 真被调）
    check("TC11 driver 真被调（sentinel 非空）", "driver invoked",
          True, len(seen_text) > 50)


def tc12_redirect_impl_note_fallback():
    """TC12 ⭐：milestone 置 IN_PROGRESS@impl，drop redirect → note-append fallback：
    不调 reopen-plan、phase 仍 impl、note 含 instruction、不退 2、不 crash。"""
    stubs = _copy_fixture_stubs(tempfile.mkdtemp(prefix="lhb-inbox-stubs-"))
    project, sd = _mk_project([_toy_milestone("T1", "test -f %s/t1_done.txt" % "{project}")])
    _set_phase(sd, "T1", "IN_PROGRESS", "impl")
    a0 = state._find(state.load_milestones(sd), "T1")["attempt_count"]

    crashed = False
    sig = None
    try:
        _drop(sd, "redirect", milestone="T1", instruction="impl 阶段换做法：改用流式写")
        sig = loop.consume_inbox(sd, _stub_opts(stubs, project))
    except Exception:
        crashed = True
    m = state._find(state.load_milestones(sd), "T1")
    check("TC12 ⭐ impl 相位 redirect 不 crash", "no crash", False, crashed)
    check("TC12 phase 仍 impl（不撞非法 reopen 退 plan）", "no reopen", "impl", m["phase"])
    note_text = json.dumps(m.get("note") or [], ensure_ascii=False)
    check("TC12 note 含 instruction（fallback 留痕）", "note append", True,
          "impl 阶段换做法：改用流式写" in note_text)
    # 关键：不出现 reopen_plan 事件（没有把 illegal-2 当成功吞掉）
    evs = [e["ev"] for e in _events(sd)]
    check("TC12 不调 reopen-plan（无 reopen_plan 事件）", "no illegal-2",
          False, "reopen_plan" in evs)
    check("TC12 consume 未退升级码（None）", "no escalate", None, sig)
    check("TC12 attempt 不变", "no burn", a0, m["attempt_count"])


def tc13_redirect_flood_cap():
    """TC13 ⭐：对 @impl_review 的 milestone 连投 max_replans 条 redirect、每条后驱到 review 相位 →
    撞 max_replans 后升级（mid 进 infra_blocked、replan_break、消费/tick 返回 4，不 livelock）。"""
    stubs = _copy_fixture_stubs(tempfile.mkdtemp(prefix="lhb-inbox-stubs-"))
    project, sd = _mk_project([_toy_milestone("T1", "test -f %s/t1_done.txt" % "{project}",
                                              max_attempts=99)])
    MAXR = 3
    driver = _stub_opts(stubs, project)["driver_cmd"]
    judge = _stub_opts(stubs, project)["judge_cmd"]
    last_rc = None
    for i in range(MAXR + 1):
        # 每轮把 milestone 强置回 impl_review（模拟 driver 重出方案又交审）
        _set_phase(sd, "T1", "IN_PROGRESS", "impl_review")
        _drop(sd, "redirect", milestone="T1", instruction="洪水 redirect #%d" % i)
        # 经真 tick：撞上限那拍 consume 返回 'escalate' → tick 退 4（端到端验退出码契约）
        last_rc, _ = run_loop("tick", sd, "--max-replans", str(MAXR),
                              "--driver-cmd", driver, "--judge-cmd", judge)
        cur = state.load_cursor(sd)
        if "T1" in (cur.get("infra_blocked") or []):
            break
    cur = state.load_cursor(sd)
    check("TC13 ⭐ replan_count 累到软上限", "≥%d" % MAXR, True,
          cur.get("replan_count", {}).get("T1", 0) >= MAXR)
    check("TC13 撞上限 → mid 进 infra_blocked（升级）", "escalate", True,
          "T1" in (cur.get("infra_blocked") or []))
    check("TC13 撞上限那拍 tick 返回升级码 4（不 livelock）", "exit 4", 4, last_rc)
    evs = [e["ev"] for e in _events(sd)]
    check("TC13 记 replan_break 事件", "审计", True, "replan_break" in evs)
    # 升级后再 tick：该 mid 被过滤跳过，不再被无限踢（无 actionable 或不再 reopen）
    _drop(sd, "redirect", milestone="T1", instruction="升级后再投")
    rc, _ = run_loop("tick", sd, "--max-replans", str(MAXR),
                     "--driver-cmd", _stub_opts(stubs, project)["driver_cmd"],
                     "--judge-cmd", _stub_opts(stubs, project)["judge_cmd"])
    check("TC13 升级后 tick 不再 livelock（退 0 idle 或 4）", "no livelock",
          True, rc in (0, 4))


def tc14_redirect_on_needs_confirm():
    """TC14（回归）：redirect 打到 NEEDS_CONFIRM 的 milestone → 视同 reject+换方向：回 IN_PROGRESS@plan、
    带 note、清旗。修复"redirect 被静默吞、永不到 driver"（NEEDS_CONFIRM 永不被 _next_todo 选中）。"""
    stubs = _copy_fixture_stubs(tempfile.mkdtemp(prefix="lhb-inbox-stubs-"))
    project, sd = _mk_project([_toy_milestone("T1", "test -f %s/t1_done.txt" % "{project}")])
    _set_phase(sd, "T1", "NEEDS_CONFIRM", "impl")
    ms = state.load_milestones(sd)
    state._find(ms, "T1")["flag"] = {"kind": "blocked-workaround", "summary": "卡住了"}
    state.save_milestones(sd, ms)

    sig = loop.consume_inbox(sd, _stub_opts(stubs, project)) \
        if _drop(sd, "redirect", milestone="T1", instruction="改用方案B") else None
    m = state._find(state.load_milestones(sd), "T1")
    note_text = json.dumps(m.get("note") or [], ensure_ascii=False)
    check("TC14 redirect 打 NEEDS_CONFIRM → 回 IN_PROGRESS@plan（不再被吞）", "reopen",
          True, m["status"] == "IN_PROGRESS" and m["phase"] == "plan")
    check("TC14 redirect 指示进了 note（→ 会渲染给 driver）", "note", True, "改用方案B" in note_text)
    check("TC14 清掉了举旗 flag", "flag cleared", True, "flag" not in m)
    check("TC14 consume 不升级（返回 None）", "no escalate", None, sig)
    evs = [e["ev"] for e in _events(sd)]
    check("TC14 记 redirect_on_needs_confirm 事件", "审计", True, "redirect_on_needs_confirm" in evs)


def tcP_prompts_redirect_placeholder():
    """TCP：prompts 单测——note→{{redirect}} 渲染出来；无 note 渲染干净（无 UNSET / 无残留占位）。"""
    INSTR = "人工 redirect：改用消息队列"
    m_with_note = {
        "id": "M1", "goal": "目标 G",
        "acceptance": {"type": "tdd", "probe": "test"},
        "note": [{"id": "r1", "ts": "20260623T0000Z", "text": INSTR}],
    }
    m_no_note = {
        "id": "M2", "goal": "目标 H",
        "acceptance": {"type": "tdd", "probe": "test"},
    }
    ctx = {"project_path": "/tmp/x", "state_dir": "/tmp/x/.longhaul",
           "carry_forward": "", "mode": "plan-only"}
    out_with = prompts.render(m_with_note, "driver", ctx)
    out_without = prompts.render(m_no_note, "driver", ctx)
    check("TCP note→driver 渲染含 instruction", "{{redirect}} 填充", True, INSTR in out_with)
    check("TCP 无 note 时无 [[redirect:UNSET]] 残留", "空串降级", False,
          "[[redirect:UNSET]]" in out_without)
    check("TCP 无 note 时无 {{redirect}} 字面残留", "无占位残留", False,
          "{{redirect}}" in out_without)
    # 既有占位仍正常（goal 填充、无残留），不破坏 F1
    check("TCP goal 仍正常填充（不破坏 F1）", "F1 兼容", True,
          "目标 H" in out_without and "{{goal}}" not in out_without)


def main():
    if not os.path.isdir(FIXTURE_SRC):
        print("FIXTURE MISSING: %s" % FIXTURE_SRC, file=sys.stderr)

    tcs = [tcP_prompts_redirect_placeholder,
           tc1_pause_noops, tc1b_pause_persists_across_ticks,
           tc2_resume_proceeds, tc3_redirect_reopen_review_phase,
           tc4_abort_stops, tc5_consumed_once_archived, tc6_malformed_quarantined,
           tc7_ordering, tc8_redirect_done_conservative, tc9_respec_logged,
           tc10_halfwrite_skipped, tc11_redirect_reaches_driver,
           tc12_redirect_impl_note_fallback, tc13_redirect_flood_cap,
           tc14_redirect_on_needs_confirm]
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
