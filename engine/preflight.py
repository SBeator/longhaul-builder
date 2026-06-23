#!/usr/bin/env python3
"""preflight.py —— B 簇：放行前的确定性校验（web 项目必须有真 E2E 验收门）。

为什么存在：ai-cockpit 复盘发现——前端/UI 的 milestone 被拆成了 `tdd`（只静态断言 HTML/JS 文本 +
打 API），整个构建没有一个浏览器层 E2E + 截图。框架当时**没有任何机器兜底**来发现"一个 web 项目
零 E2E 门"。本脚本就是那道兜底：`lhb confirm`（P0 放行）前跑它，发现"看着是前端/UI 的 milestone
却没配 web-e2e/e2e 验收门"就挡住放行（除非人 --force 明确跳过），逼拆解时给 UI 步配真浏览器探针。

判据（保守，宁可漏报不误挡）：某 milestone 的 goal 命中前端/UI 强信号词、且 acceptance.type 不是
web-e2e/e2e → 记一条 gap。有 gap = 该 web 项目缺 E2E 门。

agent/基建无关：纯标准库，只读 milestones.json。
"""
import argparse
import json
import os
import sys

#: 前端/UI 强信号（保守，仍避开 ui/tab/spa 这类易误命中的短子串；中文信号无歧义）。
#: 2026-06-23 review 扩词：原表只覆盖少数中文词，react/vue/组件/表单/路由/布局/css/h5 这类
#: 常见前端术语全漏 → 用这些词描述的纯前端项目照样零 E2E 蒙混过 confirm（ai-cockpit 那个坑会复发）。
UI_SIGNALS = ("前端", "页面", "网页", "网站", "界面", "浏览器", "看板", "渲染", "按钮",
              "视图", "可视化", "弹窗", "菜单", "导航", "表单", "组件", "路由", "布局",
              "样式", "交互", "登录页", "注册页", "活动页", "落地页", "下拉",
              "dashboard", "frontend", "front-end", "browser", "react", "vue", "svelte",
              "playwright", "css", "h5")
E2E_TYPES = ("web-e2e", "e2e")


def _milestones(state_dir):
    p = os.path.join(state_dir, "milestones.json")
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8") as f:
        d = json.load(f)
    return d.get("milestones", d) if isinstance(d, dict) else d


def _ui_ish(goal):
    g = (goal or "").lower()
    return any(s in g for s in UI_SIGNALS)


def check_web_e2e(state_dir):
    """返回缺 E2E 门的 UI milestone 列表（每条 {id, goal, type}）。空 = 没问题。"""
    gaps = []
    for m in _milestones(state_dir):
        if not _ui_ish(m.get("goal")):
            continue
        t = ((m.get("acceptance") or {}).get("type") or "").lower()
        if t not in E2E_TYPES:
            gaps.append({"id": m.get("id"), "goal": (m.get("goal") or "")[:60], "type": t or "(未设)"})
    return gaps


def main(argv=None):
    ap = argparse.ArgumentParser(description="放行前校验：web 项目必须有真 E2E 验收门（B 簇）")
    ap.add_argument("state_dir")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    gaps = check_web_e2e(a.state_dir)
    if a.json:
        print(json.dumps({"ok": not gaps, "gaps": gaps}, ensure_ascii=False))
        return 0 if not gaps else 1
    if not gaps:
        print("✓ preflight: 无 E2E 缺口（或非 web 项目）")
        return 0
    print("⚠️ preflight: 这些看着是前端/UI 的 milestone 没配真 E2E 验收门（acceptance.type 应为 web-e2e）:",
          file=sys.stderr)
    for g in gaps:
        print("   · %s [type=%s] %s" % (g["id"], g["type"], g["goal"]), file=sys.stderr)
    print("   按 DESIGN §2.4：UI milestone 应是 web-e2e + 真浏览器 probe_cmd（playwright 出退出码 + 截图）。",
          file=sys.stderr)
    print("   修正 milestones.json，或用 `lhb confirm <dir> --force` 明确跳过本校验。", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
