#!/usr/bin/env python3
"""reportdoc.py —— 运行报告能力 v2：从证据机器渲染一篇**结构化**运行报告（md + html）。

四段式（用户定的格式）：
  1 · 背景      —— 项目背景 + 本轮做的事背景
  2 · 阶段性进展 —— **结构化表格**（每步：做了啥/状态/出方案·实现·审耗时/备注），不堆文字
  3 · 耗时      —— 交互甘特（html/飞书 内嵌那张图；md 给文字时间线兜底）
  4 · 总结与复盘 —— 两维度：(a) 框架流程本身 (b) 项目本身，各自「好/不好/改进建议」

能力层（agent 无关）：产 md + html。复盘**机器从运行信号生成**（超时/熔断/返工/举旗/一次过…→
结论与建议），可复现、不靠 agent 自述。飞书发布是绑定（bindings/publish-feishu.sh）。

用法（一般经 `lhb report-doc <project>` 调）：python3 engine/reportdoc.py <state_dir> --stamp <s> [--format html]
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gantt        # noqa: E402
import state        # noqa: E402
import timeline     # noqa: E402

PHASE_STATUS_CN = {"DONE": "✅ 完成", "IN_PROGRESS": "🔄 进行中", "BLOCKED": "⛔ 卡住",
                   "NEEDS_CONFIRM": "🚩 待确认", "TODO": "⬜ 待做", "SKIPPED": "⏭️ 跳过"}
_GANTT_MARK = "<!--LHB:GANTT-->"


def _one_liner(state_dir):
    sp = os.path.join(state_dir, "spec.md")
    if os.path.exists(sp):
        for line in open(sp, encoding="utf-8"):
            if line.startswith("# "):
                t = line[2:].strip()
                for pre in ("spec — ", "spec—", "spec - ", "spec: ", "spec："):   # 去 spec 骨架前缀
                    if t.startswith(pre):
                        return t[len(pre):].strip()
                return t
    return "longhaul build"


def _spec_background(state_dir):
    """从 spec.md 取第一段实质正文当"项目背景"（跳过标题/引用/列表/水平线）。"""
    sp = os.path.join(state_dir, "spec.md")
    if not os.path.exists(sp):
        return ""
    para = []
    for line in open(sp, encoding="utf-8"):
        s = line.rstrip()
        if not s.strip():
            if para:
                break
            continue
        if s.startswith(("#", ">", "-", "*", "|", "```", "---", "===")):
            if para:
                break
            continue
        para.append(s.strip())
    return " ".join(para)[:400]


def _clip(s, n=40):
    s = (s or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[:n] + "…"


def _clean_sum(s, limit=80):
    """清洗 + 边界收尾：去技术噪声（#选择器/[属性]/.类名/残留标点），超长在自然断点加「等」（不用 …）。"""
    g = (s or "").replace("\n", " ").strip()
    g = re.sub(r"#[\w-]+", "", g)                      # #选择器
    g = re.sub(r"\[[^\]]*\]", "", g)                   # [data-attr]
    g = re.sub(r"<[^>]*>", "", g)                      # <key> 等占位符
    g = re.sub(r"\.[a-zA-Z][\w-]*", "", g)             # .类名 / .html
    g = re.sub(r"[（(][\s+、，,/；;]+", "（", g)           # 去括号开头被删选择器留下的残标点（含空格）
    g = re.sub(r"\s+([）)])", r"\1", g)
    g = re.sub(r"\s+", " ", g).strip(" 、，,；;")
    if len(g) <= limit:
        return g
    head = g[:limit]
    brk = set("+、。；/ ")
    cut = max((i for i in range(len(head) - 1, int(limit * 0.5), -1) if head[i] in brk), default=limit)
    return head[:cut].rstrip(" +、。；/") + " 等"


def _did_title(goal):
    """第一列「做了什么」＝最简单介绍：goal 冒号前的标题。无冒号就取短摘要。"""
    g = (goal or "").replace("\n", " ").strip()
    ps = [g.find(s) for s in ("：", ":") if g.find(s) > 0]
    return g[:min(ps)].strip() if ps else _clean_sum(g, 22)


def _did_detail(goal):
    """第二列「详情」＝冒号后的修改内容摘要（清洗 + 边界收尾）。无冒号则空。"""
    g = (goal or "").replace("\n", " ").strip()
    ps = [g.find(s) for s in ("：", ":") if g.find(s) > 0]
    return _clean_sum(g[min(ps) + 1:]) if ps else ""


def _signals(state_dir):
    """从证据抽运行信号（给复盘用）。复用 gantt.extract 的逐步耗时 + 失败时间线。"""
    g = gantt.extract(state_dir) or {"rows": [], "bars": [], "marks": []}
    evs = gantt._load_events(state_dir)
    ms = state.load_milestones(state_dir)

    def of(ev):
        return [e for e in evs if e.get("ev") == ev]
    timeouts = [e for e in evs if e.get("ev") == "infra_retry" and "timed out" in (e.get("reason") or "")]
    rows = g["rows"]
    smooth = [r for r in rows if r["status"] == "DONE" and not r["events"] and r["n_drv"] > 0]
    struggled = sorted([r for r in rows if r["events"]], key=lambda r: -len(r["events"]))
    return {
        "g": g, "ms": ms, "rows": rows, "smooth": smooth, "struggled": struggled,
        "done": [m for m in ms if m.get("status") == "DONE"],
        "blocked": [m for m in ms if m.get("status") == "BLOCKED"],
        "skipped": [m for m in ms if m.get("status") == "SKIPPED"],
        "confirm": [m for m in ms if m.get("status") == "NEEDS_CONFIRM"],
        "timeouts": timeouts, "crashes": [e for e in evs if e.get("ev") == "infra_retry" and e not in timeouts],
        "reopen": of("reopen_plan"), "reject": of("flag_rejected"), "raised": of("flag_raised"),
        "fail": of("fail"), "break": of("circuit_break"), "recover": of("self_recovery"),
        "split": of("milestone_split"),
        "drv_min": round(sum(r["drv_min"] for r in rows), 1),
        "rev_min": round(sum(r["rev_min"] for r in rows), 1),
        "wasted_min": round(sum(b["dur"] for b in g["bars"] if b["cat"] == "timeout") / 60, 1),
        "hours": g.get("hours", 0),
    }


def _goal_of(ms, mid):
    return next((m.get("goal", "") for m in ms if m.get("id") == mid), "")


def build_progress_table(state_dir):
    """第 2 段：阶段性进展，结构化表格（不堆文字）。"""
    s = _signals(state_dir)
    rows, ms = s["rows"], s["ms"]
    by = {r["mid"]: r for r in rows}
    out = ["| 步 | 做了什么 | 详情 | 状态 | 出方案/实现/审 | 备注 |", "|---|---|---|---|---|---|"]
    for m in ms:
        mid = m.get("id")
        r = by.get(mid, {})
        goal = m.get("goal", "")
        st = PHASE_STATUS_CN.get(m.get("status"), m.get("status") or "")
        if r.get("n_drv"):
            timing = "%s分(%d次) / %s分" % (r.get("drv_min", 0), r.get("n_drv", 0), r.get("rev_min", 0))
        else:
            timing = "—"
        evs = r.get("events", [])
        if evs:
            kinds = {}
            for e in evs:
                k = "超时" if "超时" in e["label"] else ("驳回" if "驳回" in e["label"] or "REVISE" in e["label"] else ("熔断" if "熔断" in e["label"] else "返工"))
                kinds[k] = kinds.get(k, 0) + 1
            note = "·".join("%s×%d" % (k, v) for k, v in kinds.items())
        else:
            note = "顺利" if m.get("status") == "DONE" else ""
        out.append("| `%s` | %s | %s | %s | %s | %s |" % (
            mid, _did_title(goal), _did_detail(goal), st, timing, note))
    return "\n".join(out)


def build_retro(state_dir):
    """第 4 段：两维度复盘（机器从运行信号生成，可复现）。"""
    s = _signals(state_dir)
    rows, ms = s["rows"], s["ms"]
    n_door = len(s["reopen"]) + len(s["reject"])
    out = ["## 4 · 总结与复盘", "",
           "> 两个维度：(a) 跑这件事的**框架流程**本身、(b) **项目**本身。下列结论全部来自运行信号（超时/返工/门裁定…），可复现、非 agent 自述。", ""]

    # ---- 4a 框架流程 ----
    out += ["### 4a · 框架流程（「一句话自主推进」这套机制）", "", "**做得好：**"]
    out.append("- %d/%d 个 milestone 一次过（出方案→写实现→判官审，零返工）。" % (len(s["smooth"]), len(rows)))
    if n_door:
        out.append("- 门机制实际挡下 **%d** 次方向/质量问题（门1 重规划 %d 次 + 举旗驳回 %d 次），坏方案没蒙混过关。" % (n_door, len(s["reopen"]), len(s["reject"])))
    if s["recover"]:
        out.append("- %d 次撞上限触发**自救换 approach**，没直接熔断卡死。" % len(s["recover"]))
    if s["timeouts"]:
        out.append("- %d 次 driver 超时都被超时续跑/重试接住，全程无人工干预。" % len(s["timeouts"]))
    out.append("")
    out.append("**不好 / 耗在哪：**")
    if s["wasted_min"] > 0:
        mids = "、".join(sorted(set(e["milestone"] for e in s["timeouts"])))
        out.append("- ⏱️ **%d 次 driver 超时白跑**，累计浪费约 **%.0f 分钟**（%s）——这一轮算白等。" % (len(s["timeouts"]), s["wasted_min"], mids))
    if s["struggled"]:
        w = s["struggled"][0]
        wl = w["events"][-1]
        out.append("- 🔁 返工集中在 **%s**（%d 次失败/驳回），最后一次：%s" % (w["mid"], len(w["events"]), _clip(wl["reason"] or wl["label"], 70)))
    if s["break"]:
        out.append("- ⛔ %d 次熔断（连续失败到 max_attempts 上限）。" % len(s["break"]))
    tot = s["drv_min"] + s["rev_min"]
    if tot > 0:
        out.append("- 时间结构：driver（出方案+实现）**%.0f 分** vs 判官审 **%.0f 分**（审占 %.0f%%）。" % (s["drv_min"], s["rev_min"], 100 * s["rev_min"] / tot))
    if len(out) and out[-1] != "":
        out.append("")
    out.append("**改进建议（框架级）：**")
    sugg = []
    if s["wasted_min"] > 5 or len(s["timeouts"]) >= 3:
        sugg.append("超时白跑偏多 → 把易超时的步（如前端整页实现）规划时默认拆小，或上调 `LONGHAUL_DRIVER_TIMEOUT`；超时续跑能接住但仍白等一轮。")
    if s["struggled"] and len(s["struggled"][0]["events"]) >= 3:
        sugg.append("**%s** 反复返工 → 该步 acceptance 可能不够具体、或步子过大；规划阶段（门1）就该主动拆分（split 能力已具备）。" % s["struggled"][0]["mid"])
    if tot > 0 and s["rev_min"] / tot > 0.35:
        sugg.append("判官审占比偏高（%.0f%%）→ 精简判官 prompt，或对低风险步跳过双门。" % (100 * s["rev_min"] / tot))
    if s["break"]:
        sugg.append("出现熔断 → 复核 `max_attempts` / 自救策略是否够用。")
    if not sugg:
        sugg.append("本轮框架表现平稳，无显著可改点；继续观察超时与返工分布即可。")
    out += ["%d. %s" % (i, x) for i, x in enumerate(sugg, 1)]
    out.append("")

    # ---- 4b 项目本身 ----
    out += ["### 4b · 项目本身", "",
            "**达成：** %d/%d milestone 完成%s。" % (len(s["done"]), len(ms), "（全绿）" if len(s["done"]) == len(ms) else "")]
    un = []
    if s["blocked"]:
        un.append("卡住 " + ", ".join(m["id"] for m in s["blocked"]))
    if s["skipped"]:
        un.append("跳过 " + ", ".join(m["id"] for m in s["skipped"]))
    if s["confirm"]:
        un.append("待确认 " + ", ".join(m["id"] for m in s["confirm"]))
    if un:
        out.append("**未完：** " + "；".join(un) + "。")
    out.append("")
    out.append("**需关注（折腾过的步，折腾多≠最终对，建议按四列证据表复核产物）：**")
    if s["struggled"]:
        for r in s["struggled"][:5]:
            le = r["events"][-1]
            out.append("- `%s` %s — 折腾 %d 次，最后一次：%s" % (
                r["mid"], _clip(_goal_of(ms, r["mid"]), 22), len(r["events"]), _clip(le["reason"] or le["label"], 70)))
    else:
        out.append("- 无明显折腾步；仍建议按各步 acceptance 抽查真实产物。")
    out.append("")
    out.append("**改进建议（项目级）：**")
    pj = ["重点复核上面折腾过的步的**真实产物**（折腾多往往埋着将就/降级，按四列证据表验收）。"]
    pj.append("卡住/跳过/待确认的步需人工跟进收口。" if un else "全绿，但 MVP 真能跑要端到端走一遍验收，别只看 milestone 状态。")
    out += ["%d. %s" % (i, x) for i, x in enumerate(pj, 1)]
    out.append("")
    return "\n".join(out)


def build_md(state_dir, stamp="", feishu=False):
    """完整运行报告（markdown，四段式）。feishu=True 出飞书版（耗时段指向文末妙笔甘特、去掉本地占位/details）。"""
    one = _one_liner(state_dir)
    s = _signals(state_dir)
    out = ["# 运行报告 — %s" % one, ""]
    if stamp:
        out += ["> 生成于 %s（longhaul 从 `.longhaul/` 证据机器渲染，非 agent 自述）" % stamp, ""]

    out += ["## 1 · 背景", ""]
    bg = _spec_background(state_dir)
    if bg:
        out += ["**项目背景：** " + bg, ""]
    out += ["**本轮：** 目标「%s」，结果 **%d / %d** milestone 完成，历时约 **%s 小时**。" % (
        one, len(s["done"]), len(s["ms"]), s["hours"]), ""]

    out += ["## 2 · 阶段性进展", "", build_progress_table(state_dir), ""]

    out += ["## 3 · 耗时", ""]
    if feishu:
        out += ["下方为本轮**交互甘特**（每步出方案/实现/审/超时/返工，hover 看详情、可横向缩放）：", ""]
    else:
        out += ["下图为本轮**交互甘特**（每步出方案/实现/审/超时/返工，hover 看详情）；md 阅读器看不到交互图时，见下方文字时间线。",
                "", _GANTT_MARK, "",
                "<details><summary>文字时间线（兜底）</summary>", "", "```", timeline.render(state_dir), "```", "", "</details>", ""]

    out += [build_retro(state_dir)]
    return "\n".join(out)


# ---- 极简 markdown → html（零三方库；够把报告渲染成可读网页 + 供飞书发布）----

def _esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _bold(s):
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"`(.+?)`", r"<code>\1</code>", s)
    return s


def _table_html(tbl_lines):
    """把 markdown 表格行渲成 <table>（跳过分隔行）。"""
    cells = [[c.strip() for c in ln.strip().strip("|").split("|")] for ln in tbl_lines]
    body = ["<table>"]
    for i, row in enumerate(cells):
        if i == 1 and all(set(c) <= set("-: ") for c in row):
            continue
        tag = "th" if i == 0 else "td"
        body.append("<tr>" + "".join("<%s>%s</%s>" % (tag, _bold(_esc(c)), tag) for c in row) + "</tr>")
    body.append("</table>")
    return "\n".join(body)


def build_html(md, title="运行报告", state_dir=None):
    """md → html；遇到 GANTT 占位符就嵌入交互甘特（需 state_dir）；支持表格 / details。"""
    body, in_code, in_list = [], False, False
    lines = md.split("\n")
    i = 0
    while i < len(lines):
        raw = lines[i]
        i += 1
        if raw.strip() == _GANTT_MARK:
            if state_dir:
                body.append(gantt.build_html(state_dir, "本轮运行流水（交互甘特）", standalone=False))
            continue
        if raw.strip() == "```":
            body.append("</pre>" if in_code else "<pre>")
            in_code = not in_code
            continue
        if in_code:
            body.append(_esc(raw))
            continue
        line = raw.rstrip()
        # 表格：连续以 | 开头的行
        if line.startswith("|"):
            tbl = [line]
            while i < len(lines) and lines[i].strip().startswith("|"):
                tbl.append(lines[i].rstrip())
                i += 1
            if in_list:
                body.append("</ul>")
                in_list = False
            body.append(_table_html(tbl))
            continue
        if line.startswith("- "):
            if not in_list:
                body.append("<ul>")
                in_list = True
            body.append("<li>%s</li>" % _bold(_esc(line[2:])))
            continue
        if in_list:
            body.append("</ul>")
            in_list = False
        if line.startswith("<details") or line.startswith("</details") or line.startswith("<summary"):
            body.append(line)  # 透传 details（html 折叠）
        elif line.startswith("### "):
            body.append("<h3>%s</h3>" % _bold(_esc(line[4:])))
        elif line.startswith("## "):
            body.append("<h2>%s</h2>" % _bold(_esc(line[3:])))
        elif line.startswith("# "):
            body.append("<h1>%s</h1>" % _bold(_esc(line[2:])))
        elif line.startswith("> "):
            body.append("<blockquote>%s</blockquote>" % _bold(_esc(line[2:])))
        elif line == "":
            body.append("")
        else:
            body.append("<p>%s</p>" % _bold(_esc(line)))
    if in_list:
        body.append("</ul>")
    if in_code:
        body.append("</pre>")
    return _HTML_SHELL % (_esc(title), "\n".join(body))


_HTML_SHELL = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>%s</title>
<style>
body{font-family:"PingFang SC","Microsoft YaHei",system-ui,sans-serif;max-width:880px;margin:32px auto;
  padding:0 20px;color:#1f2329;line-height:1.6}
h1{font-size:26px;border-bottom:2px solid #1456f0;padding-bottom:8px}
h2{font-size:20px;margin-top:30px;color:#1456f0}h3{font-size:16px;margin-top:22px}
code{background:#f2f3f5;padding:1px 5px;border-radius:4px;font-family:"SF Mono",Menlo,Consolas,monospace;font-size:90%%}
pre{background:#0d1117;color:#e6edf3;padding:14px;border-radius:8px;overflow-x:auto;
  font-family:"SF Mono",Menlo,Consolas,monospace;font-size:13px;line-height:1.5;white-space:pre}
ul{padding-left:22px}li{margin:3px 0}blockquote{color:#646a73;border-left:3px solid #dee0e3;
  padding-left:12px;margin-left:0}strong{color:#0d1117}
table{border-collapse:collapse;width:100%%;margin:10px 0;font-size:13.5px}
th,td{border:1px solid #dee0e3;padding:7px 10px;text-align:left;vertical-align:top}
th{background:#f5f6f7;font-weight:600}tr:nth-child(even) td{background:#fafbfc}
details{margin:8px 0}summary{cursor:pointer;color:#646a73;font-size:13px}
</style></head><body>
%s
</body></html>"""


def main(argv=None):
    ap = argparse.ArgumentParser(description="运行报告能力 v2：从证据产结构化 md + html")
    ap.add_argument("state_dir")
    ap.add_argument("--stamp", default="")
    ap.add_argument("--format", choices=("md", "html"), default="md")
    ap.add_argument("--flavor", choices=("local", "feishu"), default="local",
                    help="feishu：正文耗时段指向文末妙笔甘特、去掉本地占位/details")
    a = ap.parse_args(argv)
    md = build_md(a.state_dir, a.stamp, feishu=(a.flavor == "feishu"))
    sys.stdout.write(build_html(md, "运行报告", a.state_dir) if a.format == "html" else md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
