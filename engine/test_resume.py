#!/usr/bin/env python3
"""test_resume.py —— P0「超时不白跑」回归：impl 重试时 driver prompt 注入「续跑」上下文。

修复：driver 改的是真文件、超时被杀代码还在盘上，旧逻辑拿同 prompt 从头重来＝白跑。
现在：本步发生过 infra_retry（上次被中断）且工作区有未提交改动 → driver prompt 注入
「已有部分进展(改了这些文件)、git status 看进度、接着做完别重来」。
"""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import loop      # noqa: E402
import state     # noqa: E402

_rows = []


def check(name, expected, actual):
    ok = expected == actual
    _rows.append((name, str(expected), str(actual), "✅" if ok else "❌"))
    print(("  ✓ " if ok else "  ✗ ") + name + ("" if ok else " (期望:%r 实际:%r)" % (expected, actual)))
    return ok


def _git(d, *a):
    subprocess.run(["git", "-C", d, *a], capture_output=True, text=True)


def _mk_git_proj():
    proj = tempfile.mkdtemp(prefix="lhb-resume-")
    _git(proj, "init", "-q")
    _git(proj, "config", "user.email", "t@t")
    _git(proj, "config", "user.name", "t")
    with open(os.path.join(proj, "seed.txt"), "w") as f:
        f.write("seed\n")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-qm", "seed")
    sd = os.path.join(proj, ".longhaul")
    state.main(["init", sd, "--one-liner", "resume test"])
    return proj, sd


def main():
    proj, sd = _mk_git_proj()

    # 全新 impl（无 infra_retry）→ 无续跑上下文
    check("全新 impl 无续跑上下文", "", loop._resume_note(sd, "M1", "implement"))

    # plan-only 永不注入续跑（即便有 retry）
    cur = state.load_cursor(sd); cur["infra_retries"] = {"M1": 2}; state.save_cursor(sd, cur)
    check("plan-only 不注入续跑", "", loop._resume_note(sd, "M1", "plan-only"))

    # 模拟上次被中断留下的部分进展（改一个文件 + 新增一个未跟踪文件）
    with open(os.path.join(proj, "seed.txt"), "a") as f:
        f.write("partial work from interrupted attempt\n")
    with open(os.path.join(proj, "new_module.py"), "w") as f:
        f.write("# half-written\n")
    note = loop._resume_note(sd, "M1", "implement")
    check("impl 重试 → 注入续跑（非空）", True, bool(note.strip()))
    check("续跑含'接着做完'指令", True, "接着做完" in note)
    check("续跑含'别从头重来/绝不'", True, ("从头重来" in note or "从零重来" in note))
    check("续跑列出已改文件 seed.txt", True, "seed.txt" in note)
    check("续跑列出未跟踪新文件 new_module.py", True, "new_module.py" in note)
    check("续跑提示 git status/diff 看进度", True, ("git status" in note or "git diff" in note))

    # retry 但工作区无改动（上次刚启动就被杀）→ 不注入（按全新做）
    proj2, sd2 = _mk_git_proj()
    cur2 = state.load_cursor(sd2); cur2["infra_retries"] = {"M1": 1}; state.save_cursor(sd2, cur2)
    check("retry 但工作区无改动 → 不注入", "", loop._resume_note(sd2, "M1", "implement"))

    # _driver_ctx 把 resume_context 带进上下文（键名对齐 driver.md 占位）
    ctx = loop._driver_ctx(sd, "M1", "implement")
    check("_driver_ctx 含 resume_context 键", True, "resume_context" in ctx)
    check("_driver_ctx[implement] 的 resume_context 非空（有进展）", True, bool(ctx["resume_context"].strip()))

    npass = sum(1 for r in _rows if r[3] == "✅")
    print("\n%s (%d/%d)" % ("ALL PASS ✅" if npass == len(_rows) else "SOME FAIL ❌", npass, len(_rows)))
    return 0 if npass == len(_rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
