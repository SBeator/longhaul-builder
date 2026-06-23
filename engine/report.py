#!/usr/bin/env python3
"""report.py —— 从 evidence 确定性渲染「进度报告」（A 簇：报告/播报硬化）。

为什么存在：进度报告里"做了什么/改了哪些文件"以前只是 SKILL 给 agent 的软约定，机器没强制 →
agent 一偷懒就塌成"(见 evidence/)"。本脚本把报告内容**从已有证据机器渲染**出来，agent 只负责把
本脚本的输出转发给人，没法省略、没法编。内容全部来自 .longhaul 里真实存在的证据文件：

  目标/验收类型 ← milestones.json
  方案摘要      ← evidence/<M>/plan.md
  改了哪些文件  ← evidence/<M>/changed-files.txt（loop 在 impl 结束时 git diff 机器捕获）
  测试 红→绿    ← evidence/<M>/red.txt + green.txt
  门1/门2 判官  ← evidence/<M>/review-plan_review.json / review-impl_review.json
  探针结论      ← evidence/<M>/verify.jsonl
  附图清单      ← evidence/<M>/*.png|*.jpg|*.jpeg|*.svg（E2E 截图等，给绑定层附图用）
  各阶段耗时    ← 复用 timeline.py（events.jsonl 的 step_timing）

用法：
  python3 engine/report.py <state_dir>                 # 焦点 milestone 详述 + 其余简报
  python3 engine/report.py <state_dir> --milestone M5  # 指定某个详述
  python3 engine/report.py <state_dir> --images M5     # 只打印该 milestone 的图片证据路径（绑定层附图用）
  python3 engine/report.py <state_dir> --json          # 机器可读

agent/基建无关：纯标准库，只读 .longhaul 文件。
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import timeline  # noqa: E402  复用 step_timing 渲染（同目录）

IMG_EXT = (".png", ".jpg", ".jpeg", ".svg", ".gif", ".webp")
PHASE_CN = {"plan": "出方案", "plan_review": "审方案(门1)", "impl": "实施", "impl_review": "审实施(门2)",
            "done": "完成", "blocked": "熔断"}
STATUS_CN = {"TODO": "未开始", "IN_PROGRESS": "进行中", "DONE": "已完成",
             "BLOCKED": "熔断", "SKIPPED": "跳过", "NEEDS_CONFIRM": "待你确认"}


def _read(path, limit=None):
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            t = f.read()
    except OSError:
        return None
    if limit and len(t) > limit:
        t = t[:limit] + "\n…(截断)"
    return t


def _milestones(state_dir):
    p = os.path.join(state_dir, "milestones.json")
    d = json.loads(_read(p) or '{"milestones":[]}')
    return d.get("milestones", d) if isinstance(d, dict) else d


def _ev_dir(state_dir, mid):
    return os.path.join(state_dir, "evidence", mid)


def _tail(text, n=8):
    if not text:
        return None
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines[-n:])


def _exit_lines(text):
    """从 red/green 抽 EXIT_CODE= 行（驱动按约定贴真实退出码）。"""
    if not text:
        return None
    hits = [ln.strip() for ln in text.splitlines() if "EXIT_CODE" in ln.upper()]
    return " / ".join(hits) if hits else None


def _plan_summary(state_dir, mid):
    t = _read(os.path.join(_ev_dir(state_dir, mid), "plan.md"))
    if not t:
        return None
    # 取前若干非空行作为方案摘要（plan.md 头部通常就是做法/测试策略/范围）。
    lines = [ln for ln in t.splitlines() if ln.strip()]
    return "\n".join(lines[:14])


def _review(state_dir, mid, kind):
    """读 review-<kind>.json → {verdict, reason, raw_reason}。kind ∈ plan_review|impl_review。"""
    p = os.path.join(_ev_dir(state_dir, mid), "review-%s.json" % kind)
    t = _read(p)
    if not t:
        return None
    try:
        d = json.loads(t)
    except ValueError:
        return None
    raw = (d.get("raw") or "")
    # 抽 raw 里的 REASON 行（判官理由），没有就用 reason 字段。
    reason_line = None
    for ln in raw.splitlines():
        if ln.strip().upper().startswith("REASON"):
            reason_line = ln.strip()
            break
    return {"verdict": d.get("verdict"), "ok": d.get("ok"),
            "reason": reason_line or d.get("reason"),
            "duration_ms": d.get("duration_ms")}


def _verify_summary(state_dir, mid):
    """读 verify.jsonl 最后一条 → {probe, exit_code, verdict, sha256}。"""
    t = _read(os.path.join(_ev_dir(state_dir, mid), "verify.jsonl"))
    if not t:
        return None
    last = None
    for ln in t.splitlines():
        ln = ln.strip()
        if ln:
            try:
                last = json.loads(ln)
            except ValueError:
                pass
    if not last:
        return None
    return {"probe": last.get("probe"), "exit_code": last.get("exit_code"),
            "verdict": last.get("verdict"), "sha256": (last.get("sha256") or "")[:12]}


def _changed_files(state_dir, mid):
    return _read(os.path.join(_ev_dir(state_dir, mid), "changed-files.txt"), limit=2000)


def images(state_dir, mid):
    """该 milestone evidence 目录里的图片证据路径列表（绝对路径，给绑定层附图用）。"""
    d = _ev_dir(state_dir, mid)
    if not os.path.isdir(d):
        return []
    out = []
    for root, _dirs, files in os.walk(d):
        for fn in sorted(files):
            if fn.lower().endswith(IMG_EXT):
                out.append(os.path.abspath(os.path.join(root, fn)))
    return out


def milestone_detail(state_dir, m):
    """一个 milestone 的详述块（dict，供文本/JSON 两种渲染共用）。"""
    mid = m["id"]
    acc = m.get("acceptance") or {}
    red = _read(os.path.join(_ev_dir(state_dir, mid), "red.txt"))
    green = _read(os.path.join(_ev_dir(state_dir, mid), "green.txt"))
    return {
        "id": mid,
        "status": m.get("status"),
        "phase": m.get("phase"),
        "goal": m.get("goal"),
        "acceptance_type": acc.get("type"),
        "probe_cmd": acc.get("probe_cmd") or acc.get("probe"),
        "attempt": m.get("attempt_count"),
        "plan_summary": _plan_summary(state_dir, mid),
        "changed_files": _changed_files(state_dir, mid),
        "test_red": _exit_lines(red) or (_tail(red, 4) if red else None),
        "test_green": _exit_lines(green) or (_tail(green, 4) if green else None),
        "gate1": _review(state_dir, mid, "plan_review"),
        "gate2": _review(state_dir, mid, "impl_review"),
        "verify": _verify_summary(state_dir, mid),
        "images": images(state_dir, mid),
        "timing": timeline.render(state_dir, only=mid),
    }


def _focus_milestone(milestones, want=None):
    """默认焦点 = 指定的；否则最后一个非 TODO（最近动过的）；都没有就最后一个。"""
    if want:
        for m in milestones:
            if m["id"] == want:
                return m
        return None
    acted = [m for m in milestones if m.get("status") != "TODO"]
    return (acted or milestones or [None])[-1]


def _fmt_review(r):
    if not r:
        return "（无证据）"
    return "%s — %s" % (r.get("verdict") or "?", (r.get("reason") or "").strip() or "（判官未留理由）")


def render(state_dir, want=None):
    ms = _milestones(state_dir)
    if not ms:
        return "(无 milestones——可能还没拆解)"
    focus = _focus_milestone(ms, want)
    done = sum(1 for m in ms if m.get("status") == "DONE")
    out = ["📋 进度报告 ｜ %d/%d milestone 完成" % (done, len(ms)), ""]

    # 其余 milestone：每个一行简报
    out.append("【其他 milestone（简报）】")
    for m in ms:
        if focus and m["id"] == focus["id"]:
            continue
        st = STATUS_CN.get(m.get("status"), m.get("status") or "?")
        out.append("· %s [%s] %s" % (m["id"], st, (m.get("goal") or "")[:48]))
    out.append("")

    if not focus:
        return "\n".join(out)

    # 焦点 milestone：详述
    d = milestone_detail(state_dir, focus)
    st = STATUS_CN.get(d["status"], d["status"])
    out.append("【当前重点：%s [%s]（验收类型 %s）】" % (d["id"], st, d.get("acceptance_type") or "?"))
    out.append("目标：%s" % (d.get("goal") or ""))
    out.append("")
    out.append("▸ 方案摘要：")
    out.append(d.get("plan_summary") or "  （无 plan.md）")
    out.append("")
    out.append("▸ 改了哪些文件：")
    out.append((d.get("changed_files") or "  （未捕获 changed-files.txt——可能非 git 仓或旧版引擎）").rstrip())
    out.append("")
    out.append("▸ 测试 红→绿：")
    out.append("  红：%s" % (d.get("test_red") or "（无 red.txt）"))
    out.append("  绿：%s" % (d.get("test_green") or "（无 green.txt）"))
    out.append("")
    out.append("▸ 探针（确定性证据闸）：")
    v = d.get("verify")
    out.append("  %s" % ("%s exit=%s sha256=%s" % (v.get("verdict"), v.get("exit_code"), v.get("sha256"))
                         if v else "（无 verify.jsonl——该步可能 review-only）"))
    out.append("")
    out.append("▸ 判官裁定：")
    out.append("  门1 审方案：%s" % _fmt_review(d.get("gate1")))
    out.append("  门2 审实施：%s" % _fmt_review(d.get("gate2")))
    out.append("")
    imgs = d.get("images") or []
    out.append("▸ 附图（%d 张，发报告时附上）：" % len(imgs))
    for p in imgs:
        out.append("  📎 %s" % p)
    if not imgs:
        out.append("  （无截图——纯后端步无附图）")
    out.append("")
    out.append("▸ 各阶段耗时：")
    out.append(d.get("timing") or "（无 step_timing）")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description="从 evidence 渲染进度报告（A 簇）")
    ap.add_argument("state_dir")
    ap.add_argument("--milestone", default=None, help="焦点 milestone（默认=最近动过的）")
    ap.add_argument("--images", default=None, metavar="M",
                    help="只打印该 milestone 的图片证据路径（每行一个，给绑定层附图用）")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.images:
        for p in images(a.state_dir, a.images):
            print(p)
        return 0
    if a.json:
        ms = _milestones(a.state_dir)
        focus = _focus_milestone(ms, a.milestone)
        print(json.dumps({
            "total": len(ms), "done": sum(1 for m in ms if m.get("status") == "DONE"),
            "focus": milestone_detail(a.state_dir, focus) if focus else None,
            "others": [{"id": m["id"], "status": m.get("status"), "goal": m.get("goal")}
                       for m in ms if not focus or m["id"] != focus["id"]],
        }, ensure_ascii=False))
        return 0
    print(render(a.state_dir, a.milestone))
    return 0


if __name__ == "__main__":
    sys.exit(main())
