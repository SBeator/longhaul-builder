#!/usr/bin/env python3
"""longhaul-builder — plan.py：milestone 拆解阶段的 MVP 脚手架（F8 / AC8 / DESIGN §2.3 阶段2）。

拆解 = 「冻结 spec.md → 拆成一串可独立验收的 milestone → milestones.json」。完整拆解需要 AI 判断
依赖序、合并/拆分粒度——那需要接上真 LLM driver（可后接的智能实现）来做。本脚手架是它的**确定性骨架**：

  把 spec.md 的 `## Acceptance Criteria` 每条解析成一个 milestone stub（id 从 AC 编号派生），
  并尽量从 `## 验收探针` 章节里把对应那条的探针手段回填进 acceptance.probe，产出一份
  **结构合法、可被 state.py set-milestones 消费**的 milestones.json 骨架。

**诚实声明**：这是骨架生成器，不是智能拆解。它做「一条 AC → 一个 milestone」的朴素 1:1 映射
（不判依赖序、不合并、不补 probe_cmd），goal/probe 文本直接抄 AC，需真 agent/人后续细化粒度、
补可执行 probe_cmd。它保证：① milestones.json schema 合法 ② 每条带 acceptance{type,probe} ③ 计数
字段齐全（status/attempt_count/max_attempts），下游 state/loop 能直接吃。

agent/基建无关：纯标准库，只做正则解析 + JSON（无网络、无 LLM、无三方依赖，P0-2/A1）。
"""

import argparse
import json
import os
import re
import sys

DEFAULT_MAX_ATTEMPTS = 3

#: 解析 spec.md 里 '## Acceptance Criteria' 这一节下的列表项 '- AC1 ...'。
_AC_LINE_RE = re.compile(r"^\s*-\s+(AC\d+)\s+(.*\S)\s*$")
#: 解析 '## 验收探针' 这一节下的 'AC1：探针...' 或 '- AC1：探针...'（中英文冒号都吃）。
_PROBE_LINE_RE = re.compile(r"^\s*-?\s*(AC\d+)\s*[:：]\s*(.*\S)\s*$")
_SECTION_RE = re.compile(r"^##\s+(.*\S)\s*$")


def _extract_section(text: str, title_keyword: str):
    """抽出某个 '## <含 keyword>' 章节下、到下一个 '## ' 之前的所有行。返回行列表（可空）。"""
    lines = text.splitlines()
    out, in_sec = [], False
    for ln in lines:
        m = _SECTION_RE.match(ln)
        if m:
            in_sec = title_keyword in m.group(1)
            continue
        if in_sec:
            out.append(ln)
    return out


def parse_acceptance(text: str):
    """从 spec.md 解析 (ac_id, criterion) 列表，并尽力回填 probe 文本。

    返回 list of {id, goal, probe}。无 Acceptance 节 / 无 AC 行 → 返回 []（调用方据此报错）。
    """
    ac_lines = _extract_section(text, "Acceptance Criteria")
    probe_lines = _extract_section(text, "验收探针")

    probes = {}
    for ln in probe_lines:
        m = _PROBE_LINE_RE.match(ln)
        if m:
            probes[m.group(1)] = m.group(2)

    items = []
    for ln in ac_lines:
        m = _AC_LINE_RE.match(ln)
        if m:
            ac_id, criterion = m.group(1), m.group(2)
            items.append({
                "id": ac_id,
                "goal": criterion,
                "probe": probes.get(ac_id, ""),  # 回填该 AC 的探针手段（没有就空）
            })
    return items


def to_milestones(parsed_acs):
    """把解析出的 AC 列表映射成 milestones.json 的 milestones 数组（schema 合法、计数齐全）。"""
    milestones = []
    for ac in parsed_acs:
        milestones.append({
            "id": ac["id"],
            "goal": ac["goal"],
            "acceptance": {
                "type": "tdd",                    # 默认 tdd；真拆解时按手段改 integration/e2e…
                "probe": ac.get("probe", ""),     # 来自 '## 验收探针' 节；空则待人补
                # probe_cmd 不自动生成（NL→可执行命令是真 agent 的活，骨架诚实留空）
            },
            "status": "TODO",
            "phase": "plan",
            "attempt_count": 0,
            "max_attempts": DEFAULT_MAX_ATTEMPTS,
            "last_error": None,
        })
    return milestones


#: 自曝声明——写进输出 JSON 顶层（set-milestones 只读 "milestones"，本字段被忽略，纯提示）。
SKELETON_NOTE = (
    "⚠️ 朴素骨架：1 条 Acceptance Criterion = 1 个 milestone（不判依赖序/不合并/不补 probe_cmd）。"
    "几乎肯定过度拆分——agent/人必须按「可独立验收的工作单元」重拆（核心算法/API/存储/前端/测试套件…），"
    "给每条补可执行 acceptance.probe_cmd，再 set-milestones。删掉本字段不影响。"
)


def plan(text: str) -> dict:
    """spec.md 文本 → {"_skeleton_note":..., "milestones": [...]} 骨架 dict。AC 为空时 milestones=[]。"""
    return {"_skeleton_note": SKELETON_NOTE, "milestones": to_milestones(parse_acceptance(text))}


# ---- CLI：python3 engine/plan.py <spec.md> [-o milestones.json] ---------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="plan.py",
        description="milestone 拆解脚手架（MVP）：spec.md → milestones.json 骨架")
    ap.add_argument("spec", help="冻结 spec.md 路径")
    ap.add_argument("-o", "--out", default=None,
                    help="写到文件（缺省打印到 stdout）")
    args = ap.parse_args(argv)

    if not os.path.exists(args.spec):
        print("error: spec not found: %s" % args.spec, file=sys.stderr)
        return 2
    with open(args.spec, encoding="utf-8") as f:
        text = f.read()
    result = plan(text)
    n = len(result["milestones"])
    if n == 0:
        print("warning: 未从 spec 的 '## Acceptance Criteria' 解析出任何 AC 行；"
              "产出空 milestones（请检查 spec 章节标题/格式）", file=sys.stderr)
    else:
        # 总是自曝：这是 1:1 朴素映射，几乎肯定要重拆——别让调用方照搬。
        print("⚠️ plan.py 朴素骨架：把 %d 条 AC 机械 1:1 成了 %d 个 milestone，几乎肯定过度拆分。\n"
              "   请按「可独立验收的工作单元」重拆（别照搬每条 AC 一个）、补 probe_cmd，再 set-milestones。"
              % (n, n), file=sys.stderr)
    payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(payload)
        print("wrote %d milestone stub(s) to %s" % (n, args.out))
    else:
        sys.stdout.write(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
