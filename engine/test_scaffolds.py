#!/usr/bin/env python3
"""age.py / plan.py 脚手架的冒烟自测（F8 / AC8）。

立场：这两个是**MVP 骨架**（不是全自动老化/拆解）。测的是骨架的**契约**：
- age.py：questions 非空；skeleton 产出含全部标准章节的 spec.md（plan.py 能消费）。
- plan.py：从 spec 的 Acceptance 节解析出 AC → milestone stub；schema 合法、计数齐全；
  能与 age.py 串成 age→plan 流水（骨架 spec → 骨架 milestones）。
- 端到端：plan.py 产出的 milestones.json 能被 state.py set-milestones 真吃下（不报错）。

红线：只在 tempdir 造文件，绝不碰 LIVE .longhaul。四列证据表口径。
运行：python3 engine/test_scaffolds.py  → 退出码 0 全过 / 1 有不一致。
"""
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import age      # noqa: E402  (RED 阶段此 import 失败 = 预期的红)
import plan     # noqa: E402
import state    # noqa: E402

rows = []


def check(case, inp, expected, actual):
    ok = (expected == actual)
    rows.append((case, inp, str(expected), str(actual), "✅" if ok else "❌"))
    return ok


class _NS:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def run_cli(mod, argv):
    buf = io.StringIO()
    code = 1
    with redirect_stdout(buf):
        try:
            code = mod.main(list(argv))
        except SystemExit as e:
            code = int(e.code) if e.code else 0
    return code, buf.getvalue()


# ---- age.py 冒烟 -------------------------------------------------------------

def test_age():
    one = "把 X 升级为 Y"
    q = age.questions(one)
    check("age.questions 非空且含需求", one, True, bool(q.strip()) and one in q)
    check("age.questions 列出多条苏格拉底问", one, True, q.count(".") >= 5)

    sk = age.skeleton(one)
    check("age.skeleton 非空", one, True, bool(sk.strip()))
    check("age.skeleton 含一句话需求", one, True, one in sk)
    # 标准章节全在（plan.py 据此消费）
    missing = [s for s in age.REQUIRED_SECTIONS if s not in sk]
    check("age.skeleton 含全部标准章节", "REQUIRED_SECTIONS", [], missing)

    # CLI
    code, out = run_cli(age, ["questions", "--one-liner", one])
    check("age CLI questions 退 0 + 输出", "questions", (0, True), (code, bool(out.strip())))
    code, out = run_cli(age, ["skeleton", "--one-liner", one])
    check("age CLI skeleton 退 0 + 输出", "skeleton", (0, True), (code, bool(out.strip())))


# ---- plan.py 冒烟 + age→plan 流水 -------------------------------------------

_SPEC_SAMPLE = """# spec — demo

## 一句话需求
做一个 demo

## Acceptance Criteria（可测可量）
- AC1 第一条可测验收标准
- AC2 第二条可测验收标准

## 验收探针（怎么验）
- AC1：单测 test_one
- AC2：golden 比对

## 成熟度门
### P0
- P0-1 范围
"""


def test_plan_parse():
    parsed = plan.parse_acceptance(_SPEC_SAMPLE)
    ids = [a["id"] for a in parsed]
    check("plan 解析出 AC1/AC2", "spec sample", ["AC1", "AC2"], ids)
    # probe 回填
    probe1 = next(a["probe"] for a in parsed if a["id"] == "AC1")
    check("plan 回填 AC1 探针", "AC1 probe", "单测 test_one", probe1)

    result = plan.plan(_SPEC_SAMPLE)
    ms = result["milestones"]
    check("plan 产出 2 个 milestone stub", "spec sample", 2, len(ms))
    # schema 合法：每条带 id/goal/acceptance{type,probe}/status/attempt_count/max_attempts
    m0 = ms[0]
    schema_ok = (m0["id"] == "AC1" and m0["goal"] == "第一条可测验收标准"
                 and isinstance(m0["acceptance"], dict)
                 and "type" in m0["acceptance"] and "probe" in m0["acceptance"]
                 and m0["status"] == "TODO" and m0["attempt_count"] == 0
                 and m0["max_attempts"] == 3)
    check("plan stub schema 合法", "milestone[0]", True, schema_ok)

    # 空 spec（无 Acceptance）→ 空 milestones（合法但空）
    empty = plan.plan("# spec\n## 一句话需求\nx\n")
    check("plan 无 AC → 空 milestones", "no acceptance", 0, len(empty["milestones"]))


def test_age_to_plan_pipeline():
    """age.skeleton → plan.plan：骨架 spec 能被 plan 解析（骨架 AC1/AC2 占位行）。"""
    sk = age.skeleton("做个东西")
    result = plan.plan(sk)
    # age 骨架里 Acceptance 节有 '- AC1 <...>' / '- AC2 ...' 占位行 → plan 应解析出 2 条
    check("age→plan 流水解析出骨架 AC", "skeleton→plan", 2, len(result["milestones"]))


def test_plan_feeds_state():
    """plan.py 产出的 milestones.json 能被 state.py set-milestones 真吃下（端到端 schema 兼容）。"""
    project = tempfile.mkdtemp(prefix="lhb-scaffold-")
    state_dir = os.path.join(project, ".longhaul")
    run_cli(state, ["init", state_dir, "--one-liner", "demo"])  # 经 main 包装
    ms_file = os.path.join(project, "milestones.json")
    code, _ = run_cli(plan, [_write(project, "spec.md", _SPEC_SAMPLE), "-o", ms_file])
    check("plan CLI 写文件退 0", "plan -o", 0, code)
    code, _ = run_cli(state, ["set-milestones", state_dir, "--file", ms_file])
    check("state set-milestones 吃下 plan 产物退 0", "set-milestones", 0, code)
    # state 读回 milestones 正常（数量对）
    loaded = state.load_milestones(state_dir)
    check("state 读回 plan 的 milestone 数对", "load_milestones", 2, len(loaded))


def _write(d, name, text):
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    return p


def main():
    test_age()
    test_plan_parse()
    test_age_to_plan_pipeline()
    test_plan_feeds_state()
    npass = sum(1 for r in rows if r[4] == "✅")
    print("\n%-52s | %-22s | %-22s | %-22s | %s" % ("用例", "输入", "期望", "实际", "一致?"))
    print("-" * 130)
    for r in rows:
        print("%-52s | %-22s | %-22s | %-22s | %s" % r)
    ok = npass == len(rows)
    print("\n%s (%d/%d)" % ("ALL PASS ✅" if ok else "SOME FAIL ❌", npass, len(rows)))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
