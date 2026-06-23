#!/usr/bin/env python3
"""test_design_fidelity.py —— C 簇自测：设计入冻结 spec + 门2 fidelity 尺子 + driver 不无声降级。

C 簇大部分是"把约定写进 spec 骨架 / 判官 prompt / driver prompt"，靠这个结构测守住意图不被悄悄改掉
（对应 ai-cockpit 复盘：设计稿没进 spec、门2 不核对设计、driver 无声降级）。四列证据表，全绿才算过。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import age  # noqa: E402

_rows = []


def check(name, ok):
    _rows.append(ok)
    print(("  ✓ " if ok else "  ✗ ") + name)


def _read(rel):
    with open(os.path.join(HERE, rel), encoding="utf-8") as f:
        return f.read()


def main():
    sk = age.skeleton("做一个统一入口网站")
    check("spec 骨架含「设计 / 架构」节", "## 设计 / 架构" in sk)
    check("设计节含视觉/形态意图占位", "视觉" in sk and "形态" in sk)
    check("设计节点名 iframe vs 原生 关键取舍", "iframe" in sk and "原生" in sk)
    check("REQUIRED_SECTIONS 把设计/架构列为必含", "## 设计 / 架构" in age.REQUIRED_SECTIONS)
    check("老化追问含'设计/架构'问题", any("架构" in q for q in age.SOCRATIC_QUESTIONS))

    ir = _read("prompts/impl_review.md")
    check("门2 加了'忠于设计'fidelity 尺子", "忠于" in ir and "设计 / 架构" in ir)
    check("门2：偏离设计判 FAIL", "偏离设计 = FAIL" in ir)
    check("门2 据 flag.json 区分'无声降级'与'已举旗'", "flag.json" in ir)

    dr = _read("prompts/driver.md")
    check("driver 写明做到完备/不无声降级", "完备" in dr and "无声降级" in dr)
    check("driver 举旗约定指向 flag.json", "flag.json" in dr)
    check("driver 列出两种举旗 kind", "blocked-workaround" in dr and "spec-divergence" in dr)

    ok = all(_rows)
    print("\ndesign-fidelity/C 自测：%d/%d 绿" % (sum(_rows), len(_rows)))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
