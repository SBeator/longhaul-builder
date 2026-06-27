#!/usr/bin/env python3
"""test_reportdoc.py —— 运行报告能力 v2：四段式结构化报告（背景/阶段进展表/耗时甘特/2维度复盘）。

能力层（md/html，agent 无关）；复盘机器从运行信号生成、可复现；飞书发布是绑定。内容全来自真证据。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import reportdoc   # noqa: E402
import state       # noqa: E402

_rows = []


def check(name, ok):
    _rows.append(bool(ok))
    print(("  ✓ " if ok else "  ✗ ") + name)


def _mk():
    proj = tempfile.mkdtemp(prefix="lhb-rdoc-")
    sd = os.path.join(proj, ".longhaul")
    state.main(["init", sd, "--one-liner", "做个示例驾驶舱"])
    with open(os.path.join(sd, "spec.md"), "w", encoding="utf-8") as f:
        f.write("# 示例驾驶舱项目\n\n把散落各处的 AI 能力收敛到一个本地驾驶舱。\n\n> 冻结需求。\n")
    msf = os.path.join(proj, "ms.json")
    json.dump({"milestones": [
        {"id": "M1", "goal": "后端骨架", "acceptance": {"type": "tdd"}, "status": "DONE",
         "phase": "done", "attempt_count": 1, "max_attempts": 3},
        {"id": "M2", "goal": "前端页面：#app 整页交互重做 + [data-form] 表单提交真落库 + 路由切换 + 主题明暗切换 + 移动端响应式适配 + 骨架屏加载态 + 错误边界兜底 + 国际化文案 + 无障碍支持 + 还有很多其它扩展组件做了一大堆内容需要慢慢收尾完善",
         "acceptance": {"type": "web-e2e"}, "status": "DONE",
         "phase": "done", "attempt_count": 2, "max_attempts": 3},
    ]}, open(msf, "w", encoding="utf-8"), ensure_ascii=False)
    state.main(["set-milestones", sd, "--file", msf])
    return _write_synth_events(sd)


def _write_synth_events(sd):
    # 清掉 init 真实时间戳，写合成证据：M1 顺利、M2 撞超时白跑 + 一次返工
    with open(os.path.join(sd, "events.jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps({"ts": "2026-06-24T00:00:10Z", "ev": "step_timing", "milestone": "M1",
                            "phase": "impl", "step": "driver", "started": "2026-06-24T00:00:00Z",
                            "duration_ms": 120000}) + "\n")
        f.write(json.dumps({"ts": "2026-06-24T00:03:00Z", "ev": "step_timing", "milestone": "M1",
                            "step": "review", "started": "2026-06-24T00:02:30Z", "duration_ms": 30000}) + "\n")
        f.write(json.dumps({"ts": "2026-06-24T00:20:00Z", "ev": "step_timing", "milestone": "M2",
                            "phase": "impl", "step": "driver", "started": "2026-06-24T00:10:00Z",
                            "duration_ms": 600000}) + "\n")
        f.write(json.dumps({"ts": "2026-06-24T00:20:00Z", "ev": "infra_retry", "milestone": "M2",
                            "reason": "driver(impl) infra: driver timed out after 600s"}) + "\n")
        f.write(json.dumps({"ts": "2026-06-24T00:25:00Z", "ev": "fail", "milestone": "M2",
                            "error": "判官 REVISE：表单提交没真落库", "attempt": 1}) + "\n")
    return sd


def _mk_tricky():
    """#9 回归夹具：无冒号长 goal、深冒号 goal、含管道符 goal —— 历史串列/详情空的三类。"""
    proj = tempfile.mkdtemp(prefix="lhb-rdoc-tricky-")
    sd = os.path.join(proj, ".longhaul")
    state.main(["init", sd, "--one-liner", "tricky"])
    msf = os.path.join(proj, "ms.json")
    json.dump({"milestones": [
        {"id": "T1", "goal": "全局交互反馈统一(按钮/采纳回链/toast)、空错态UX、长列表虚拟化、弱依赖核查",
         "acceptance": {"type": "tdd"}, "status": "DONE", "phase": "done",
         "attempt_count": 1, "max_attempts": 3},
        {"id": "T2", "goal": "实时动态价值化(已完成→看产物) + 今日排期列全定时任务(schedules+crontab) + 缺勤趋势 hover + 备份健康(搬 agent-config-backup 状态：最近备份/成功失败/各仓分支/恢复手册)",
         "acceptance": {"type": "tdd"}, "status": "DONE", "phase": "done",
         "attempt_count": 1, "max_attempts": 3},
        {"id": "T3", "goal": "左栏|右栏 双面板：A|B 同屏对比 + 导出",
         "acceptance": {"type": "tdd"}, "status": "DONE", "phase": "done",
         "attempt_count": 1, "max_attempts": 3},
    ]}, open(msf, "w", encoding="utf-8"), ensure_ascii=False)
    state.main(["set-milestones", sd, "--file", msf])
    return sd


def main():
    sd = _mk()
    md = reportdoc.build_md(sd, "2026-06-24-1530")
    check("md 标题'运行报告'", "运行报告" in md)
    check("§1 背景 含项目背景 + 一句话", "1 · 背景" in md and "示例驾驶舱" in md and "收敛" in md)
    check("§1 背景 给出 N/N + 历时", "2 / 2" in md and "历时" in md)
    check("§2 阶段性进展 是 markdown 表格", "2 · 阶段性进展" in md and "| 步 |" in md and "|---|" in md)
    check("§2 表格列出 M1/M2 + 状态", "`M1`" in md and "`M2`" in md and "✅" in md)
    _tbl = md.split("## 2 · 阶段性进展")[1].split("## 3")[0]
    check("§2 拆成 做了什么(简介) + 详情 两列", "| 做了什么 | 详情 |" in md)
    check("§2 第一列做了什么=简单标题(冒号前)", "前端页面" in _tbl)
    check("§2 第二列详情=修改内容摘要(组件、去技术噪声、不留 … 截断)",
          "整页交互重做" in _tbl and "表单提交" in _tbl
          and "#app" not in _tbl and "data-form" not in _tbl and "…" not in _tbl)
    check("§2 超长详情收尾用「等」不用 …", "等" in _tbl)
    check("§2 表格备注标出 M2 折腾（超时/返工）", "超时" in md and ("返工" in md or "驳回" in md))
    check("§3 耗时 含交互甘特占位 + 文字兜底", "3 · 耗时" in md and reportdoc._GANTT_MARK in md and "时间线" in md)
    check("§4 复盘 两维度(框架/项目)", "4 · 总结与复盘" in md and "4a · 框架流程" in md and "4b · 项目本身" in md)
    check("§4a 框架：一次过 + 超时白跑 + 改进建议", "一次过" in md and "超时白跑" in md and "改进建议（框架级）" in md)
    check("§4b 项目：达成 + 需关注折腾步 + 改进建议", "达成" in md and "需关注" in md and "M2" in md and "改进建议（项目级）" in md)
    check("§4 结论挂'可复现/运行信号'(非 agent 自述)", "运行信号" in md and "可复现" in md)

    # html：占位符被交互甘特替换 + 表格渲成 <table>
    html = reportdoc.build_html(md, "示例报告", sd)
    check("html 合法外壳", "<!DOCTYPE html>" in html and "<body>" in html)
    check("html 渲了章节(h1/h2/h3)", "<h1>" in html and "<h2>" in html and "<h3>" in html)
    check("html 表格渲成 <table>/<th>/<td>", "<table>" in html and "<th>" in html and "<td>" in html)
    check("html 内嵌了交互甘特(画布+DATA)", "lhg-canvas" in html and "var DATA=" in html)
    check("html 甘特注入真 reason(超时白跑可见)", "timed out" in html)
    check("html 透传 details 折叠(文字时间线兜底)", "<details>" in html and "<summary>" in html)
    check("html 加粗渲成 <strong>", "<strong>" in html)
    check("无 state_dir 时占位符被安全丢弃(不留裸标记)", reportdoc._GANTT_MARK not in reportdoc.build_html(md, "x"))

    # 飞书 flavor：耗时段指向文末妙笔甘特、去掉本地占位/details（飞书里甘特是独立 HTML Box）
    fmd = reportdoc.build_md(sd, "2026-06-24", feishu=True)
    check("飞书 flavor 去掉 GANTT 占位 + details", reportdoc._GANTT_MARK not in fmd and "<details>" not in fmd)
    check("飞书 flavor 耗时段引出下方甘特", "下方为本轮" in fmd and "## 3 · 耗时" in fmd)
    check("报告不含 §5 焦点详述（用户砍了）", "5 · 焦点" not in md and "5 · 焦点" not in fmd)
    check("飞书 flavor 仍保留四段+表格+复盘", "1 · 背景" in fmd and "| 步 |" in fmd and "4a · 框架流程" in fmd and "4b · 项目本身" in fmd)

    # —— #9 详情列根因修复：无冒号 / 深冒号 / 管道符 三类都稳健 ——
    t_nc, d_nc = reportdoc._split_goal("全局交互反馈统一(按钮/采纳回链/toast)、空错态UX、长列表虚拟化、弱依赖核查")
    check("#9 无冒号:做了什么=短标题", 0 < len(t_nc) <= reportdoc.TITLE_MAX)
    check("#9 无冒号:详情不空(AC14 bug)", d_nc.strip() != "")
    deep = "实时动态价值化(已完成→看产物) + 今日排期列全定时任务(schedules+crontab) + 缺勤趋势 hover + 备份健康(搬 agent-config-backup 状态：最近备份/成功失败/各仓分支/恢复手册)"
    t_dp, d_dp = reportdoc._split_goal(deep)
    check("#9 深冒号:做了什么不吞整句(AC4 bug)", len(t_dp) <= reportdoc.TITLE_MAX)
    check("#9 深冒号:详情有内容", d_dp.strip() != "")
    t_pp, d_pp = reportdoc._split_goal("左栏|右栏 双面板：A|B 同屏对比")
    check("#9 管道符转义(单元格无裸竖线)",
          "|" not in t_pp.replace("\\|", "") and "|" not in d_pp.replace("\\|", ""))
    check("#9 空 goal 安全", reportdoc._split_goal("") == ("", "") and reportdoc._split_goal(None) == ("", ""))
    # 表级回归：每数据行恒 6 列（转义后管道/深冒号都不串列）
    ptbl = reportdoc.build_progress_table(_mk_tricky())
    drows = [ln for ln in ptbl.splitlines() if ln.startswith("| `")]
    npipes = [ln.replace("\\|", "\x00").count("|") for ln in drows]
    check("#9 表格每行恒 6 列(管道/深冒号不串列)", len(drows) == 3 and all(n == 7 for n in npipes))

    # _one_liner 去 spec 骨架前缀
    d2 = tempfile.mkdtemp(prefix="lhb-one-")
    open(os.path.join(d2, "spec.md"), "w", encoding="utf-8").write("# spec — 干净标题测试\n\n正文\n")
    check("_one_liner 去掉 'spec — ' 前缀", reportdoc._one_liner(d2) == "干净标题测试")

    npass = sum(1 for r in _rows if r)
    print("\n运行报告能力 v2：%d/%d 绿" % (npass, len(_rows)))
    return 0 if npass == len(_rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
