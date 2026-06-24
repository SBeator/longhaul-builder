#!/usr/bin/env python3
"""test_split.py —— item11 举旗式拆分（A）：把「太大」的 milestone 拆成可独立验收子步。

人确认拆分 → state.split：原 milestone 标 SKIPPED、按子目标插入子步(<mid>.1/.2…，继承 acceptance/
max_attempts，TODO@plan)、cursor 指第一个子步、清原步熔断账、记 milestone_split。「不死磕」。
inbox split 动词让人用大白话经 AI 触发（lhb say split --milestone M --into 'a;b'）。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import loop    # noqa: E402
import state   # noqa: E402

_rows = []


def check(name, ok):
    _rows.append(bool(ok))
    print(("  ✓ " if ok else "  ✗ ") + name)


def _m(mid, status, phase="plan"):
    return {"id": mid, "goal": mid + " 目标", "acceptance": {"type": "web-e2e", "probe": "x"},
            "status": status, "phase": phase, "attempt_count": 0, "max_attempts": 3}


def _mk(ms):
    proj = tempfile.mkdtemp(prefix="lhb-split-")
    sd = os.path.join(proj, ".longhaul")
    state.main(["init", sd, "--one-liner", "x"])
    mf = os.path.join(proj, "ms.json")
    json.dump({"milestones": ms}, open(mf, "w", encoding="utf-8"), ensure_ascii=False)
    state.main(["set-milestones", sd, "--file", mf])
    state.main(["p0-confirm", sd, "--by", "t"])
    return sd


def _events(sd):
    p = os.path.join(sd, "events.jsonl")
    return [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()] if os.path.exists(p) else []


def main():
    # M1 DONE、M2 卡住(拆它)、M3 TODO
    sd = _mk([_m("M1", "DONE", "done"), _m("M2", "BLOCKED", "blocked"), _m("M3", "TODO")])
    rc = state.main(["split", sd, "M2", "--into", "M2 后端骨架; M2 前端页面"])
    order = [m["id"] for m in state.load_milestones(sd)]
    byid = {m["id"]: m for m in state.load_milestones(sd)}
    check("split 退 0", rc == 0)
    check("原 M2 标 SKIPPED（留审计）", byid["M2"]["status"] == "SKIPPED")
    check("插入 M2.1 / M2.2 子步", "M2.1" in byid and "M2.2" in byid)
    check("子步插在 M2 之后、M3 之前（顺序对）", order == ["M1", "M2", "M2.1", "M2.2", "M3"])
    check("子步 TODO@plan", byid["M2.1"]["status"] == "TODO" and byid["M2.1"]["phase"] == "plan")
    check("子步继承 acceptance 类型(web-e2e)", byid["M2.1"]["acceptance"].get("type") == "web-e2e")
    check("子步 goal 来自 --into", byid["M2.1"]["goal"] == "M2 后端骨架")
    cur = state.load_cursor(sd)
    check("cursor 指向第一个子步 M2.1", cur.get("active_milestone") == "M2.1")
    check("记 milestone_split 事件", any(e["ev"] == "milestone_split" and e.get("milestone") == "M2" for e in _events(sd)))

    # --into 少于 2 个子目标 → 干净报错退 2
    sd2 = _mk([_m("X1", "BLOCKED", "blocked")])
    rc2 = state.main(["split", sd2, "X1", "--into", "就一个"])
    check("--into <2 子目标 → 退 2（拒绝）", rc2 == 2)
    check("拒绝时不动 X1", state._find(state.load_milestones(sd2), "X1")["status"] == "BLOCKED")

    # inbox split 动词：人经 AI 投递 split 消息 → consume → state.split 真跑
    sd3 = _mk([_m("M1", "DONE", "done"), _m("Y1", "BLOCKED", "blocked")])
    loop._drop_message(sd3, "split", milestone="Y1", into="Y1 第一步; Y1 第二步")
    loop.consume_inbox(sd3, {"max_replans": 5})
    byid3 = {m["id"]: m for m in state.load_milestones(sd3)}
    check("inbox split 端到端 → Y1 SKIPPED + 插 Y1.1/Y1.2", byid3["Y1"]["status"] == "SKIPPED"
          and "Y1.1" in byid3 and "Y1.2" in byid3)

    npass = sum(1 for r in _rows if r)
    print("\n举旗式拆分：%d/%d 绿" % (npass, len(_rows)))
    return 0 if npass == len(_rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
