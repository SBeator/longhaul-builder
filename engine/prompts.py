#!/usr/bin/env python3
"""longhaul-builder — prompt/rubric 模板渲染器（F1 / AC1 / US2）。

把 driver / 审方案(plan_review) / 审实现(impl_review) 三套 prompt 从"编排者脑子里"
抽成 engine/prompts/ 下的**模板文件** + 一个渲染器，让任何 agent/机器都能按某个
milestone 参数化产出完整 prompt——构建逻辑落进文件，不再硬编码在某个 agent 的上下文里。

设计立场（见 DESIGN.md §2.3 两道门 / §2.4 三层裁定 / §2.5 两把尺子）：
- 模板是**单一事实源**：三套模板各自编码 DESIGN 里那套两门两审流程；改流程只改模板。
- 渲染器是纯函数：吃一个 milestone dict + 可选 ctx，吐一段填好占位的 prompt 文本。
- agent/基建无关：纯标准库，只读本地模板文件 + 字符串替换。无任何三方依赖。

占位策略（已文档化）：
- `{{placeholder}}` 形式。**已提供 key**（来自 milestone 或 ctx）必须被填满，不残留字面量。
- **未提供 key**：替换为可见标记 `[[<key>:UNSET]]`，既不崩、又能在 prompt 里被人/agent
  一眼看出"这个上下文没给"，而不是悄悄留空导致语义丢失。
"""

import os
import re
import sys

PROMPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")

#: 合法模板 kind（= prompts/<kind>.md）。test = 测试独立 agent（课题，可选阶段）。
KINDS = ("driver", "plan_review", "impl_review", "test")

#: 未提供 key 的占位降级标记（可见，不静默留空）
UNSET_MARKER = "[[{key}:UNSET]]"

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


def _template_path(kind: str) -> str:
    return os.path.join(PROMPTS_DIR, f"{kind}.md")


def _format_notes(note_list) -> str:
    """把 milestone 的结构化 note（list of {id,ts,text}）渲染成可读多行串（F6 redirect 接线）。

    None / 空 list → ""（render 的 _sub 对"提供了 key 但空串"返回空串，无 [[redirect:UNSET]] 噪音）。
    既容忍人手填的自由字符串 note，也容忍结构化 list-of-dict（缺字段时尽力渲染）。
    """
    if not note_list:
        return ""
    if isinstance(note_list, str):
        return note_list
    lines = []
    for n in note_list:
        if isinstance(n, dict):
            lines.append("[redirect %s @%s] %s"
                         % (n.get("id", "?"), n.get("ts", "?"), n.get("text", "")))
        else:
            lines.append(str(n))
    return "\n".join(lines)


def _build_mapping(milestone: dict, ctx: dict) -> dict:
    """把 milestone 字段 + ctx 摊平成 {占位名: 值字符串}。"""
    acc = (milestone or {}).get("acceptance", {}) or {}
    mapping = {
        # 来自 milestone
        "milestone_id": milestone.get("id", ""),
        "goal": milestone.get("goal", ""),
        "acceptance_type": acc.get("type", ""),
        "acceptance_probe": acc.get("probe", ""),
        # F6: 人工 redirect 指示（结构化 note → 可读串；无 note → 空串，干净降级）
        "redirect": _format_notes(milestone.get("note")),
        # 来自 ctx（调用方注入：项目路径 / 状态目录 / 交接 / 模式）
        "project_path": ctx.get("project_path", ""),
        "state_dir": ctx.get("state_dir", ""),
        "carry_forward": ctx.get("carry_forward", ""),
        "mode": ctx.get("mode", ""),
    }
    # 允许 ctx 覆盖/补充任意额外占位（如未来模板新增占位）
    for k, v in ctx.items():
        mapping.setdefault(k, v)
    return {k: ("" if v is None else str(v)) for k, v in mapping.items()}


def render(milestone: dict, kind: str, ctx: dict = None) -> str:
    """渲染一套 prompt。

    :param milestone: milestone dict（至少含 id/goal/acceptance{type,probe}）。
    :param kind: 'driver' | 'plan_review' | 'impl_review'。
    :param ctx: 可选上下文 dict（project_path / state_dir / carry_forward / mode 等）。
    :returns: 占位已填充的完整 prompt 文本（非空）。
    :raises ValueError: kind 非法（清晰报错，列出合法值）。
    :raises FileNotFoundError: 模板文件缺失。
    """
    if kind not in KINDS:
        raise ValueError(
            f"unknown prompt kind: {kind!r}; expected one of {KINDS}"
        )
    path = _template_path(kind)
    if not os.path.exists(path):
        raise FileNotFoundError(f"prompt template missing: {path}")
    with open(path, encoding="utf-8") as f:
        template = f.read()

    mapping = _build_mapping(milestone or {}, ctx or {})

    def _sub(match):
        key = match.group(1)
        if key in mapping and mapping[key] != "":
            return mapping[key]
        if key in mapping:  # 提供了 key 但值为空串 → 就用空串（已知地"没内容"）
            return ""
        return UNSET_MARKER.format(key=key)  # 未提供 → 可见降级标记，不静默

    return _PLACEHOLDER_RE.sub(_sub, template)


# ---- CLI：python3 engine/prompts.py <kind> <state_dir> <milestone_id> --------
# 从 state_dir 的 milestones.json 里挑出该 milestone，渲染并打印（供 loop.py 调用）。

def _load_milestone(state_dir: str, mid: str) -> dict:
    import json
    ms_path = os.path.join(state_dir, "milestones.json")
    with open(ms_path, encoding="utf-8") as f:
        data = json.load(f)
    items = data["milestones"] if isinstance(data, dict) else data
    for m in items:
        if m.get("id") == mid:
            return m
    raise KeyError(f"milestone {mid!r} not found in {ms_path}")


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 3:
        print("usage: prompts.py <kind> <state_dir> <milestone_id> "
              "[--mode plan-only|implement]", file=sys.stderr)
        print(f"  kind ∈ {KINDS}", file=sys.stderr)
        return 2
    kind, state_dir, mid = argv[0], argv[1], argv[2]
    mode = "implement"
    if "--mode" in argv:
        i = argv.index("--mode")
        if i + 1 < len(argv):
            mode = argv[i + 1]
    try:
        milestone = _load_milestone(state_dir, mid)
    except (OSError, KeyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    ctx = {
        "project_path": os.path.dirname(os.path.abspath(state_dir)),
        "state_dir": state_dir,
        "carry_forward": "(none — 从 CLI 渲染，无交接上下文)",
        "mode": mode,
    }
    try:
        print(render(milestone, kind, ctx))
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
