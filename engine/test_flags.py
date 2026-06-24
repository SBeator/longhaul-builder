#!/usr/bin/env python3
"""test_flags.py —— D 簇 dogfood 自测：非阻塞举旗 + 异步确认 + 回插（NEEDS_CONFIRM）。

四列证据表：用例 | 输入 | 实际是否符合预期。验 D 的命脉：
- driver 举旗 → milestone 标 NEEDS_CONFIRM、**cursor 非阻塞推进到下一个**（不硬停 BLOCKED）；
- 收尾守门：有未确认举旗 → cmd_next 报 needs_confirm（不蒙混成 done）；
- 人回插 confirm/reject/resolve 三条路由正确；loop 检测 flag.json + inbox 回插端到端。
"""
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import state  # noqa: E402
import loop    # noqa: E402

_rows = []


def check(name, ok):
    _rows.append(ok)
    print(("  ✓ " if ok else "  ✗ ") + name)


def _fresh(ms):
    sd = tempfile.mkdtemp(prefix="lhb-flags-")
    state.main(["init", sd, "--one-liner", "x"])
    mfile = os.path.join(sd, "ms.json")
    with open(mfile, "w", encoding="utf-8") as f:
        json.dump({"milestones": ms}, f, ensure_ascii=False)
    state.main(["set-milestones", sd, "--file", mfile])
    state.main(["p0-confirm", sd, "--by", "tester"])
    return sd


def _m(mid, status, phase):
    return {"id": mid, "goal": "%s 目标" % mid, "acceptance": {"type": "tdd"},
            "status": status, "phase": phase, "attempt_count": 1, "max_attempts": 3}


def _cap(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        state.main(argv)
    return buf.getvalue()


def main():
    # === 举旗非阻塞：M1(impl) 举旗 → NEEDS_CONFIRM，cursor 推进到 M2 ===
    sd = _fresh([_m("M1", "IN_PROGRESS", "impl"), _m("M2", "TODO", "plan")])
    state.main(["flag", sd, "M1", "--kind", "spec-divergence", "--summary", "改用了更优的 X"])
    ms = {m["id"]: m for m in state.load_milestones(sd)}
    cur = state.load_cursor(sd)
    check("举旗后 M1 = NEEDS_CONFIRM", ms["M1"]["status"] == "NEEDS_CONFIRM")
    check("举旗记下了 kind/summary", ms["M1"].get("flag", {}).get("kind") == "spec-divergence")
    check("非阻塞：cursor 推进到下一个 M2（不停在 M1）", cur.get("active_milestone") == "M2")
    check("举旗保留 p0_confirmed（没整体重写 cursor）", cur.get("p0_confirmed") is True)

    # === 收尾守门：唯一非 DONE 的是 NEEDS_CONFIRM → cmd_next 报 needs_confirm（不报 done）===
    sd2 = _fresh([_m("M1", "NEEDS_CONFIRM", "impl"), _m("M2", "DONE", "done")])
    nxt = json.loads(_cap(["next", sd2]))
    check("有未确认举旗 → cmd_next state=needs_confirm（不蒙混成 done）", nxt["state"] == "needs_confirm")
    check("needs_confirm 列出 M1", nxt.get("needs_confirm") == ["M1"])

    # === confirm（场景2接受）：NEEDS_CONFIRM → DONE 推进 ===
    sd3 = _fresh([_m("M1", "NEEDS_CONFIRM", "impl"), _m("M2", "TODO", "plan")])
    state.main(["confirm", sd3, "M1"])
    m1 = {m["id"]: m for m in state.load_milestones(sd3)}["M1"]
    check("confirm → M1 DONE", m1["status"] == "DONE")
    check("confirm 清掉 flag", "flag" not in m1)
    _ev = [json.loads(l) for l in open(os.path.join(sd3, "events.jsonl"), encoding="utf-8")
           if l.strip()]
    _fc = [e for e in _ev if e["ev"] == "flag_confirmed"]
    check("confirm 审计留痕 gate2_bypassed=True（举旗步未过门2，透明化）",
          bool(_fc) and _fc[-1].get("gate2_bypassed") is True)

    # === reject（场景2驳回）：NEEDS_CONFIRM → 回 plan 重做、push note ===
    sd4 = _fresh([_m("M1", "NEEDS_CONFIRM", "impl"), _m("M2", "TODO", "plan")])
    state.main(["reject", sd4, "M1", "--instruction", "回到最初的原生方案"])
    m1 = {m["id"]: m for m in state.load_milestones(sd4)}["M1"]
    check("reject → M1 回 IN_PROGRESS@plan", m1["status"] == "IN_PROGRESS" and m1["phase"] == "plan")
    check("reject 把'回原方案'指示 push 进 note（→driver redirect）",
          any("回到最初的原生方案" in (n.get("text") or "") for n in (m1.get("note") or [])))

    # === resolve（场景1）：NEEDS_CONFIRM → 回 impl 带提示重跑 ===
    sd5 = _fresh([_m("M1", "NEEDS_CONFIRM", "impl"), _m("M2", "TODO", "plan")])
    state.main(["resolve", sd5, "M1", "--instruction", "依赖装好了，按这个继续"])
    m1 = {m["id"]: m for m in state.load_milestones(sd5)}["M1"]
    check("resolve → M1 回 IN_PROGRESS@impl", m1["status"] == "IN_PROGRESS" and m1["phase"] == "impl")
    check("resolve 把提示 push 进 note", any("依赖装好了" in (n.get("text") or "") for n in (m1.get("note") or [])))

    # === cmd_flags：列出待确认举旗 ===
    sd6 = _fresh([_m("M1", "NEEDS_CONFIRM", "impl"), _m("M2", "TODO", "plan")])
    state.load_milestones(sd6)  # ensure
    flags = json.loads(_cap(["flags", sd6]))
    check("cmd_flags 列出 NEEDS_CONFIRM 的 M1", [p["id"] for p in flags["pending"]] == ["M1"])

    # === loop._detect_and_raise_flag：driver 写 flag.json → 检测到 → NEEDS_CONFIRM + 归档旗子 ===
    sd7 = _fresh([_m("M1", "IN_PROGRESS", "impl"), _m("M2", "TODO", "plan")])
    ev = os.path.join(sd7, "evidence", "M1")
    os.makedirs(ev, exist_ok=True)
    with open(os.path.join(ev, "flag.json"), "w", encoding="utf-8") as f:
        json.dump({"kind": "blocked-workaround", "summary": "缺凭证，先跳过"}, f, ensure_ascii=False)
    raised = loop._detect_and_raise_flag(sd7, "M1")
    m1 = {m["id"]: m for m in state.load_milestones(sd7)}["M1"]
    check("loop 检测到 flag.json → True", raised is True)
    check("loop 据 flag.json 标 M1 NEEDS_CONFIRM", m1["status"] == "NEEDS_CONFIRM")
    check("loop 归档了 flag.json（不再原名存在，免重触发）",
          not os.path.exists(os.path.join(ev, "flag.json")))

    # === inbox 端到端：人发 confirm 消息 → consume_inbox → M1 DONE ===
    sd8 = _fresh([_m("M1", "NEEDS_CONFIRM", "impl"), _m("M2", "DONE", "done")])
    loop._drop_message(sd8, "confirm", milestone="M1")
    loop.consume_inbox(sd8, {"max_replans": 5})
    m1 = {m["id"]: m for m in state.load_milestones(sd8)}["M1"]
    check("inbox confirm 端到端 → M1 DONE", m1["status"] == "DONE")

    # === item2c 减返工：_phase_plan 也检测 plan 期举旗 → NEEDS_CONFIRM（早举旗、不进门1/impl）===
    sd9 = _fresh([_m("M1", "IN_PROGRESS", "plan"), _m("M2", "TODO", "plan")])
    ev9 = os.path.join(sd9, "evidence", "M1"); os.makedirs(ev9, exist_ok=True)
    with open(os.path.join(ev9, "flag.json"), "w", encoding="utf-8") as f:
        json.dump({"kind": "spec-divergence", "summary": "出方案就发现要偏离 spec，impl 前先问"},
                  f, ensure_ascii=False)
    m_m1 = state._find(state.load_milestones(sd9), "M1")
    opts9 = {"driver_cmd": "true", "driver_timeout": 30, "dry_run": False,
             "max_infra_retries": 5, "max_replans": 5, "judge_cmd": "true",
             "probe_timeout": 30, "review_timeout": 30}
    loop._phase_plan(sd9, m_m1, opts9)
    m1 = {m["id"]: m for m in state.load_milestones(sd9)}["M1"]
    check("item2c _phase_plan 检测 plan 期举旗 → M1 NEEDS_CONFIRM", m1["status"] == "NEEDS_CONFIRM")
    check("item2c plan 举旗后不进 plan_review", m1["phase"] != "plan_review")
    # item2a/2b：prompt 硬化（门1 重点审方向 + driver 早举旗）已写入模板
    _pdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")
    _planrev = open(os.path.join(_pdir, "plan_review.md"), encoding="utf-8").read()
    _driver = open(os.path.join(_pdir, "driver.md"), encoding="utf-8").read()
    check("item2a 门1 加'方向/approach 对不对'重点维度", "方向 / approach 对不对" in _planrev)
    check("item2b driver plan-only 鼓励'出方案阶段就该举旗'", "出方案阶段就该举旗" in _driver)

    ok = all(_rows)
    print("\nflags/D 自测：%d/%d 绿" % (sum(_rows), len(_rows)))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
