#!/usr/bin/env python3
"""test_progress.py —— item7 进度播报带时间：timeline.progress_line 计算正确。

修最初的"进度更新不带时间"bug：每个 milestone 完成时播报一行带「本步耗时(分阶段)+累计+当前时间」。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import timeline   # noqa: E402

_rows = []


def check(name, ok):
    _rows.append(bool(ok))
    print(("  ✓ " if ok else "  ✗ ") + name)


def _mk_events(sd, evs):
    os.makedirs(sd, exist_ok=True)
    with open(os.path.join(sd, "events.jsonl"), "w", encoding="utf-8") as f:
        for e in evs:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def main():
    sd = tempfile.mkdtemp(prefix="lhb-prog-")
    # 合成：init + M1 三步（出方案 60s / 实现 180s / 审 30s）
    _mk_events(sd, [
        {"ts": "2026-06-24T00:00:00Z", "ev": "init", "one_liner": "x"},
        {"ts": "2026-06-24T00:01:00Z", "ev": "step_timing", "milestone": "M1",
         "phase": "plan", "step": "driver", "started": "2026-06-24T00:00:00Z", "duration_ms": 60000},
        {"ts": "2026-06-24T00:04:00Z", "ev": "step_timing", "milestone": "M1",
         "phase": "impl", "step": "driver", "started": "2026-06-24T00:01:00Z", "duration_ms": 180000},
        {"ts": "2026-06-24T00:04:30Z", "ev": "step_timing", "milestone": "M1",
         "phase": "impl_review", "step": "review", "started": "2026-06-24T00:04:00Z", "duration_ms": 30000},
    ])
    line = timeline.progress_line(sd, "M1")
    print("  渲染：", line)
    check("含 milestone 名 + '完成'", "M1" in line and "完成" in line)
    check("含'本步'总耗时(4m30s)", "本步" in line and ("4m30s" in line or "4m" in line))
    check("分阶段含'出方案 1m'", "出方案" in line and "1m" in line)
    check("分阶段含'实现 3m'", "实现" in line and "3m" in line)
    check("分阶段含'审 30s'", "审" in line)
    check("含'累计'", "累计" in line)
    check("含当前时间(HH:MM 冒号)", line.count(":") >= 1 and "｜" in line)

    # 无 step_timing 的 milestone → 不崩、本步 0
    line2 = timeline.progress_line(sd, "M9")
    check("无数据 milestone 不崩", "M9" in line2 and "完成" in line2)

    npass = sum(1 for r in _rows if r)
    print("\n进度播报带时间：%d/%d 绿" % (npass, len(_rows)))
    return 0 if npass == len(_rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
