#!/usr/bin/env python3
"""test_iterations.py —— 文档收敛：迭代归档（统一命名目录 + state 快照 + report + meta）+ INDEX 列表。"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import iterations   # noqa: E402
import state        # noqa: E402

_rows = []


def check(name, ok):
    _rows.append(bool(ok))
    print(("  ✓ " if ok else "  ✗ ") + name)


def _mk(one="ai-cockpit 前端重做（驾驶舱化 · 零 iframe）", n_done=2):
    proj = tempfile.mkdtemp(prefix="lhb-it-")
    os.system("cd %s && git init -q" % proj)
    sd = os.path.join(proj, ".longhaul")
    state.main(["init", sd, "--one-liner", one])
    with open(os.path.join(sd, "spec.md"), "w", encoding="utf-8") as f:
        f.write("# %s\n\n把散落能力收敛进一个驾驶舱。\n" % one)
    msf = os.path.join(proj, "ms.json")
    json.dump({"milestones": [
        {"id": "AC1", "goal": "骨架", "acceptance": {"type": "tdd"},
         "status": "DONE" if n_done >= 1 else "TODO", "attempt_count": 1},
        {"id": "AC2", "goal": "整页交互", "acceptance": {"type": "web-e2e"},
         "status": "DONE" if n_done >= 2 else "TODO", "attempt_count": 1},
    ]}, open(msf, "w", encoding="utf-8"), ensure_ascii=False)
    state.main(["set-milestones", sd, "--file", msf])
    with open(os.path.join(sd, "events.jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps({"ts": "2026-06-24T00:00:10Z", "ev": "step_timing", "milestone": "AC1",
                            "phase": "impl", "step": "driver", "started": "2026-06-24T00:00:00Z",
                            "duration_ms": 60000}) + "\n")
    return proj, sd


def main():
    # slugify
    check("slugify 截到分隔符 + 留中文字母", iterations.slugify("ai-cockpit 前端重做（驾驶舱化 · 零 iframe）") == "ai-cockpit-前端重做")
    check("slugify 去空格/特殊符", iterations.slugify("做个 X / Y：Z") == "做个-X")
    check("slugify 空串兜底", iterations.slugify("") == "iteration")

    proj, sd = _mk()
    r = iterations.archive(proj, sd, stamp="2026-06-24-1530")
    itd = iterations.it_dir(proj)
    folder = r["folder"]
    check("归档目录命名 <序号>-<日期>-<slug>", r["dir"].startswith("01-20260624-ai-cockpit"))
    check("产出 report.md", os.path.exists(os.path.join(folder, "report.md")))
    check("产出 report.html(内嵌甘特)", os.path.exists(os.path.join(folder, "report.html"))
          and "lhg-canvas" in open(os.path.join(folder, "report.html"), encoding="utf-8").read())
    check("产出 meta.json", os.path.exists(os.path.join(folder, "meta.json")))
    check("state/ 是 .longhaul 全量快照", os.path.exists(os.path.join(folder, "state", "milestones.json"))
          and os.path.exists(os.path.join(folder, "state", "events.jsonl")))
    meta = json.load(open(os.path.join(folder, "meta.json"), encoding="utf-8"))
    check("meta 记录 done/total/status(全绿→✅完成)", meta["done"] == 2 and meta["total"] == 2 and "完成" in meta["status"])

    idx = open(os.path.join(itd, "INDEX.md"), encoding="utf-8").read()
    check("INDEX.md 有标题 + 最新置顶", "迭代历史" in idx and "⭐ 最新" in idx and "01" in idx)
    check("INDEX.md 全部迭代是结构化表格", "| # |" in idx and "| 01 |" in idx)
    check("INDEX.md 链接到报告", "01-20260624-ai-cockpit" in idx and "report.md" in idx)

    # 复用同目录：再归档不新建序号
    r2 = iterations.archive(proj, sd, stamp="2026-06-24-1600")
    check("再归档复用同目录(不新建序号)", r2["dir"] == r["dir"] and len(iterations._ordinal_dirs(itd)) == 1)

    # 飞书回填
    iterations.set_feishu(proj, sd, "https://x.larkoffice.com/docx/ABC123")
    idx2 = open(os.path.join(itd, "INDEX.md"), encoding="utf-8").read()
    check("set_feishu 回填进 INDEX", "ABC123" in idx2 and "[飞书]" in idx2)
    check("set_feishu 存了 token(供下次原地更新同一篇)", iterations.feishu_token(sd) == "ABC123")

    # 第二轮迭代：序号递增到 02
    # 模拟新一轮：清掉 iteration marker（像开了新构建）
    os.remove(os.path.join(sd, "iteration.json"))
    state.main(["init", sd, "--one-liner", "第二轮：补 H5 适配"])  # 复用同 sd 当新一轮
    with open(os.path.join(sd, "spec.md"), "w", encoding="utf-8") as f:
        f.write("# 第二轮：补 H5 适配\n\n移动端适配。\n")
    msf = os.path.join(proj, "ms2.json")
    json.dump({"milestones": [{"id": "M1", "goal": "H5", "acceptance": {"type": "tdd"}, "status": "DONE"}]},
              open(msf, "w", encoding="utf-8"), ensure_ascii=False)
    state.main(["set-milestones", sd, "--file", msf])
    r3 = iterations.archive(proj, sd, stamp="2026-06-25-0900", date="20260625")
    check("新一轮序号递增到 02", r3["dir"].startswith("02-20260625"))
    idx3 = open(os.path.join(itd, "INDEX.md"), encoding="utf-8").read()
    check("INDEX 两条都在 + 02 置顶为最新", "| 02 |" in idx3 and "| 01 |" in idx3 and idx3.index("⭐ 最新") < idx3.index("第二轮"))

    # _overall_status：SKIPPED 是终态——被 split 替换的 milestone 留 SKIPPED 不该把整体卡在「进行中」
    # （对齐 loop 的真实完成语义：state._next_todo 只挑 TODO/IN_PROGRESS，SKIPPED 早被忽略）
    S = lambda *sts: [{"id": "x%d" % i, "status": s} for i, s in enumerate(sts)]
    check("overall:全 DONE → ✅ 完成", iterations._overall_status(S("DONE", "DONE")) == "✅ 完成")
    check("overall:DONE+SKIPPED(被拆分) → ✅ 完成(不卡进行中)",
          iterations._overall_status(S("DONE", "DONE", "SKIPPED")) == "✅ 完成")
    check("overall:还有 TODO → 不算完成", iterations._overall_status(S("DONE", "TODO")) != "✅ 完成")
    check("overall:全 SKIPPED(无任何 DONE) → 不算完成", iterations._overall_status(S("SKIPPED")) != "✅ 完成")

    npass = sum(1 for r in _rows if r)
    print("\n文档收敛(迭代归档+INDEX)：%d/%d 绿" % (npass, len(_rows)))
    return 0 if npass == len(_rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
