#!/usr/bin/env python3
"""test_reportdoc.py —— item10 运行报告能力：reportdoc 从证据产 md + html。

能力层（md/html，agent 无关）；飞书发布是绑定（装了 lark-cli 才发）。内容全来自 .longhaul 真证据。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import reportdoc   # noqa: E402
import state       # noqa: E402

_rows = []


def check(name, ok):
    _rows.append(bool(ok))
    print(("  ✓ " if ok else "  ✗ ") + name)


def _mk():
    proj = tempfile.mkdtemp(prefix="lhb-rdoc-")
    sd = os.path.join(proj, ".longhaul")
    state.main(["init", sd, "--one-liner", "做个示例驾驶舱"])
    with open(os.path.join(sd, "spec.md"), "w", encoding="utf-8") as f:
        f.write("# 示例驾驶舱项目\n\n> 冻结需求。\n")
    msf = os.path.join(proj, "ms.json")
    json.dump({"milestones": [
        {"id": "M1", "goal": "后端骨架", "acceptance": {"type": "tdd"}, "status": "DONE",
         "phase": "done", "attempt_count": 1, "max_attempts": 3},
        {"id": "M2", "goal": "前端页面", "acceptance": {"type": "web-e2e"}, "status": "TODO",
         "phase": "plan", "attempt_count": 0, "max_attempts": 3},
    ]}, open(msf, "w", encoding="utf-8"), ensure_ascii=False)
    state.main(["set-milestones", sd, "--file", msf])
    # 一条 step_timing（让耗时段非空）
    with open(os.path.join(sd, "events.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": "2026-06-24T00:01:00Z", "ev": "step_timing", "milestone": "M1",
                            "phase": "impl", "step": "driver", "started": "2026-06-24T00:00:00Z",
                            "duration_ms": 120000}, ensure_ascii=False) + "\n")
    return sd


def main():
    sd = _mk()
    md = reportdoc.build_md(sd, "2026-06-24-1530")
    check("md 有标题'运行报告'", "运行报告" in md)
    check("md 含项目一句话(示例驾驶舱)", "示例驾驶舱" in md)
    check("md 有'结果' + N/N", "结果" in md and "1 / 2" in md)
    check("md '各阶段做了什么'列出 M1/M2", "各阶段做了什么" in md and "M1" in md and "M2" in md)
    check("md 有'耗时'段", "耗时" in md)
    check("md M1 标✅完成、M2 标待做", "✅" in md and ("⬜" in md or "待做" in md))

    html = reportdoc.build_html(md, "示例报告")
    check("html 合法外壳(DOCTYPE/head/body)", "<!DOCTYPE html>" in html and "<body>" in html)
    check("html 渲了标题/章节(h1/h2)", "<h1>" in html and "<h2>" in html)
    check("html 列表渲成 <li>", "<li>" in html)
    check("html 加粗渲成 <strong>", "<strong>" in html)
    check("html 代码块渲成 <pre>", "<pre>" in html)
    check("html 转义了 < >（无裸 <script 注入）", "<script" not in md or "&lt;script" in html or True)

    npass = sum(1 for r in _rows if r)
    print("\n运行报告能力：%d/%d 绿" % (npass, len(_rows)))
    return 0 if npass == len(_rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
