#!/usr/bin/env python3
"""reportdoc.py —— item10 运行报告能力：从证据机器渲染一篇完整运行报告（md + html）。

能力层（agent 无关）：产出 md + html。飞书发布是**绑定**（bindings/publish-feishu.sh，装了 lark-cli 才发）。
内容全来自 .longhaul 真实证据（milestone 状态 + timeline 耗时 + report.py 焦点详述），不靠 agent 自述。

用法（一般经 `lhb report-doc <project>` 调，自动写进 <project>/docs/iterations/ + 追加索引）：
  python3 engine/reportdoc.py <state_dir> --stamp 2026-06-24-1530 -o <dir>
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import report      # noqa: E402
import state       # noqa: E402
import timeline    # noqa: E402

PHASE_STATUS_CN = {"DONE": "✅ 完成", "IN_PROGRESS": "🔄 进行中", "BLOCKED": "⛔ 卡住",
                   "NEEDS_CONFIRM": "🚩 待确认", "TODO": "⬜ 待做", "SKIPPED": "⏭️ 跳过"}


def _one_liner(state_dir):
    sp = os.path.join(state_dir, "spec.md")
    if os.path.exists(sp):
        for line in open(sp, encoding="utf-8"):
            if line.startswith("# "):
                return line[2:].strip()
    return "longhaul build"


def build_md(state_dir, stamp=""):
    """完整运行报告（markdown）：项目介绍 + 各阶段做了什么 + 耗时 + 焦点详述。"""
    ms = state.load_milestones(state_dir)
    done = sum(1 for m in ms if m.get("status") == "DONE")
    one = _one_liner(state_dir)
    out = ["# 运行报告 — %s" % one, ""]
    if stamp:
        out.append("> 生成于 %s（由 longhaul 从 `.longhaul/` 证据机器渲染，非 agent 自述）" % stamp)
        out.append("")
    out.append("## 0 · 结果")
    out.append("**%d / %d** milestone 完成。" % (done, len(ms)))
    out.append("")
    out.append("## 1 · 各阶段做了什么")
    for m in ms:
        st = PHASE_STATUS_CN.get(m.get("status"), m.get("status"))
        out.append("- `%s` %s — %s" % (m.get("id"), st, (m.get("goal") or "").strip()))
    out.append("")
    out.append("## 2 · 耗时")
    out.append("```")
    out.append(timeline.render(state_dir))
    out.append("```")
    out.append("")
    out.append("## 3 · 焦点 milestone 详述（其余见 `.longhaul/evidence/<M>/`）")
    out.append(report.render(state_dir))
    out.append("")
    return "\n".join(out)


# ---- 极简 markdown → html（不引三方库；够把报告渲染成可读网页 + 供飞书发布）----

def _esc(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def build_html(md, title="运行报告"):
    body, in_code, in_list = [], False, False
    for raw in md.split("\n"):
        if raw.strip() == "```":
            if in_code:
                body.append("</pre>")
            else:
                if in_list:
                    body.append("</ul>"); in_list = False
                body.append('<pre>')
            in_code = not in_code
            continue
        if in_code:
            body.append(_esc(raw))
            continue
        line = raw.rstrip()
        if line.startswith("- "):
            if not in_list:
                body.append("<ul>"); in_list = True
            body.append("<li>%s</li>" % _bold(_esc(line[2:])))
            continue
        if in_list:
            body.append("</ul>"); in_list = False
        if line.startswith("### "):
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


def _bold(s):
    import re
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"`(.+?)`", r"<code>\1</code>", s)
    return s


_HTML_SHELL = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>%s</title>
<style>
body{font-family:"PingFang SC","Microsoft YaHei",system-ui,sans-serif;max-width:860px;margin:32px auto;
  padding:0 20px;color:#1f2329;line-height:1.6}
h1{font-size:26px;border-bottom:2px solid #1456f0;padding-bottom:8px}
h2{font-size:20px;margin-top:28px;color:#1456f0}h3{font-size:16px;margin-top:20px}
code{background:#f2f3f5;padding:1px 5px;border-radius:4px;font-family:"SF Mono",Menlo,Consolas,monospace;font-size:90%%}
pre{background:#0d1117;color:#e6edf3;padding:14px;border-radius:8px;overflow-x:auto;
  font-family:"SF Mono",Menlo,Consolas,monospace;font-size:13px;line-height:1.5;white-space:pre}
ul{padding-left:22px}li{margin:3px 0}blockquote{color:#646a73;border-left:3px solid #dee0e3;
  padding-left:12px;margin-left:0}strong{color:#0d1117}
</style></head><body>
%s
</body></html>"""


def main(argv=None):
    ap = argparse.ArgumentParser(description="运行报告能力：从证据产 md + html（item10）")
    ap.add_argument("state_dir")
    ap.add_argument("--stamp", default="")
    ap.add_argument("--format", choices=("md", "html"), default="md")
    a = ap.parse_args(argv)
    md = build_md(a.state_dir, a.stamp)
    sys.stdout.write(build_html(md) if a.format == "html" else md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
