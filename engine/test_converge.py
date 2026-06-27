#!/usr/bin/env python3
"""test_converge.py —— 提案↔审 收敛能力（longhaul P0 spec 双 agent 收敛的地基）。

stub proposer/reviewer 用真 shell 脚本（零网络、确定性）；覆盖：一审即过 / 一直打回撞上限 escalate /
改一轮后通过 / proposer 真改了 artifact / 轮数计数准 / 畸形输出保守判 REVISE。
"""
import os
import stat
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import converge  # noqa: E402

_rows = []


def check(name, ok):
    _rows.append(bool(ok))
    print(("  ✓ " if ok else "  ✗ ") + name)


def _stub(d, name, body):
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write("#!/usr/bin/env bash\n" + body + "\n")
    os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return p


def main():
    d = tempfile.mkdtemp(prefix="lhb-conv-")
    rev_appr = _stub(d, "ra.sh", 'echo "VERDICT: APPROVE"')
    rev_rev = _stub(d, "rr.sh", 'echo "VERDICT: REVISE"; echo "REASON: 还不行"')
    # 条件 reviewer：artifact 里有 FIXED 才放行
    rev_cond = _stub(d, "rc.sh",
                     'if grep -q FIXED "$1"; then echo "VERDICT: APPROVE"; '
                     'else echo "VERDICT: REVISE"; echo "REASON: 加上 FIXED"; fi')
    prop_fix = _stub(d, "pf.sh", 'echo FIXED >> "$1"')   # proposer：往 artifact 追加 FIXED

    art = os.path.join(d, "spec.md")

    # 1) 一审即过 → 1 轮收敛
    open(art, "w").write("draft\n")
    r = converge.converge(art, "true", "bash %s" % rev_appr, max_rounds=3)
    check("一审即过 → converged, rounds=1, 不升级",
          r["converged"] and r["rounds"] == 1 and not r["escalate"])

    # 2) 一直 REVISE → 撞 3 轮上限、escalate
    open(art, "w").write("draft\n")
    r = converge.converge(art, "true", "bash %s" % rev_rev, max_rounds=3)
    check("一直打回 → 撞上限不收敛, rounds=3, escalate=True",
          (not r["converged"]) and r["rounds"] == 3 and r["escalate"])

    # 3) 改一轮后通过 → 2 轮收敛 + proposer 真改了 artifact
    open(art, "w").write("draft\n")   # 无 FIXED
    r = converge.converge(art, "bash %s {artifact}" % prop_fix, "bash %s {artifact}" % rev_cond, max_rounds=3)
    check("改一轮后通过 → converged, rounds=2", r["converged"] and r["rounds"] == 2)
    check("proposer 真就地改了 artifact（加了 FIXED）", "FIXED" in open(art, encoding="utf-8").read())
    check("history 逐轮可复盘（第1轮 REVISE→第2轮 APPROVE）",
          [h["verdict"] for h in r["history"]] == ["REVISE", "APPROVE"])

    # 4) 轮次上限可配
    open(art, "w").write("draft\n")
    r = converge.converge(art, "true", "bash %s" % rev_rev, max_rounds=1)
    check("max_rounds=1 → 1 轮就升级", r["rounds"] == 1 and r["escalate"])

    # 5) 解析稳健
    check("无 VERDICT → 保守判 REVISE", converge.parse_verdict("blah blah")[0] == "REVISE")
    check("解析 APPROVE", converge.parse_verdict("一些分析\nVERDICT: APPROVE\nREASON: ok")[0] == "APPROVE")
    check("解析 REVISE + 抽 feedback",
          converge.parse_verdict("VERDICT: REVISE\nREASON: 缺 X")[1].startswith("缺 X"))

    npass = sum(1 for r in _rows if r)
    print("\n提案↔审 收敛(converge)：%d/%d 绿" % (npass, len(_rows)))
    return 0 if npass == len(_rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
