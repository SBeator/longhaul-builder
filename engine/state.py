#!/usr/bin/env python3
"""longhaul-builder — 状态台账 / 程序计数器（确定性核心）。

设计立场（见 DESIGN.md §1, §2.2）：
- 状态全部外置：一次构建的真相住在 run 目录的文件里，agent 上下文只当草稿纸。
- 脚本固化执行：所有状态转移由本脚本"计算并写入"，AI 只产出内容（spec 文本 / 代码 / 证据），
  绝不让 AI 直接编辑状态文件、绝不靠 AI 心算下一步——这样每次启动是确定性地捡起 cursor 指的那一步。
- 熔断内建：claim 前先查 attempt_count，超 max_attempts 即 BLOCKED，绝不无限重启烧 token。

agent/基建无关：纯标准库，只读写本地文件 + git 由外层调度器负责。无任何公司基建依赖。
"""

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

# ---- 目录约定（一次构建 = 一个 run 目录）-----------------------------------

RUN_FILES = {
    "spec": "spec.md",            # 冻结需求（老化产物）
    "milestones": "milestones.json",
    "cursor": "cursor.json",      # 程序计数器
    "events": "events.jsonl",     # append-only 决策+事件流（审计/回放源）
}
EVIDENCE_DIR = "evidence"         # 每步验收证据，由确定性脚本写入
HANDOFF_DIR = "handoff"           # milestone 边界的压缩状态

PHASES = ("age", "plan", "build", "done", "blocked")  # cursor 全局粗粒度阶段（不变）
MS_STATUS = ("TODO", "IN_PROGRESS", "DONE", "BLOCKED", "SKIPPED")
# F2: milestone 内的两道门细粒度相位（与 status 同级，随 milestone 走，handoff 天然带着）。
MS_PHASES = ("plan", "plan_review", "impl", "impl_review", "done", "blocked")
DEFAULT_MAX_ATTEMPTS = 3


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _p(run_dir: str, key: str) -> str:
    return os.path.join(run_dir, RUN_FILES[key])


def _atomic_write(path: str, text: str) -> None:
    """写临时文件再 rename，保证读者永远看到完整内容（file-level 原子性）。"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _read_json(path: str, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: str, obj) -> None:
    _atomic_write(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


# ---- 事件流（append-only）---------------------------------------------------

def append_event(run_dir: str, etype: str, **data) -> None:
    rec = {"ts": _now(), "ev": etype, **data}
    with open(_p(run_dir, "events"), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ---- cursor（程序计数器）----------------------------------------------------

def load_cursor(run_dir: str) -> dict:
    return _read_json(_p(run_dir, "cursor"), {})


def save_cursor(run_dir: str, cursor: dict) -> None:
    cursor["updated_at"] = _now()
    _write_json(_p(run_dir, "cursor"), cursor)


# ---- milestones -------------------------------------------------------------

def _default_phase_for_status(status: str) -> str:
    """F2: 旧文件无 phase 时按 status 惰性推导（保守：旧 IN_PROGRESS 视为已在实施）。"""
    return {
        "DONE": "done",
        "BLOCKED": "blocked",
        "IN_PROGRESS": "impl",  # 旧 IN_PROGRESS 当作已在 impl，避免把进行中的活当成没出过方案
        "TODO": "plan",
    }.get(status, "plan")


def load_milestones(run_dir: str) -> list:
    """载入 milestones；对缺 phase 字段的旧文件惰性补默认（读取即归一化，不写盘）。"""
    milestones = _read_json(_p(run_dir, "milestones"), {"milestones": []})["milestones"]
    for m in milestones:
        if "phase" not in m or m.get("phase") is None:
            m["phase"] = _default_phase_for_status(m.get("status", "TODO"))
    return milestones


def save_milestones(run_dir: str, milestones: list) -> None:
    _write_json(_p(run_dir, "milestones"), {"milestones": milestones})


def _find(milestones: list, mid: str) -> dict:
    for m in milestones:
        if m["id"] == mid:
            return m
    raise KeyError(f"milestone not found: {mid}")


def _next_todo(milestones: list):
    """程序计数器：确定性返回下一个该做的 milestone（首个 TODO/IN_PROGRESS），无则 None。

    关键（F2 活循环重驱）：gate-fail(impl)/fail 后 milestone 留 status=IN_PROGRESS+phase=impl，
    仍被本函数重发——这是活循环把"门2打回的实现"再次派给 driver 的路径。
    """
    for m in milestones:
        if m["status"] in ("TODO", "IN_PROGRESS"):
            return m
    return None


# ---- F2 相位/计数 核心（计数真相集中在 _enter_impl 一处）---------------------

def _set_phase(m: dict, phase: str) -> None:
    """设置 milestone 相位（带合法性断言）。"""
    assert phase in MS_PHASES, f"bad phase: {phase}"
    m["phase"] = phase


def _mirror_active_phase(run_dir: str, milestones: list) -> dict:
    """把当前 active milestone 的 phase 镜像进 cursor.active_phase（只读派生，非真相源）。"""
    cur = load_cursor(run_dir)
    active = cur.get("active_milestone")
    ap = None
    if active is not None:
        for m in milestones:
            if m["id"] == active:
                ap = m.get("phase")
                break
    cur["active_phase"] = ap
    return cur


def _block(run_dir: str, milestones: list, m: dict, reason: str) -> int:
    """熔断：标 BLOCKED + phase=blocked + cursor 升级 + circuit_break 事件 + 退出码 3。"""
    m["status"] = "BLOCKED"
    _set_phase(m, "blocked")
    save_milestones(run_dir, milestones)
    cur = _mirror_active_phase(run_dir, milestones)
    cur["phase"] = "blocked"
    cur["next_action"] = f"{m['id']} 熔断升级人工：{reason}"
    save_cursor(run_dir, cur)
    append_event(run_dir, "circuit_break", milestone=m["id"],
                 attempt_count=m["attempt_count"], last_error=m.get("last_error"))
    print(f"CIRCUIT BREAK: {m['id']} attempts={m['attempt_count']} >= max={m['max_attempts']}; BLOCKED",
          file=sys.stderr)
    return 3


def _enter_impl(run_dir: str, milestones: list, m: dict) -> int:
    """进入 impl 相位——**唯一**的 attempt_count +1 点（A = 进过几次 impl）。

    两个调用方：门1 gate-pass(plan)（首次进实施）、门2 gate-fail(impl)/fail（重进实施）。
    +1 后查熔断：A >= max_attempts → BLOCKED 退出码 3（circuit_break 从此处触发，审计可读）。
    返回 0 = 正常进入 impl；3 = 熔断。
    """
    m["attempt_count"] += 1
    if m["attempt_count"] >= m["max_attempts"]:
        # 走到上限：这次进入 impl 即耗尽，直接熔断（不真正停在 impl）。
        return _block(run_dir, milestones, m, m.get("last_error") or "超 max_attempts")
    m["status"] = "IN_PROGRESS"
    _set_phase(m, "impl")
    return 0


# ---- 命令 -------------------------------------------------------------------

def cmd_init(args) -> int:
    run_dir = args.run_dir
    os.makedirs(os.path.join(run_dir, EVIDENCE_DIR), exist_ok=True)
    os.makedirs(os.path.join(run_dir, HANDOFF_DIR), exist_ok=True)
    _atomic_write(_p(run_dir, "spec"),
                  f"# spec（待老化）\n\n## 一句话需求\n{args.one_liner}\n")
    save_milestones(run_dir, [])
    save_cursor(run_dir, {
        "phase": "age",
        "active_milestone": None,
        "active_task": None,
        "next_action": "老化：把一句话需求 grill 成冻结 spec（P0 清零后等人确认）",
    })
    append_event(run_dir, "init", one_liner=args.one_liner)
    print(f"initialized run at {run_dir} (phase=age)")
    return 0


def cmd_set_milestones(args) -> int:
    """老化+人 P0 确认后调用：载入 milestones，phase→build，cursor 指向首个。"""
    run_dir = args.run_dir
    incoming = _read_json(args.file, None)
    if incoming is None:
        print(f"error: cannot read {args.file}", file=sys.stderr)
        return 2
    items = incoming["milestones"] if isinstance(incoming, dict) else incoming
    norm = []
    for m in items:
        status = m.get("status", "TODO")
        norm.append({
            "id": m["id"],
            "goal": m["goal"],
            "acceptance": m.get("acceptance", {}),  # {type, probe}
            "status": status,
            "phase": m.get("phase", _default_phase_for_status(status)),  # F2: 两道门相位
            "attempt_count": m.get("attempt_count", 0),
            "max_attempts": m.get("max_attempts", DEFAULT_MAX_ATTEMPTS),
            "last_error": m.get("last_error"),
        })
    save_milestones(run_dir, norm)
    nxt = _next_todo(norm)
    save_cursor(run_dir, {
        "phase": "build" if nxt else "done",
        "active_milestone": nxt["id"] if nxt else None,
        "active_phase": nxt.get("phase") if nxt else None,  # F2: 只读镜像
        "active_task": None,
        "next_action": (f"claim {nxt['id']} 并细化方案→TDD→实现→验收" if nxt
                        else "全部完成，进入交付"),
    })
    append_event(run_dir, "milestones_set", count=len(norm))
    print(f"set {len(norm)} milestones; phase={'build' if nxt else 'done'}")
    return 0


def cmd_next(args) -> int:
    """程序计数器查询：打印下一步该做什么（确定性）。"""
    milestones = load_milestones(args.run_dir)
    nxt = _next_todo(milestones)
    if nxt is None:
        blocked = [m for m in milestones if m["status"] == "BLOCKED"]
        print(json.dumps({"state": "blocked" if blocked else "done",
                          "blocked": [m["id"] for m in blocked]}, ensure_ascii=False))
        return 0
    print(json.dumps({"state": "actionable", "milestone": nxt}, ensure_ascii=False))
    return 0


def cmd_claim(args) -> int:
    """认领一个 milestone 干活 = 进入出方案（phase=plan）。

    F2 计数修正：claim **不再 +1**（attempt 只在进 impl 时 +1，见 _enter_impl）。
    幂等：对已 IN_PROGRESS 的同一 milestone 只刷新 cursor、**不改 phase、不改 A**（重唤起续跑，
    不双计、不把已在 impl 的活回退到 plan）。熔断守卫保留：进来时已耗尽 → BLOCKED 退出码 3。
    """
    run_dir = args.run_dir
    milestones = load_milestones(run_dir)
    m = _find(milestones, args.milestone)
    if m["attempt_count"] >= m["max_attempts"] and m["status"] != "DONE":
        return _block(run_dir, milestones, m, "超 max_attempts，升级人工")
    if m["status"] == "IN_PROGRESS":
        # 幂等 re-claim：保持 phase/A 不动，只刷新 cursor（重唤起在当前 attempt 内续跑）。
        save_milestones(run_dir, milestones)  # 顺带把惰性补的 phase 落盘
        cur = _mirror_active_phase(run_dir, milestones)
        cur["phase"] = "build"
        cur["active_milestone"] = m["id"]
        cur["next_action"] = f"{m['id']} 续跑（phase={m['phase']}, attempt {m['attempt_count']}）"
        save_cursor(run_dir, cur)
        append_event(run_dir, "claim", milestone=m["id"], attempt=m["attempt_count"], reclaim=True)
        print(f"re-claimed {m['id']} (idempotent; phase={m['phase']}, attempt {m['attempt_count']}/{m['max_attempts']})")
        return 0
    m["status"] = "IN_PROGRESS"
    _set_phase(m, "plan")  # 认领 = 进出方案；不 +1
    save_milestones(run_dir, milestones)
    cur = _mirror_active_phase(run_dir, milestones)
    cur["phase"] = "build"
    cur["active_milestone"] = m["id"]
    cur["next_action"] = f"{m['id']} 出方案→门1→实施→门2（attempt {m['attempt_count']}）"
    save_cursor(run_dir, cur)
    append_event(run_dir, "claim", milestone=m["id"], attempt=m["attempt_count"])
    print(f"claimed {m['id']} (phase=plan, attempt {m['attempt_count']}/{m['max_attempts']})")
    return 0


def cmd_complete(args) -> int:
    """验收通过：DONE + phase=done，cursor 推进到下一个 TODO（或 done）。

    终态推进：**任何相位**都允许直接 complete →DONE（兼容旧 claim→complete、已有项目续跑、
    手动收口）。门控调用方（F5 loop / gate-pass impl）只在 impl_review 门2 PASS 后才调它。
    """
    run_dir = args.run_dir
    milestones = load_milestones(run_dir)
    m = _find(milestones, args.milestone)
    m["status"] = "DONE"
    _set_phase(m, "done")
    m["last_error"] = None
    save_milestones(run_dir, milestones)
    nxt = _next_todo(milestones)
    save_cursor(run_dir, {
        "phase": "build" if nxt else "done",
        "active_milestone": nxt["id"] if nxt else None,
        "active_phase": nxt.get("phase") if nxt else None,  # F2: 只读镜像
        "active_task": None,
        "next_action": (f"claim {nxt['id']}" if nxt else "全部 milestone DONE，进入终态验收+交付"),
    })
    append_event(run_dir, "complete", milestone=m["id"])
    print(f"completed {m['id']}; next={'%s' % nxt['id'] if nxt else 'DONE'}")
    return 0


def _fail_impl(run_dir: str, milestones: list, m: dict, error: str) -> int:
    """门2 实现失败的核心：记 last_error，**重进 impl**（_enter_impl +1，达上限熔断）。

    新语义（F2）：失败后留 status=IN_PROGRESS+phase=impl（**不回 TODO**），让 _next_todo 仍重发
    该 milestone——活循环靠这条把"门2打回的实现"再次派给 driver 继续改。未达上限退 0，达上限退 3。
    """
    m["last_error"] = error
    rc = _enter_impl(run_dir, milestones, m)  # A+1 + 查熔断（重进 impl）
    if rc == 3:
        return 3  # 熔断（_enter_impl 已写盘 + 事件 + stderr）
    save_milestones(run_dir, milestones)
    cur = _mirror_active_phase(run_dir, milestones)
    cur["next_action"] = f"{m['id']} 门2打回，继续改实现（上次：{error}，attempt {m['attempt_count']}）"
    save_cursor(run_dir, cur)
    append_event(run_dir, "fail", milestone=m["id"], attempt=m["attempt_count"], error=error)
    print(f"failed {m['id']} (attempt {m['attempt_count']}/{m['max_attempts']}); re-driving impl")
    return 0


def cmd_fail(args) -> int:
    """验收未过（门2 实现失败）：留 IN_PROGRESS+impl 重驱、A+1，达上限→BLOCKED（退出码 3）。

    兼容旧调用 `fail M --error ...`：等价一次实现重试失败；旧"claim→fail 交替到上限"序列的
    最终熔断结果与退出码 3 契约不变（计数点从 claim 移到进 impl，终点一致）。
    """
    run_dir = args.run_dir
    milestones = load_milestones(run_dir)
    m = _find(milestones, args.milestone)
    return _fail_impl(run_dir, milestones, m, args.error)


# ---- F2 显式 gate 动词（Plan A）：loop.py 拿 1:1 verdict→verb 映射 -----------
# advance-phase（产物就绪交审）/ gate-pass（门放行）/ gate-fail（门打回）/ reopen-plan（逃生口）。
# 计数真相只在 _enter_impl；这些动词是"门+裁定"语义糖，复用 complete/_fail_impl/_enter_impl 核心。

def _illegal(milestone: str, cur_phase: str, verb: str) -> int:
    print(f"error: illegal transition: cannot '{verb}' from phase '{cur_phase}' (milestone {milestone})",
          file=sys.stderr)
    return 2


def cmd_advance_phase(args) -> int:
    """driver 产物就绪交审：plan→plan_review 或 impl→impl_review。不改 status/attempt。

    非法相位（在 plan_review/impl_review/done/blocked 上调）→ 退出码 2。
    """
    run_dir = args.run_dir
    milestones = load_milestones(run_dir)
    m = _find(milestones, args.milestone)
    cur_phase = m["phase"]
    if cur_phase == "plan":
        _set_phase(m, "plan_review")
    elif cur_phase == "impl":
        _set_phase(m, "impl_review")
    else:
        return _illegal(m["id"], cur_phase, "advance-phase")
    save_milestones(run_dir, milestones)
    cur = _mirror_active_phase(run_dir, milestones)
    cur["next_action"] = f"{m['id']} 交审：{m['phase']}"
    save_cursor(run_dir, cur)
    append_event(run_dir, "phase_advance", milestone=m["id"], phase=m["phase"])
    print(f"advanced {m['id']} → {m['phase']}")
    return 0


def cmd_gate_pass(args) -> int:
    """门放行。--gate plan: plan_review→impl（_enter_impl，**首次进 impl A+1**，达上限熔断）。
                --gate impl: impl_review→done（= complete，两门皆过收口推进 cursor）。"""
    run_dir = args.run_dir
    milestones = load_milestones(run_dir)
    m = _find(milestones, args.milestone)
    cur_phase = m["phase"]
    if args.gate == "plan":
        if cur_phase != "plan_review":
            return _illegal(m["id"], cur_phase, "gate-pass --gate plan")
        rc = _enter_impl(run_dir, milestones, m)  # A+1 + 查熔断
        if rc == 3:
            return 3
        save_milestones(run_dir, milestones)
        cur = _mirror_active_phase(run_dir, milestones)
        cur["next_action"] = f"{m['id']} 门1过→实施 TDD（attempt {m['attempt_count']}）"
        save_cursor(run_dir, cur)
        append_event(run_dir, "gate", milestone=m["id"], gate="plan", result="pass",
                     phase=m["phase"], attempt=m["attempt_count"])
        print(f"gate-pass plan: {m['id']} → impl (attempt {m['attempt_count']}/{m['max_attempts']})")
        return 0
    else:  # --gate impl
        if cur_phase != "impl_review":
            return _illegal(m["id"], cur_phase, "gate-pass --gate impl")
        append_event(run_dir, "gate", milestone=m["id"], gate="impl", result="pass")
        return cmd_complete(args)  # impl_review→done，复用 complete（推进 cursor）


def cmd_gate_fail(args) -> int:
    """门打回。--gate plan: plan_review→plan（= reopen-plan，**不+1**）。
                --gate impl: impl_review→impl（= fail，**A+1**，达上限熔断）。"""
    run_dir = args.run_dir
    milestones = load_milestones(run_dir)
    m = _find(milestones, args.milestone)
    cur_phase = m["phase"]
    if args.gate == "plan":
        if cur_phase != "plan_review":
            return _illegal(m["id"], cur_phase, "gate-fail --gate plan")
        return _do_reopen_plan(run_dir, milestones, m, args.error, gate="plan")
    else:  # --gate impl
        if cur_phase != "impl_review":
            return _illegal(m["id"], cur_phase, "gate-fail --gate impl")
        append_event(run_dir, "gate", milestone=m["id"], gate="impl", result="fail", error=args.error)
        return _fail_impl(run_dir, milestones, m, args.error)  # 回 impl, A+1, 达上限熔断


def _do_reopen_plan(run_dir, milestones, m, error, gate=None) -> int:
    """退回 plan 重开方案（门1 REVISE / 门2 REOPEN_PLAN 逃生口共用）。**不 +1**。"""
    cur_phase = m["phase"]
    if cur_phase not in ("plan_review", "impl_review"):
        return _illegal(m["id"], cur_phase, "reopen-plan")
    m["status"] = "IN_PROGRESS"
    _set_phase(m, "plan")
    if error is not None:
        m["last_error"] = error
    save_milestones(run_dir, milestones)
    cur = _mirror_active_phase(run_dir, milestones)
    cur["next_action"] = f"{m['id']} 退回重开方案（{error or '方案需修订'}）"
    save_cursor(run_dir, cur)
    append_event(run_dir, "reopen_plan", milestone=m["id"], from_gate=gate, error=error)
    print(f"reopen-plan: {m['id']} → plan (attempt unchanged {m['attempt_count']})")
    return 0


def cmd_reopen_plan(args) -> int:
    """任意 plan_review/impl_review → plan，**不+1**（门1 REVISE 与门2 REOPEN_PLAN 共用）。"""
    run_dir = args.run_dir
    milestones = load_milestones(run_dir)
    m = _find(milestones, args.milestone)
    return _do_reopen_plan(run_dir, milestones, m, args.error)


# ---- F8: P0 硬门（放行 build 前必须人确认 P0 清零，DESIGN §2.3 / spec D3）------------
# 机制：cursor.p0_confirmed（默认未设）。`p0-confirm` 由**人**显式置 true（这是 §1.5 必停门之一，
# 不可由 AI/driver 自动调）。loop.tick 派 build 活之前查 is_p0_confirmed，未确认即拒绝派活、喊人。
# 向后兼容（关键，别破坏 已有项目 的 .longhaul）：旧 cursor 无该 flag。判据 = 「已进 build 的 run
# 视为隐式已确认」——cursor.phase=='build' 或 已有 milestone 不再是初始(TODO/plan) 时默认放行；
# 只对**全新、尚未起步**的 run（phase 还在 age/plan、所有 milestone TODO@plan）才强制显式确认。

def is_p0_confirmed(run_dir: str, milestones=None, cursor=None) -> bool:
    """P0 是否已确认（显式 flag 优先，否则按「是否已进 build」惰性默认，保向后兼容）。"""
    cur = load_cursor(run_dir) if cursor is None else cursor
    if "p0_confirmed" in cur:
        return bool(cur["p0_confirmed"])
    # 旧 cursor 无 flag：已进 build / 已有 milestone 起过步 → 隐式已确认（不破坏 已有项目 续跑）。
    if cur.get("phase") == "build":
        return True
    ms = load_milestones(run_dir) if milestones is None else milestones
    for m in ms:
        if m.get("status") not in (None, "TODO") or m.get("phase") not in (None, "plan"):
            return True   # 任一 milestone 已不在初始态 → 这个 run 已起步 → 隐式已确认
    return False          # 全新、phase=age/plan、所有 milestone TODO@plan → 须显式 p0-confirm


# ---- F8: carry-forward 形式化（reviewer 非阻塞 nit → 下个 milestone 的输入）------------
# notes.md 是「跨 milestone 携带项」的单一事实源（DESIGN §2.4 / §2.8）。reviewer 的非阻塞 nit
# （PASS_WITH_NITS / APPROVE_WITH_CONDITIONS）不打回当前步，而是 append 进 notes.md 给后续步当输入。
# 两条写入路径，同一格式：① loop._maybe_carry 自动落（判官出条件/nit 时）② `state.py note` 人/脚本手动落。
# 格式（机器可 grep、人可读）：'## carry-forward（<mid> · <kind> · <时间>）\n> <text>\n'

NOTES_FILE = "notes.md"


def append_carry_forward(run_dir: str, mid: str, text: str, kind: str = "manual") -> None:
    """把一条 carry-forward note append 进 notes.md（统一格式，loop._maybe_carry 与 CLI 共用）。"""
    path = os.path.join(run_dir, NOTES_FILE)
    block = "\n## carry-forward（%s · %s · %s）\n> %s\n" % (mid, kind, _now(), text)
    with open(path, "a", encoding="utf-8") as f:
        f.write(block)
    append_event(run_dir, "carry_forward", milestone=mid, kind=kind, source="cli")


def cmd_note(args) -> int:
    """把一条 carry-forward note 记进 notes.md（reviewer 非阻塞 nit → 后续 milestone 输入的手动入口）。"""
    append_carry_forward(args.run_dir, args.milestone, args.text, kind=getattr(args, "kind", "manual"))
    print("noted carry-forward for %s → %s/%s" % (args.milestone, args.run_dir, NOTES_FILE))
    return 0


def cmd_p0_confirm(args) -> int:
    """人确认 P0 清零，放行进入 build（§1.5 必停门之一；不可由 AI 自动调）。

    幂等：重复确认 no-op（已 true 再确认仍 true）。写 cursor.p0_confirmed=true + p0_confirmed 事件。
    """
    run_dir = args.run_dir
    cur = load_cursor(run_dir)
    already = bool(cur.get("p0_confirmed"))
    cur["p0_confirmed"] = True
    if getattr(args, "by", None):
        cur["p0_confirmed_by"] = args.by
    cur["p0_confirmed_at"] = _now()
    save_cursor(run_dir, cur)
    append_event(run_dir, "p0_confirmed", by=getattr(args, "by", None), already=already)
    print("P0 confirmed%s; build may proceed" % (" (idempotent)" if already else ""))
    return 0


def cmd_status(args) -> int:
    run_dir = args.run_dir
    cursor = load_cursor(run_dir)
    milestones = load_milestones(run_dir)
    counts = {}
    for m in milestones:
        counts[m["status"]] = counts.get(m["status"], 0) + 1
    print(json.dumps({"phase": cursor.get("phase"),
                      "next_action": cursor.get("next_action"),
                      "milestones": len(milestones),
                      "by_status": counts}, ensure_ascii=False, indent=2))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="state.py", description="longhaul-builder 状态台账")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init"); s.add_argument("run_dir"); s.add_argument("--one-liner", required=True); s.set_defaults(fn=cmd_init)
    s = sub.add_parser("set-milestones"); s.add_argument("run_dir"); s.add_argument("--file", required=True); s.set_defaults(fn=cmd_set_milestones)
    s = sub.add_parser("next"); s.add_argument("run_dir"); s.set_defaults(fn=cmd_next)
    s = sub.add_parser("claim"); s.add_argument("run_dir"); s.add_argument("milestone"); s.set_defaults(fn=cmd_claim)
    s = sub.add_parser("complete"); s.add_argument("run_dir"); s.add_argument("milestone"); s.set_defaults(fn=cmd_complete)
    s = sub.add_parser("fail"); s.add_argument("run_dir"); s.add_argument("milestone"); s.add_argument("--error", required=True); s.set_defaults(fn=cmd_fail)
    s = sub.add_parser("status"); s.add_argument("run_dir"); s.set_defaults(fn=cmd_status)
    # F8: P0 硬门——人显式确认 P0 清零、放行 build（必停门之一，不可由 AI 自动调）。
    s = sub.add_parser("p0-confirm"); s.add_argument("run_dir"); s.add_argument("--by", default=None); s.set_defaults(fn=cmd_p0_confirm)
    # F8: carry-forward 形式化——把一条 reviewer 非阻塞 nit 记进 notes.md 供下个 milestone 当输入。
    s = sub.add_parser("note"); s.add_argument("run_dir"); s.add_argument("milestone"); s.add_argument("text"); s.add_argument("--kind", default="manual"); s.set_defaults(fn=cmd_note)

    # F2: 显式 gate 动词（Plan A）。旧动词全不动，这些是叠加。
    s = sub.add_parser("advance-phase"); s.add_argument("run_dir"); s.add_argument("milestone"); s.set_defaults(fn=cmd_advance_phase)
    s = sub.add_parser("gate-pass"); s.add_argument("run_dir"); s.add_argument("milestone"); s.add_argument("--gate", required=True, choices=("plan", "impl")); s.set_defaults(fn=cmd_gate_pass)
    s = sub.add_parser("gate-fail"); s.add_argument("run_dir"); s.add_argument("milestone"); s.add_argument("--gate", required=True, choices=("plan", "impl")); s.add_argument("--error", default=None); s.set_defaults(fn=cmd_gate_fail)
    s = sub.add_parser("reopen-plan"); s.add_argument("run_dir"); s.add_argument("milestone"); s.add_argument("--error", default=None); s.set_defaults(fn=cmd_reopen_plan)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
