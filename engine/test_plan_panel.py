#!/usr/bin/env python3
"""test_plan_panel.py —— #10b plan 多 agent 协同：plan_review 跑 N 人 panel 并聚合。

站在 #10a 地基上：panel 每一项就是一个分阶段 agent。配 ≥2 个 → 多 agent 一起审方案、
任一发现方向问题就在 plan 期拦下（减走偏）；配 0/1 个 → 回落单审（向后兼容）。

stub judge 用内联 printf 直接吐 VERDICT 块（零网络、确定性）；只在 mkdtemp 里造 state。
"""
import glob
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import loop    # noqa: E402
import review  # noqa: E402
import state   # noqa: E402

_rows = []

APPROVE = r"printf 'VERDICT: APPROVE\nREASON: ok\n'"
REVISE = r"printf 'VERDICT: REVISE\nREASON: 方向不对，approach 选错\n'"
AWC = r"printf 'VERDICT: APPROVE_WITH_CONDITIONS\nCONDITIONS: 必须做 X\nREASON: ok 带条件\n'"
ERR = r"exit 1"   # 无 VERDICT 块 + 非零退出 → 降级 ERROR


def check(name, ok):
    _rows.append(bool(ok))
    print(("  ✓ " if ok else "  ✗ ") + name)


class _NS:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _mk():
    proj = tempfile.mkdtemp(prefix="lhb-panel-")
    sd = os.path.join(proj, ".longhaul")
    state.main(["init", sd, "--one-liner", "panel 测试靶子"])
    with open(os.path.join(sd, "spec.md"), "w", encoding="utf-8") as f:
        f.write("# panel\n\n把方案审细。\n\n> 冻结需求。\n")
    msf = os.path.join(proj, "ms.json")
    json.dump({"milestones": [
        {"id": "M1", "goal": "做个 X", "acceptance": {"type": "tdd", "probe": "pytest"},
         "max_attempts": 3}]}, open(msf, "w", encoding="utf-8"), ensure_ascii=False)
    state.main(["set-milestones", sd, "--file", msf])
    ev = os.path.join(sd, "evidence", "M1")
    os.makedirs(ev, exist_ok=True)
    open(os.path.join(ev, "plan.md"), "w", encoding="utf-8").write("# 方案\n做法：X。测试：pytest 覆盖。\n")
    return sd


def _panel(sd, cmds):
    return review.review_panel(sd, "M1", "plan_review", judge_cmds=cmds,
                               ctx={"mode": "plan-only"}, timeout=30)


def main():
    # ---- resolve_plan_panel 解析 ----
    check("resolve:||| 分隔 + 去空白/空项",
          loop.resolve_plan_panel(env={"LONGHAUL_PLAN_PANEL": "a ||| b ||| "}) == ["a", "b"])
    check("resolve:未配 → 空列表", loop.resolve_plan_panel(env={}) == [])

    sd = _mk()

    # ---- 聚合裁定 ----
    r = _panel(sd, [APPROVE, APPROVE])
    check("全 APPROVE → APPROVE(放行 exit0)", r["verdict"] == "APPROVE" and review._exit_code_for(r) == 0)
    check("聚合裁定带 panel 标记 + 人数", r.get("parsed", {}).get("panel") is True and r["parsed"]["n"] == 2)

    r = _panel(sd, [APPROVE, REVISE, APPROVE])
    check("任一 REVISE → REVISE(打回 exit1，抓走偏)",
          r["verdict"] == "REVISE" and review._exit_code_for(r) == 1)
    check("REVISE 聚合记了打回人数", r["parsed"].get("revisers") == 1)

    r = _panel(sd, [APPROVE, AWC])
    check("有条件无打回 → APPROVE_WITH_CONDITIONS(放行)",
          r["verdict"] == "APPROVE_WITH_CONDITIONS" and review._exit_code_for(r) == 0)

    r = _panel(sd, [ERR, ERR])
    check("全员降级 → ERROR(退回 infra exit3，不烧 attempt)",
          r["verdict"] == "ERROR" and review._exit_code_for(r) == 3)

    r = _panel(sd, [ERR, APPROVE])
    check("部分降级但有有效裁定 → 按有效的聚合(APPROVE)", r["verdict"] == "APPROVE")

    # ---- 审计 + canonical ----
    _panel(sd, [APPROVE, REVISE])
    panel_files = glob.glob(os.path.join(sd, "evidence", "M1", "review-plan_review.panel-*.json"))
    check("每个 panelist 留审计文件", len(panel_files) >= 2)
    canon = json.load(open(os.path.join(sd, "evidence", "M1", "review-plan_review.json"), encoding="utf-8"))
    check("canonical 写的是聚合裁定", canon.get("parsed", {}).get("panel") is True)

    # ---- 接线：_review 按 panel 数量分流 ----
    opts = loop._opts_from_args(_NS())
    opts["plan_panel"] = [APPROVE, REVISE]
    res, ex = loop._review(sd, "M1", "plan_review", opts)
    check("_review:≥2 配置 → 走 panel 聚合", res.get("parsed", {}).get("panel") is True and ex == 1)

    opts2 = loop._opts_from_args(_NS())
    opts2["plan_panel"] = []
    opts2["judge_cmd"] = APPROVE
    res2, ex2 = loop._review(sd, "M1", "plan_review", opts2)
    check("_review:无 panel → 单审(非 panel) 向后兼容",
          (res2.get("parsed") or {}).get("panel") is not True and ex2 == 0)

    npass = sum(1 for r in _rows if r)
    print("\nplan 多 agent 协同(#10b)：%d/%d 绿" % (npass, len(_rows)))
    return 0 if npass == len(_rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
