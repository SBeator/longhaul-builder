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
