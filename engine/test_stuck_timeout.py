#!/usr/bin/env python3
"""test_stuck_timeout.py —— #1 进度感知超时（卡死检测）。

修最初"硬墙 1500s 把还在写代码/还在产出的 driver 也杀掉"的白烧：
driver 长跑只在「既无文件改动又无输出」持续 stuck_timeout 秒才判死；慢但在产出的不杀；
跑满 ceiling 秒才撞兜底天花板。探针/判官不受影响（仍硬墙短超时）。

真子进程集成（sleep/touch/echo），时序取小（秒级）。
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import verify  # noqa: E402
import loop    # noqa: E402

_rows = []


def check(name, ok):
    _rows.append(bool(ok))
    print(("  ✓ " if ok else "  ✗ ") + name)


def _run(cmd, cwd, timeout, stuck=None, pdir=None):
    return verify._run_cmd(cmd, use_shell=True, cwd=cwd, env=None,
                           timeout=timeout, max_bytes=verify.DEFAULT_MAX_BYTES,
                           stuck_timeout=stuck, progress_dir=pdir)


def main():
    # 1) 卡死（无文件改动、无输出）→ 在 stuck 窗就被杀，远早于天花板
    pd = tempfile.mkdtemp(prefix="lhb-stuck-pd-")
    t0 = time.monotonic()
    ec, raw, to, dur = _run("sleep 30", pd, timeout=60, stuck=2, pdir=pd)
    el = time.monotonic() - t0
    check("卡死:无进展→超时杀(timed_out)", to is True and ec is None)
    check("卡死:在 stuck 窗就杀(~2s，远早于 60s 天花板)", el < 8)

    # 2) 持续产出文件 → 不被卡死误杀，正常跑完(exit 0)
    pd2 = tempfile.mkdtemp(prefix="lhb-prog-pd-")
    cmd2 = 'for i in 1 2 3 4; do touch "%s/f$i"; sleep 1; done' % pd2
    ec2, _, to2, _ = _run(cmd2, pd2, timeout=60, stuck=3, pdir=pd2)
    check("在产出(每1s碰文件,stuck=3):不误杀、跑完 exit 0", to2 is False and ec2 == 0)

    # 3) 输出活动也算进展（只 echo、不碰文件）→ 不被卡死误杀
    pd3 = tempfile.mkdtemp(prefix="lhb-out-pd-")   # 空目录，命令不往里写
    cmd3 = 'for i in 1 2 3 4 5 6; do echo tick; sleep 0.5; done'
    ec3, _, to3, _ = _run(cmd3, pd3, timeout=60, stuck=2, pdir=pd3)
    check("只输出不碰文件(stuck=2):输出算进展、不误杀", to3 is False and ec3 == 0)

    # 4) 兜底天花板：一直碰文件(永不卡死)但超过 ceiling → 仍被杀
    pd4 = tempfile.mkdtemp(prefix="lhb-ceil-pd-")
    cmd4 = 'while true; do touch "%s/f"; sleep 0.3; done' % pd4
    t4 = time.monotonic()
    ec4, _, to4, _ = _run(cmd4, pd4, timeout=3, stuck=30, pdir=pd4)
    el4 = time.monotonic() - t4
    check("天花板:一直碰文件也在 ceiling 杀", to4 is True and ec4 is None)
    check("天花板:~ceiling(3s)被杀，不无限跑", el4 < 9)

    # 5) 向后兼容：不给 stuck 参数 = 原硬墙超时不变
    pd5 = tempfile.mkdtemp(prefix="lhb-hard-pd-")
    ec5, _, to5, _ = _run("sleep 10", pd5, timeout=1)   # 无 stuck/pdir
    check("向后兼容:无 stuck 参→硬墙超时照旧", to5 is True and ec5 is None)
    ec5b, _, to5b, _ = _run("true", pd5, timeout=5)
    check("向后兼容:正常命令 exit 0 不变", to5b is False and ec5b == 0)

    # 6) invoke_driver 区分「卡死早杀」vs「撞天花板」的 reason
    proj = tempfile.mkdtemp(prefix="lhb-drv-")
    sd = os.path.join(proj, ".longhaul")
    os.makedirs(sd, exist_ok=True)
    rc_s, reason_s = loop.invoke_driver("sleep 30", "p", sd, "M1", "implement",
                                        timeout=60, stuck_timeout=2)
    check("reason:卡死→RC_INFRA + 含『stuck/续跑』", rc_s == loop.RC_INFRA and "stuck" in reason_s)
    rc_c, reason_c = loop.invoke_driver('while true; do touch "%s/f"; sleep 0.3; done' % proj,
                                        "p", sd, "M1", "implement", timeout=3, stuck_timeout=30)
    check("reason:撞天花板→RC_INFRA + 含『天花板』", rc_c == loop.RC_INFRA and "天花板" in reason_c)

    npass = sum(1 for r in _rows if r)
    print("\n进度感知超时(#1 卡死检测)：%d/%d 绿" % (npass, len(_rows)))
    return 0 if npass == len(_rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
