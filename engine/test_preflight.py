#!/usr/bin/env python3
"""test_preflight.py —— B 簇 dogfood 自测：web 项目缺真 E2E 门 → 放行前被挡。

四列证据表：用例 | 输入(milestones) | 实际是否符合预期。验的是 B 簇兜底：UI milestone 没配
web-e2e 验收门时 preflight 报 gap，从而 `lhb confirm` 挡住放行（堵 ai-cockpit "12 个 UI 全 tdd、零 E2E"）。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import preflight  # noqa: E402

_rows = []


def check(name, ok):
    _rows.append(ok)
    print(("  ✓ " if ok else "  ✗ ") + name)


def _mk(ms):
    sd = tempfile.mkdtemp(prefix="lhb-preflight-")
    with open(os.path.join(sd, "milestones.json"), "w", encoding="utf-8") as f:
        json.dump({"milestones": ms}, f, ensure_ascii=False)
    return sd


def _m(mid, goal, mtype):
    return {"id": mid, "goal": goal, "acceptance": {"type": mtype}, "status": "TODO"}


def main():
    # 用例1：纯后端/算法项目（无 UI 信号）→ 无 gap，放行 OK。
    sd1 = _mk([_m("M1", "核心发牌算法：按规则分配角色", "tdd"),
               _m("M2", "REST API：GET /deal 返回分配", "integration")])
    g1 = preflight.check_web_e2e(sd1)
    check("纯后端项目无 E2E gap（不误挡）", g1 == [])

    # 用例2：web 项目，前端 milestone 却是 tdd（ai-cockpit 的病）→ 报 gap。
    sd2 = _mk([_m("M1", "后端 API", "integration"),
               _m("M3", "统一壳：前端单页 + 页面 tab 导航渲染", "tdd"),
               _m("M14", "修复中心前端界面：按钮点击触发", "tdd")])
    g2 = preflight.check_web_e2e(sd2)
    check("web 项目 UI milestone 是 tdd → 报 gap", len(g2) == 2)
    check("gap 列出 M3 与 M14", {x["id"] for x in g2} == {"M3", "M14"})

    # 用例3：同样的 web 项目，前端 milestone 改成 web-e2e → 无 gap，放行 OK。
    sd3 = _mk([_m("M1", "后端 API", "integration"),
               _m("M3", "统一壳：前端单页 + 页面 tab 导航渲染", "web-e2e"),
               _m("M14", "修复中心前端界面：按钮点击触发", "web-e2e")])
    g3 = preflight.check_web_e2e(sd3)
    check("UI milestone 改成 web-e2e → 无 gap", g3 == [])

    # 用例5（2026-06-23 review 扩词）：常见前端术语（react/vue/组件/表单/路由/布局/css/h5）
    # 描述的纯前端 milestone 也必须命中、报 gap —— 修复前这些全漏、零 E2E 蒙混过 confirm。
    sd5 = _mk([_m("F1", "React 组件库：Button / Modal 封装", "tdd"),
               _m("F2", "Vue 登录表单交互", "tdd"),
               _m("F3", "SPA 单页路由切换", "tdd"),
               _m("F4", "CSS 响应式布局适配", "tdd"),
               _m("F5", "H5 活动页下拉菜单", "tdd")])
    g5 = preflight.check_web_e2e(sd5)
    check("扩词后 react/vue/组件/路由/css/h5 前端 milestone 全报 gap",
          {x["id"] for x in g5} == {"F1", "F2", "F3", "F4", "F5"})
    # 不误挡：纯后端术语仍不命中
    sd6 = _mk([_m("B1", "数据库迁移脚本与索引优化", "tdd"),
               _m("B2", "消息队列消费者幂等处理", "integration")])
    check("扩词后纯后端仍不误挡（无 gap）", preflight.check_web_e2e(sd6) == [])

    # 用例4：CLI main 退出码契约（有 gap → 退 1；无 gap → 退 0）。
    check("preflight main 有 gap 退 1", preflight.main([sd2]) == 1)
    check("preflight main 无 gap 退 0", preflight.main([sd1]) == 0)

    ok = all(_rows)
    print("\npreflight/B 自测：%d/%d 绿" % (sum(_rows), len(_rows)))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
