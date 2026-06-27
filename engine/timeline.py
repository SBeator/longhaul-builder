#!/usr/bin/env python3
"""timeline.py —— 从 events.jsonl 渲染人读的"执行流水"（每阶段 开始时间 + 耗时）。

框架本来就把每步 started/duration 记进 events.jsonl（step_timing 事件）；本脚本把它渲染成
"哪个 milestone 的哪个阶段、几点起、花了多久"，给 agent 做进度报告 / 给人最终看时间线用。
（也补上 F8 留的"timeline 报告"那个口子。）纯标准库。

用法：
  python3 engine/timeline.py <state_dir>                 # 全部
  python3 engine/timeline.py <state_dir> --milestone M1  # 只看某个
  python3 engine/timeline.py <state_dir> --json          # 机器可读
"""
import argparse
import json
import os
import sys

# 实质步骤（人关心的）：出方案/审/验证。state:* 是毫秒级账本动作，默认不单列。
SUBSTANTIVE = {"driver", "review", "verify"}
PHASE_CN = {"plan": "出方案", "plan_review": "审方案(门1)", "impl": "实施",
            "impl_review": "审实施(门2)"}
STEP_CN = {"driver": "driver", "review": "判官", "verify": "证据闸"}


def _fmt(ms):
    s = round((ms or 0) / 1000)
    m, sec = divmod(s, 60)
    return f"{m}m{sec:02d}s" if m else f"{sec}s"


def _hms(ts):
    return ts[11:19] if ts and len(ts) >= 19 else (ts or "")


def load(state_dir):
    p = os.path.join(state_dir, "events.jsonl")
    if not os.path.exists(p):
        return []
    out = []
    for line in open(p):
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def build(state_dir, only=None):
    evs = load(state_dir)
    init = next((e["ts"] for e in evs if e.get("ev") == "init"), None)
    p0 = next((e["ts"] for e in evs if e.get("ev") == "p0_confirmed"), None)
    rows = []
    for e in evs:
        if e.get("ev") != "step_timing":
            continue
        if e.get("step") not in SUBSTANTIVE:
            continue
        mid = e.get("milestone")
        if only and mid != only:
            continue
        rows.append({"milestone": mid, "phase": e.get("phase"),
                     "step": e.get("step"), "started": e.get("started"),
                     "duration_ms": e.get("duration_ms") or 0})
    # 每 milestone 小计
    per = {}
    for r in rows:
        per.setdefault(r["milestone"], []).append(r)
    return {"init": init, "p0_confirmed": p0, "rows": rows, "per_milestone": per}


def render(state_dir, only=None):
    d = build(state_dir, only)
    if not d["rows"]:
        return "(无 step_timing 数据——可能还没跑或旧版引擎)"
    lines = []
    if d["init"] and d["p0_confirmed"] and not only:
        # P0 老化对话窗口（init → p0 放行）
        secs = _window(d["init"], d["p0_confirmed"])
        lines.append(f"P0 老化/定需求对话：{_hms(d['init'])} → {_hms(d['p0_confirmed'])}（约 {_fmt(secs*1000)}）")
    total = 0
    for mid, rs in d["per_milestone"].items():
        sub = sum(r["duration_ms"] for r in rs)
        total += sub
        lines.append(f"\n[{mid}]  起 {_hms(rs[0]['started'])}  小计 {_fmt(sub)}")
        for r in rs:
            ph = PHASE_CN.get(r["phase"], r["phase"])
            st = STEP_CN.get(r["step"], r["step"])
            lines.append(f"   {_hms(r['started'])} （{_fmt(r['duration_ms'])}）{ph}·{st}")
    lines.append(f"\n构建累计耗时 {_fmt(total)}  ({len(d['rows'])} 步)")
    return "\n".join(lines)


def _goal_of(state_dir, mid):
    """从 milestones.json 取该 milestone 的 goal（缺文件/缺项→空串，播报不崩）。"""
    try:
        ms = json.load(open(os.path.join(state_dir, "milestones.json"), encoding="utf-8")).get("milestones", [])
        return next((m.get("goal", "") for m in ms if m.get("id") == mid), "")
    except Exception:
        return ""


def _goal_brief(goal, limit=46):
    """播报用紧凑「做了什么」：去技术噪声(#选择器/[属性]/<占位>)，超长在自然断点收尾加「等」。"""
    import re
    g = (goal or "").replace("\n", " ").strip()
    if not g:
        return ""
    g = re.sub(r"#[\w-]+", "", g)
    g = re.sub(r"\[[^\]]*\]", "", g)
    g = re.sub(r"<[^>]*>", "", g)
    g = re.sub(r"\s+", " ", g).strip()
    if len(g) <= limit:
        return g
    head = g[:limit]
    brk = set("：:+、。；/ ")
    cut = max((i for i in range(len(head) - 1, int(limit * 0.5), -1) if head[i] in brk), default=limit)
    return head[:cut].rstrip(" ：:+、。；/") + " 等"


def progress_line(state_dir, mid):
    """item 7 进度播报带时间：一行「<mid> 完成：做了什么 ｜ 本步耗时(分阶段) ｜ 累计 ｜ 当前时间」。

    给 bin/lhb 的 notify_progress 用——每个 milestone 完成时播报，让人跑着就看见"做了啥/哪步多久/到现在多久"，
    不用事后扒 events.jsonl（修"进度更新不带时间"bug 2026-06-24；补"播报缺详情/做了什么"bug 2026-06-27 #9）。
    """
    import datetime
    d = build(state_dir, only=mid)
    rs = d["per_milestone"].get(mid, [])
    plan = sum(r["duration_ms"] for r in rs if r["step"] == "driver" and r["phase"] == "plan")
    impl = sum(r["duration_ms"] for r in rs if r["step"] == "driver" and r["phase"] == "impl")
    review = sum(r["duration_ms"] for r in rs if r["step"] == "review")
    step_total = sum(r["duration_ms"] for r in rs)
    cumulative = "?"
    full = build(state_dir)   # 全量：累计＝init→现在的墙钟
    if full["init"]:
        try:
            t0 = datetime.datetime.strptime(full["init"][:19], "%Y-%m-%dT%H:%M:%S")
            secs = int((datetime.datetime.utcnow() - t0).total_seconds())
            h, mm = secs // 3600, (secs % 3600) // 60
            cumulative = "%dh%02dm" % (h, mm) if h else "%dm" % mm   # 累计可达数小时，用时分制更直观
        except Exception:
            pass
    now_local = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime("%H:%M")
    parts = []
    if plan:
        parts.append("出方案 " + _fmt(plan))
    if impl:
        parts.append("实现 " + _fmt(impl))
    if review:
        parts.append("审 " + _fmt(review))
    breakdown = "（" + " · ".join(parts) + "）" if parts else ""
    brief = _goal_brief(_goal_of(state_dir, mid))   # #9：播报带「做了什么」详情（最初一直缺，只报 id+耗时）
    head = "✅ %s 完成" % mid + (" — " + brief if brief else "")
    return "%s ｜ 本步 %s%s ｜ 累计 %s ｜ %s" % (
        head, _fmt(step_total), breakdown, cumulative, now_local)


def _window(a, b):
    # 粗略：用 HH:MM:SS 差（同日）；跨日就返回 0。纯标准库不引 datetime 解析 Z。
    try:
        import datetime
        fa = datetime.datetime.strptime(a[:19], "%Y-%m-%dT%H:%M:%S")
        fb = datetime.datetime.strptime(b[:19], "%Y-%m-%dT%H:%M:%S")
        return max(0, int((fb - fa).total_seconds()))
    except Exception:
        return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="渲染 longhaul 执行流水（每阶段 时间+耗时）")
    ap.add_argument("state_dir")
    ap.add_argument("--milestone", default=None)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.json:
        print(json.dumps(build(a.state_dir, a.milestone), ensure_ascii=False))
    else:
        print(render(a.state_dir, a.milestone))
    return 0


if __name__ == "__main__":
    sys.exit(main())
