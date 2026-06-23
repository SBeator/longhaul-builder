#!/usr/bin/env python3
"""test_report.py —— A 簇 dogfood 自测：report.py 从证据机器渲染 + loop 机器捕获改动文件。

四列证据表：用例 | 关键输入 | 实际是否符合预期。每条一行；全绿才算过。
验的是 A 簇的命脉：报告内容来自真实证据（不是 agent 自由发挥、不会塌成"见 evidence/"）。
"""
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import report  # noqa: E402
import loop     # noqa: E402
import state    # noqa: E402

_rows = []


def check(name, ok):
    _rows.append(ok)
    print(("  ✓ " if ok else "  ✗ ") + name)


def _w(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _setup(sd):
    ev = os.path.join(sd, "evidence", "M1")
    os.makedirs(ev, exist_ok=True)
    _w(os.path.join(sd, "events.jsonl"), "")
    state.save_milestones(sd, [
        {"id": "M1", "goal": "后端 health 接口", "acceptance": {"type": "web-e2e", "probe_cmd": "pytest -q"},
         "status": "DONE", "phase": "done", "attempt_count": 1, "max_attempts": 3, "last_error": None},
        {"id": "M2", "goal": "前端页面", "acceptance": {"type": "tdd"}, "status": "TODO",
         "phase": "plan", "attempt_count": 0, "max_attempts": 3, "last_error": None},
    ])
    _w(os.path.join(ev, "plan.md"),
       "# PLAN M1\n做法：起一个 stdlib http server，加 /api/health 端点。\n测试策略：pytest 打 200。\n")
    _w(os.path.join(ev, "red.txt"), "FAILED test_health\nEXIT_CODE=1\n")
    _w(os.path.join(ev, "green.txt"), "1 passed\nEXIT_CODE=0\n")
    _w(os.path.join(ev, "review-plan_review.json"),
       json.dumps({"verdict": "APPROVE", "ok": True,
                   "raw": "REASON: 方案合理放行。\nVERDICT: APPROVE\n", "reason": "verdict=APPROVE"},
                  ensure_ascii=False))
    _w(os.path.join(ev, "review-impl_review.json"),
       json.dumps({"verdict": "PASS", "ok": True,
                   "raw": "REASON: 实现满足探针、证据真实。\nVERDICT: PASS\n", "reason": "verdict=PASS"},
                  ensure_ascii=False))
    _w(os.path.join(ev, "verify.jsonl"),
       json.dumps({"milestone": "M1", "probe": "pytest -q", "exit_code": 0,
                   "verdict": "PASS", "sha256": "abcdef1234567890"}) + "\n")
    with open(os.path.join(ev, "screenshot.png"), "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
    _w(os.path.join(ev, "changed-files.txt"), "A\tserver.py\nM\tREADME.md\n")
    state.append_event(sd, "step_timing", milestone="M1", phase="impl", step="driver",
                       started="2026-06-23T10:00:00Z", duration_ms=5000, rc=0)


def main():
    sd = tempfile.mkdtemp(prefix="lhb-report-test-")
    _setup(sd)
    r = report.render(sd, "M1")

    check("方案摘要来自 plan.md", "/api/health" in r and "http server" in r)
    check("改了哪些文件来自 changed-files.txt（非'未捕获'兜底）", "server.py" in r and "未捕获" not in r)
    check("测试红→绿带真实 EXIT_CODE（红1/绿0）", "EXIT_CODE=1" in r and "EXIT_CODE=0" in r)
    check("门1判官裁定+理由", "APPROVE" in r and "方案合理" in r)
    check("门2判官裁定+理由", "PASS" in r and "证据真实" in r)
    check("探针 exit+sha256", "exit=0" in r and "abcdef" in r)
    check("附图列出 .png 路径", "screenshot.png" in r)
    check("其他 milestone 简报含 M2", "M2" in r)
    check("各阶段耗时段非空（5s）", "5s" in r)
    imgs = report.images(sd, "M1")
    check("report.images() 返回 screenshot.png（绑定层附图用）",
          any(p.endswith("screenshot.png") for p in imgs))

    # —— loop 机器捕获改动（真 git 仓，不信 driver 自写）——
    proj = tempfile.mkdtemp(prefix="lhb-proj-")
    for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", proj] + args, check=False, capture_output=True)
    _w(os.path.join(proj, "a.txt"), "hello\n")
    subprocess.run(["git", "-C", proj, "add", "-A"], check=False, capture_output=True)
    subprocess.run(["git", "-C", proj, "commit", "-q", "-m", "base"], check=False, capture_output=True)
    base = subprocess.run(["git", "-C", proj, "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    psd = os.path.join(proj, ".longhaul")
    os.makedirs(os.path.join(psd, "evidence"), exist_ok=True)
    _w(os.path.join(psd, "events.jsonl"), "")
    _w(os.path.join(proj, "server.py"), "print('hi')\n")   # driver 新增
    _w(os.path.join(proj, "a.txt"), "hello world\n")        # driver 改动
    loop._capture_changed_files(psd, "M1", base)
    cf = open(os.path.join(psd, "evidence", "M1", "changed-files.txt"), encoding="utf-8").read()
    check("loop 机器捕获：新增的 server.py 在内", "server.py" in cf)
    check("loop 机器捕获：改动的 a.txt 在内", "a.txt" in cf)
    check("loop 捕获非'非git仓'兜底（真跑了 git diff）", "非 git 仓" not in cf)

    ok = all(_rows)
    print("\nreport/A 自测：%d/%d 绿" % (sum(_rows), len(_rows)))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
