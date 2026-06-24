#!/usr/bin/env python3
"""test_gate_hardening.py —— 验收门防被骗（item 3 防假绿 + item 4 防覆盖不全）。

· 3 防假绿：web-e2e 探针模板不止 wait_for_selector（空壳/占位/加载态也满足 ＝ 假绿），
  还要断言「真内容渲染」（非空文本/有子元素、不命中占位标记、达 MIN_COUNT）。
· 4 防覆盖不全：门2 rubric 要求集成/smoke 覆盖全路由或显式报覆盖率，不许只验易测子集蒙混。
是 B 簇真 E2E 的下一层（机器从证据强制，连"验收被骗"也堵）。
"""
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
_rows = []


def check(name, ok):
    _rows.append(bool(ok))
    print(("  ✓ " if ok else "  ✗ ") + name)


def main():
    e2e = open(os.path.join(ROOT, "bindings", "e2e-playwright.sh"), encoding="utf-8").read()
    impl = open(os.path.join(HERE, "prompts", "impl_review.md"), encoding="utf-8").read()

    # ---- item 3 防假绿：探针模板真断言内容 ----
    check("3 探针不止 wait_for_selector，还查内容(query_selector_all)", "query_selector_all" in e2e)
    check("3 探针断言真内容(非空文本 + 子元素)", "inner_text" in e2e and "children" in e2e)
    check("3 探针有占位/加载态检测(FORBID + '占位')", "FORBID" in e2e and "占位" in e2e)
    check("3 探针有 MIN_COUNT(逼真渲染条目数)", "MIN_COUNT" in e2e)
    check("3 探针失败信息含'假绿防护'", "假绿防护" in e2e)
    check("3 e2e-playwright.sh bash 语法 OK",
          subprocess.run(["bash", "-n", os.path.join(ROOT, "bindings", "e2e-playwright.sh")]).returncode == 0)
    pyblock = re.search(r"<<'PY'\n(.*?)\nPY", e2e, re.S)
    pyok = False
    if pyblock:
        try:
            compile(pyblock.group(1), "<e2e>", "exec")
            pyok = True
        except SyntaxError:
            pyok = False
    check("3 e2e 内嵌 python 语法 OK", pyok)

    # ---- item 3/4 门2 rubric 两把尺子 ----
    check("3 门2 rubric 有'防假绿'尺子", "防假绿" in impl)
    check("3 门2 防假绿点名'只验存在不验内容 = FAIL'", "只验" in impl and "假绿" in impl)
    check("4 门2 rubric 有'防覆盖不全'尺子", "防覆盖不全" in impl)
    check("4 门2 提到'易测子集' + '显式报覆盖率'", "易测子集" in impl and "覆盖率" in impl)

    npass = sum(1 for r in _rows if r)
    print("\n验收门硬化：%d/%d 绿" % (npass, len(_rows)))
    return 0 if npass == len(_rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
