#!/usr/bin/env python3
"""longhaul-builder — age.py：需求老化阶段的 MVP 脚手架（F8 / AC8 / DESIGN §2.3 阶段1）。

老化 = 「一句话需求 → 苏格拉底式追问 → 冻结 spec.md」。完整老化是一个**AI 对话过程**
（联网查规则、分段确认、grill 出 [NEEDS CLARIFICATION]）——那需要接上真 LLM driver（可后接的智能实现）来做。本脚手架是它的**确定性骨架**：

  1. `questions(one_liner)` —— 产出一组苏格拉底式追问模板（让 AI/人据此 grill 需求）。
  2. `skeleton(one_liner)` —— 产出一份**带标准章节的 spec.md 骨架**（User Stories / Acceptance /
     验收探针 / P0·P1·P2 / Assumptions / Decision Log / Risks），每节留 TODO 占位待填。

**诚实声明**：这是骨架生成器，不是全自动老化。它保证产出的 spec.md **结构正确、章节齐全、可被
plan.py 消费**，但章节内容是 TODO 占位，由真 agent（或人）在老化对话里填实 + 走 P0 硬门确认。
对比 state.cmd_init 写的极简 spec（只有一句话需求），本脚手架补齐了所有标准章节的脚手架。

agent/基建无关：纯标准库，只做字符串模板（无网络、无 LLM、无三方依赖，保可移植，P0-2/A1）。
"""

import argparse
import sys

#: 苏格拉底式追问模板——老化阶段拿它去 grill 一句话需求（覆盖 spec 必填字段的盲区）。
SOCRATIC_QUESTIONS = [
    "谁是用户、各自要完成什么任务？（→ User Stories，每条 As a / I want / so that）",
    "怎样算「做完了」？给出**可测可量**的验收标准，每条配一个能跑的验收探针。",
    "哪些是**不可逆 / 会做错方向**的决定（P0 硬门，必须人确认）？哪些是可逆假设（P1，AI 自拍记账）？",
    "明确的 Non-goals / 不做什么？避免范围蔓延。",
    "整体设计与架构：有没有和人确认过的设计稿/架构图？哪些模块原生做深、哪些先 iframe/嵌入现有？把**视觉/形态意图**也定清——写进「设计/架构」节，门2 会据它核对忠实度（ai-cockpit 踩过：设计稿没进 spec → 成品偏离）。",
    "有哪些 [NEEDS CLARIFICATION]：现在还说不清、必须先问清楚才能动的点？",
    "依赖与约束：语言 / 运行环境 / 不能破坏的既有契约（向后兼容）？",
    "已知风险（P2）：哪些地方可能踩坑但不阻塞起步？",
]

#: spec.md 标准章节骨架（与 plan.py 解析的 '## Acceptance Criteria' 等标题对齐——单一事实源）。
SPEC_SKELETON_TEMPLATE = """# spec — {one_liner}

> **DRAFT 骨架**（age.py 生成）。每节是 TODO 占位，由老化对话（AI grill + 人分段确认）填实。
> 填完 P0 清零 + 人确认（`state.py p0-confirm`）后方可进入 build。

## 一句话需求
{one_liner}

## User Stories
- US1 作为 <用户角色>：我想 <能力>，以便 <价值>。  <!-- TODO 老化时填实 -->
- US2 作为 <另一角色>：...  <!-- TODO -->

## 设计 / 架构（冻结 · 含视觉意图）
> ★ai-cockpit 复盘补的关键缺口：把**和人确认过的整体设计/架构**写死进 spec——否则 loop 只对着文字 spec 建、
> 那张设计稿/架构图丢失，成品会偏离（门2 reviewer 据本节核对"是否忠于设计"）。
- 整体架构 / 信息架构：<有哪些模块/页面/tab、怎么组织、数据流向>  <!-- TODO -->
- 视觉 / 形态意图：<画风、布局；贴**确认过的设计稿/线框**图或链接，约定长什么样>  <!-- TODO：把设计稿放这 -->
- 关键取舍（★必须和人对齐并写明）：<哪些原生做深、哪些先 iframe/嵌入现有；"完备版"必须做到位的是哪些>  <!-- TODO：别让人以为全原生、结果半数 iframe -->

## Acceptance Criteria（可测可量）
- AC1 <可测可量的验收标准>  <!-- TODO：每条必须客观可判 -->
- AC2 ...  <!-- TODO -->

## 验收探针（怎么验）
> 每条 AC 对应一个能跑的探针（TDD 单测 / golden / E2E 截图 / shell 检查）。plan.py 会按 AC 拆 milestone。
- AC1：<探针命令或手段>  <!-- TODO：尽量给可执行 probe_cmd -->
- AC2：...  <!-- TODO -->

## 成熟度门
### P0（硬门：放行前必须人确认 — 不可逆 / 会做错方向）
- P0-1 <范围 / 关键不可逆决策>  <!-- TODO -->

### P1（可逆假设，AI 自拍记账）
- A1 <可逆假设，AI 自己定、记进 assumption ledger，不阻塞>  <!-- TODO -->

### P2（非 MVP / 风险，入风险列表不阻塞）
- <风险项>  <!-- TODO -->

## Assumptions（assumption ledger）
- <AI 自拍的可逆假设逐条记录>  <!-- TODO -->

## Decision Log
- D1 <决策 + 来源>  <!-- TODO -->

## Risks
- <携带的未决风险，建设时盯>  <!-- TODO -->

## NEEDS CLARIFICATION
- [NEEDS CLARIFICATION] <还说不清、必须先问清的点>  <!-- TODO：老化时清零 -->
"""

#: spec.md 骨架必含的标准章节标题（冒烟测试 + plan.py 据此校验结构完整）。
REQUIRED_SECTIONS = (
    "## User Stories",
    "## 设计 / 架构",
    "## Acceptance Criteria",
    "## 验收探针",
    "## 成熟度门",
    "### P0",
    "### P1",
    "### P2",
    "## Assumptions",
)


def questions(one_liner: str) -> str:
    """产出一组苏格拉底式追问（老化对话的开场清单），返回多行可读文本。"""
    head = "# 老化追问（针对一句话需求）\n> 需求：%s\n\n" % (one_liner or "(空)")
    body = "\n".join("%d. %s" % (i + 1, q) for i, q in enumerate(SOCRATIC_QUESTIONS))
    return head + body + "\n"


def skeleton(one_liner: str) -> str:
    """产出一份带标准章节的 spec.md 骨架（每节 TODO 占位），返回完整 markdown 文本。"""
    return SPEC_SKELETON_TEMPLATE.format(one_liner=(one_liner or "<一句话需求>").strip())


# ---- item8 脚手架：AGENTS.md 入口骨架（跨 agent 标准、AI 可读、指向不复制规则）------------

AGENTS_SKELETON_TEMPLATE = """# {name}

> {one_liner}

由 **longhaul-builder** 自主构建/迭代（构建状态外置在 `.longhaul/`，像 `.git` 跟项目走）。

## 这是什么 / 怎么跑
- 需求 / 设计 / 验收：见 `.longhaul/spec.md`（冻结需求 + 「## 设计 / 架构」+ Acceptance Criteria）。
- 怎么起 / 目录结构 / 用法：见 `README.md`（若有）。
- 全局工程约定：指向你的事实源（如 `~/.config/agent-standards/core-conventions.md`）——**本文件只指向、不复制规则**（能力层与绑定层分离）。

## 迭代历史（这项目怎么一步步来的）
见 `docs/iterations/` —— 每轮 longhaul 构建留一条：日期 · 做了啥 · 运行报告链接（`lhb report` 收尾自动追加）。

## 给接手的 AI（人 / Claude / Codex / Coco 通用）
先读 ① `.longhaul/spec.md`（要做成什么）② `.longhaul/milestones.json`（本轮拆解 + 进度）③ `docs/iterations/` 最新一条（上轮到哪了）。**别手改 `.longhaul/` 里的状态文件**（cursor / milestones / events——框架管，像 .git）。
"""


def agents_skeleton(one_liner: str, name: str = "项目") -> str:
    """产出 AGENTS.md 入口骨架——薄、指向 README/spec/iterations/全局约定，不复制规则（item8）。"""
    return AGENTS_SKELETON_TEMPLATE.format(name=(name or "项目").strip(),
                                           one_liner=(one_liner or "<一句话需求>").strip())


# ---- CLI：python3 engine/age.py questions|skeleton|agents --one-liner "..." [-o file] ----

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="age.py",
        description="老化脚手架（MVP）：一句话 → 苏格拉底追问 + spec.md 骨架")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sq = sub.add_parser("questions", help="产出苏格拉底式追问清单")
    sq.add_argument("--one-liner", required=True)
    ss = sub.add_parser("skeleton", help="产出带标准章节的 spec.md 骨架")
    ss.add_argument("--one-liner", required=True)
    ss.add_argument("-o", "--out", default=None, help="写到文件（缺省打印到 stdout）")
    sa = sub.add_parser("agents", help="产出 AGENTS.md 入口骨架（AI 可读，指向不复制）")
    sa.add_argument("--one-liner", required=True)
    sa.add_argument("--name", default="项目", help="项目名（默认目录名）")
    sa.add_argument("-o", "--out", default=None, help="写到文件（缺省打印到 stdout）")
    args = ap.parse_args(argv)

    if args.cmd == "questions":
        sys.stdout.write(questions(args.one_liner))
        return 0
    if args.cmd == "agents":
        text = agents_skeleton(args.one_liner, args.name)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(text)
            print("wrote AGENTS.md to %s" % args.out)
        else:
            sys.stdout.write(text)
        return 0
    text = skeleton(args.one_liner)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print("wrote spec skeleton to %s" % args.out)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
