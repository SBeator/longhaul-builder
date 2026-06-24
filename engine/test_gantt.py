#!/usr/bin/env python3
"""test_gantt.py —— 交互甘特生成器：从 events.jsonl + milestones.json 抽数据 + 渲自包含 HTML。

内容全来自真证据（step_timing + 失败事件），零三方依赖。"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gantt   # noqa: E402
import state   # noqa: E402

_rows = []


def check(name, ok):
    _rows.append(bool(ok))
    print(("  ✓ " if ok else "  ✗ ") + name)


def _mk():
    proj = tempfile.mkdtemp(prefix="lhb-gantt-")
    sd = os.path.join(proj, ".longhaul")
    state.main(["init", sd, "--one-liner", "甘特测试"])
    msf = os.path.join(proj, "ms.json")
    json.dump({"milestones": [
        {"id": "AC1", "goal": "骨架", "acceptance": {"type": "tdd"}, "status": "DONE"},
        {"id": "AC2", "goal": "页面：实现一个交互页", "acceptance": {"type": "web-e2e"}, "status": "DONE"},
    ]}, open(msf, "w", encoding="utf-8"), ensure_ascii=False)
    state.main(["set-milestones", sd, "--file", msf])
    ev = os.path.join(sd, "events.jsonl")
    # 清掉 init/set-milestones 的真实时间戳事件，只留合成 step_timing（让 span 由合成事件定，不被 init 的"现在"污染）
    with open(ev, "w", encoding="utf-8") as f:
        # AC1：出方案 + 写实现，顺利
        f.write(json.dumps({"ts": "2026-06-24T00:00:10Z", "ev": "step_timing", "milestone": "AC1",
                            "phase": "plan", "step": "driver", "started": "2026-06-24T00:00:00Z",
                            "duration_ms": 60000}) + "\n")
        f.write(json.dumps({"ts": "2026-06-24T00:03:00Z", "ev": "step_timing", "milestone": "AC1",
                            "phase": "impl", "step": "driver", "started": "2026-06-24T00:01:00Z",
                            "duration_ms": 120000}) + "\n")
        f.write(json.dumps({"ts": "2026-06-24T00:04:00Z", "ev": "step_timing", "milestone": "AC1",
                            "step": "review", "started": "2026-06-24T00:03:00Z", "duration_ms": 60000}) + "\n")
        # AC2：写实现撞超时（白跑）+ 一次失败返工
        f.write(json.dumps({"ts": "2026-06-24T00:20:00Z", "ev": "step_timing", "milestone": "AC2",
                            "phase": "impl", "step": "driver", "started": "2026-06-24T00:10:00Z",
                            "duration_ms": 600000}) + "\n")
        f.write(json.dumps({"ts": "2026-06-24T00:20:00Z", "ev": "infra_retry", "milestone": "AC2",
                            "reason": "driver(impl) infra: driver timed out after 600s"}) + "\n")
        f.write(json.dumps({"ts": "2026-06-24T00:25:00Z", "ev": "fail", "milestone": "AC2",
                            "error": "判官 REVISE：表单没渲染出来", "attempt": 1}) + "\n")
    return sd


def main():
    sd = _mk()
    d = gantt.extract(sd)
    check("extract 非空", d is not None)
    check("rows = 2（AC1/AC2）", len(d["rows"]) == 2)
    check("bars 含出方案/写实现/审/超时", len(d["bars"]) >= 4)
    check("识别 timeout 段 + 带 reason",
          any(b["cat"] == "timeout" and b.get("reason") for b in d["bars"]))
    check("AC2 有失败/返工时间线", any(r["mid"] == "AC2" and r["events"] for r in d["rows"]))
    check("AC1 顺利无 events", any(r["mid"] == "AC1" and not r["events"] for r in d["rows"]))
    check("marks 收集到失败事件（超时/返工）", len(d["marks"]) >= 1)
    check("meta 有 hours/span/start_local", d["hours"] > 0 and d["span"] > 0 and d["start_local"])

    html = gantt.build_html(sd, "测试甘特")
    check("html 含 DOCTYPE 外壳", "<!DOCTYPE html>" in html)
    check("html 嵌入 DATA + 画布 + 交互绑定", "var DATA=" in html and "lhg-canvas" in html and "bindTip" in html)
    check("html 注入了真实 reason（超时白跑可见）", "timed out" in html)
    frag = gantt.build_html(sd, "测试", standalone=False)
    check("fragment 模式无 DOCTYPE（飞书 HTML Box 用）", "<!DOCTYPE" not in frag and "lhg-canvas" in frag)

    # 空数据优雅降级
    empty = os.path.join(tempfile.mkdtemp(), ".longhaul")
    state.main(["init", empty, "--one-liner", "x"])
    check("无 step_timing 时优雅降级（不抛）", "无 step_timing" in gantt.build_html(empty))

    npass = sum(1 for r in _rows if r)
    print("\n交互甘特：%d/%d 绿" % (npass, len(_rows)))
    return 0 if npass == len(_rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
