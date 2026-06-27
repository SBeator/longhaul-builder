#!/usr/bin/env python3
"""iterations.py —— 文档收敛：把每一轮 longhaul 构建归档成一个**结构化、统一命名**的迭代目录。

用户痛点：历史散在 `.longhaul/`、`.longhaul-buildN-archive/`、docs/ 三处，还有怪目录。
收敛后**唯一**结构：

    docs/iterations/
    ├── INDEX.md                       ← 结构化列表（最新在顶，唯一入口）
    ├── 01-20260623-首版统一AI驾驶舱/
    │   ├── report.md / report.html     ← 这轮运行报告（v2 四段式）
    │   ├── meta.json                   ← 这轮的元信息（给 INDEX 用）
    │   └── state/                      ← 原始证据（= 这轮 .longhaul 的快照）
    └── 02-20260624-…/ …

命名规则：`<两位序号>-<YYYYMMDD>-<slug>/`，一 feature 一独立子目录、全收敛在 docs/iterations 下。
活动构建仍用 `.longhaul/`（工作态、像 .git）；跑完/开新一轮时 `lhb archive-iteration` 自动归档进来。
agent 无关、可移植。
"""
import argparse
import datetime
import json
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gantt        # noqa: E402
import reportdoc    # noqa: E402
import state        # noqa: E402

_OVERALL = [("BLOCKED", "⛔ 有卡住"), ("NEEDS_CONFIRM", "🚩 待确认"),
            ("IN_PROGRESS", "🔄 进行中"), ("TODO", "🔄 进行中")]


def slugify(s, maxlen=22):
    s = re.split(r"[（(·,，。/：:]", (s or "").strip())[0].strip()   # 取核心，截到第一个分隔符
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"[^\w一-鿿-]", "", s)                       # 留中文/字母数字下划线/连字符
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:maxlen] or "iteration"


def build_date(state_dir):
    """这轮构建发生的日期（首个事件 ts 的本地日期 YYYYMMDD）；无事件返 None。"""
    for e in gantt._load_events(state_dir):
        ts = e.get("ts")
        if ts:
            d = datetime.datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S") + datetime.timedelta(hours=8)
            return d.strftime("%Y%m%d")
    return None


def it_dir(project):
    return os.path.join(project, "docs", "iterations")


def _ordinal_dirs(itd):
    if not os.path.isdir(itd):
        return []
    return sorted(d for d in os.listdir(itd)
                  if re.match(r"^\d{2}-", d) and os.path.isdir(os.path.join(itd, d)))


def next_ordinal(itd):
    nums = [int(d[:2]) for d in _ordinal_dirs(itd)]
    return "%02d" % ((max(nums) if nums else 0) + 1)


def _overall_status(ms):
    # SKIPPED 是终态（被拆分替换/有意跳过），不是待办——只要其余都 DONE 且至少有一个 DONE 即算完成，
    # 否则一个被 split 的 milestone(留 SKIPPED 审计) 会永远把整体卡在「进行中」。
    if ms and all(m.get("status") in ("DONE", "SKIPPED") for m in ms) \
            and any(m.get("status") == "DONE" for m in ms):
        return "✅ 完成"
    sts = {m.get("status") for m in ms}
    for key, lbl in _OVERALL:
        if key in sts:
            return lbl
    return "🔄 进行中"


def _read_marker(state_dir):
    p = os.path.join(state_dir, "iteration.json")
    if os.path.exists(p):
        try:
            return json.load(open(p, encoding="utf-8"))
        except ValueError:
            pass
    return {}


def _write_marker(state_dir, **kw):
    p = os.path.join(state_dir, "iteration.json")
    m = _read_marker(state_dir)
    m.update(kw)
    json.dump(m, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    return m


def archive(project, state_dir, stamp="", date=None, slug=None):
    """把当前 .longhaul 归档成一个迭代目录（已归档过则**复用同目录刷新**，不新建序号）。

    返回 {dir, folder, md, meta}。报告 + state 快照 + meta.json 都落进 folder，并重建 INDEX.md。
    """
    itd = it_dir(project)
    os.makedirs(itd, exist_ok=True)
    marker = _read_marker(state_dir)
    dirname = marker.get("iteration_dir")
    if not dirname:                       # 首次归档：分配序号-日期-slug
        one = reportdoc._one_liner(state_dir)
        nn = next_ordinal(itd)
        dt = date or build_date(state_dir) or (stamp.replace("-", "")[:8] if stamp else "00000000")
        sl = slug or slugify(one)
        dirname = "%s-%s-%s" % (nn, dt, sl)
        _write_marker(state_dir, iteration_dir=dirname)
    folder = os.path.join(itd, dirname)
    os.makedirs(folder, exist_ok=True)

    snap = os.path.join(folder, "state")   # 原始证据快照（= .longhaul 全量）
    if os.path.exists(snap):
        shutil.rmtree(snap)
    shutil.copytree(state_dir, snap)

    md = reportdoc.build_md(state_dir, stamp)
    with open(os.path.join(folder, "report.md"), "w", encoding="utf-8") as f:
        f.write(md)
    with open(os.path.join(folder, "report.html"), "w", encoding="utf-8") as f:
        f.write(reportdoc.build_html(md, "运行报告 — " + reportdoc._one_liner(state_dir), state_dir))

    ms = state.load_milestones(state_dir)
    meta = {
        "ordinal": dirname[:2], "dir": dirname, "date": dirname[3:11],
        "title": reportdoc._one_liner(state_dir),
        "done": sum(1 for m in ms if m.get("status") == "DONE"), "total": len(ms),
        "status": _overall_status(ms), "hours": (gantt.extract(state_dir) or {}).get("hours", 0),
        "feishu_url": marker.get("feishu_url", ""),
    }
    json.dump(meta, open(os.path.join(folder, "meta.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    build_index(project)
    return {"dir": dirname, "folder": folder, "md": os.path.join(folder, "report.md"), "meta": meta}


def _token_from_url(url):
    return url.rstrip("/").split("/")[-1].split("?")[0] if url else ""


def feishu_token(state_dir):
    """这条迭代已发布过的飞书文档 token（给 update-in-place 用；没发过返空）。"""
    return _read_marker(state_dir).get("feishu_token", "")


def set_feishu(project, state_dir, url):
    """归档后拿到飞书 URL 再回填进 meta + marker + 重建 INDEX；同时记 token（供下次原地更新同一篇）。"""
    if not url:
        return
    tok = _token_from_url(url)
    _write_marker(state_dir, feishu_url=url, feishu_token=tok)
    marker = _read_marker(state_dir)
    dirname = marker.get("iteration_dir")
    if not dirname:
        return
    mp = os.path.join(it_dir(project), dirname, "meta.json")
    if os.path.exists(mp):
        m = json.load(open(mp, encoding="utf-8"))
        m["feishu_url"] = url
        m["feishu_token"] = tok
        json.dump(m, open(mp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    build_index(project)


def _load_metas(itd):
    metas = []
    for d in _ordinal_dirs(itd):
        mp = os.path.join(itd, d, "meta.json")
        if os.path.exists(mp):
            try:
                metas.append(json.load(open(mp, encoding="utf-8")))
                continue
            except ValueError:
                pass
        metas.append({"ordinal": d[:2], "dir": d, "date": d[3:11], "title": d[12:],
                      "done": "?", "total": "?", "status": "—", "hours": 0, "feishu_url": ""})
    return sorted(metas, key=lambda m: m["ordinal"], reverse=True)   # 最新在顶


def _fmt_date(yyyymmdd):
    s = str(yyyymmdd)
    return "%s-%s-%s" % (s[:4], s[4:6], s[6:8]) if len(s) == 8 and s.isdigit() else s


def _links(m):
    parts = ["[报告](%s/report.md)" % m["dir"], "[html](%s/report.html)" % m["dir"]]
    if m.get("feishu_url"):
        parts.append("[飞书](%s)" % m["feishu_url"])
    return " / ".join(parts)


def build_index(project, project_name=None):
    """从所有 <NN>-*/meta.json 重建 docs/iterations/INDEX.md（结构化列表，最新在顶）。"""
    itd = it_dir(project)
    name = project_name or os.path.basename(os.path.abspath(project))
    metas = _load_metas(itd)
    out = ["# 迭代历史 · %s" % name, "",
           "> 每一轮 longhaul 自主构建 = 一条迭代，**最新在最上**。每条都能点进运行报告（背景 / 阶段进展 / 耗时甘特 / 复盘）；",
           "> 原始证据在各轮 `<目录>/state/`（= 那轮 `.longhaul` 快照）。当前活动构建仍在根目录 `.longhaul/`。", ""]
    if not metas:
        out += ["_（还没有归档的迭代。一轮构建跑完后会自动归档到这里。）_", ""]
        _write(os.path.join(itd, "INDEX.md"), "\n".join(out))
        return "\n".join(out)

    top = metas[0]
    out += ["## ⭐ 最新：%s · %s" % (top["ordinal"], top["title"]), "",
            "- 📅 %s ｜ %s ｜ %s/%s milestone ｜ 历时 %sh" % (
                _fmt_date(top["date"]), top["status"], top["done"], top["total"], top["hours"]),
            "- 📄 %s" % _links(top),
            "- 📦 原始证据 `%s/state/`" % top["dir"], ""]

    out += ["## 全部迭代", "",
            "| # | 日期 | 迭代 | 状态 | milestone | 历时 | 链接 |",
            "|---|---|---|---|---|---|---|"]
    for m in metas:
        out.append("| %s | %s | %s | %s | %s/%s | %sh | %s |" % (
            m["ordinal"], _fmt_date(m["date"]), m["title"], m["status"],
            m["done"], m["total"], m["hours"], _links(m)))
    out.append("")
    _write(os.path.join(itd, "INDEX.md"), "\n".join(out))
    return "\n".join(out)


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content if content.endswith("\n") else content + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description="文档收敛：迭代归档 + INDEX 结构化列表")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("archive", help="把当前 .longhaul 归档成一个迭代目录")
    a.add_argument("project")
    a.add_argument("--state-dir", default=None)
    a.add_argument("--stamp", default="")
    a.add_argument("--date", default=None)
    a.add_argument("--slug", default=None)
    f = sub.add_parser("set-feishu", help="归档后回填飞书 URL")
    f.add_argument("project")
    f.add_argument("url")
    f.add_argument("--state-dir", default=None)
    ix = sub.add_parser("index", help="只重建 INDEX.md")
    ix.add_argument("project")
    tk = sub.add_parser("feishu-token", help="打印这条迭代已发布的飞书 token（没发过则空）")
    tk.add_argument("project")
    tk.add_argument("--state-dir", default=None)
    args = ap.parse_args(argv)
    sd = getattr(args, "state_dir", None) or os.path.join(args.project, ".longhaul")
    if args.cmd == "archive":
        r = archive(args.project, sd, args.stamp, args.date, args.slug)
        print(r["dir"])
    elif args.cmd == "set-feishu":
        set_feishu(args.project, sd, args.url)
    elif args.cmd == "index":
        build_index(args.project)
    elif args.cmd == "feishu-token":
        print(feishu_token(sd))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
