#!/usr/bin/env python3
"""test_self_recovery.py —— item5「熔断前先自救一次」回归。

撞产品熔断阈值(max_attempts)时，第一次不直接 BLOCKED，而是给一次"换个根本不同 approach"的实施
机会（推强提示 note + self_recovery 事件）；自救也失败（再撞上限）才真熔断升级人工。
减少"撞熔断就硬停等人 redirect"的人工阻塞（ai-cockpit AC3 教训）。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import state   # noqa: E402

_rows = []


def check(name, ok):
    _rows.append(bool(ok))
    print(("  ✓ " if ok else "  ✗ ") + name)


def _mk():
    sd = tempfile.mkdtemp(prefix="lhb-selfrec-") + "/.longhaul"
    state.main(["init", sd, "--one-liner", "x"])
    return sd


def _events(sd):
    p = os.path.join(sd, "events.jsonl")
    return [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()] if os.path.exists(p) else []


def main():
    sd = _mk()

    # 正常（未撞上限）：attempt 0→1 < max 3 → 进 impl，不自救不熔断
    m = {"id": "M1", "goal": "g", "acceptance": {}, "status": "IN_PROGRESS",
         "phase": "impl_review", "attempt_count": 0, "max_attempts": 3, "last_error": None}
    rc = state._enter_impl(sd, [m], m)
    check("未撞上限 → 进 impl 退 0", rc == 0 and m["status"] == "IN_PROGRESS" and m["phase"] == "impl")
    check("未撞上限 → 没触发自救", not m.get("self_recovery_used"))

    # 撞上限第一次（attempt 2→3 == max 3）→ 自救：不熔断、换 approach 重试
    m2 = {"id": "M2", "goal": "g", "acceptance": {}, "status": "IN_PROGRESS",
          "phase": "impl_review", "attempt_count": 2, "max_attempts": 3, "last_error": "门2 FAIL"}
    rc2 = state._enter_impl(sd, [m2], m2)
    check("撞上限首次 → 自救不熔断（退 0、留 impl）", rc2 == 0 and m2["status"] == "IN_PROGRESS" and m2["phase"] == "impl")
    check("撞上限首次 → 标记 self_recovery_used", m2.get("self_recovery_used") is True)
    note_txt = " ".join(n.get("text", "") for n in (m2.get("note") or []))
    check("自救推了'换 approach'提示 note", "换" in note_txt and "approach" in note_txt)
    check("自救清了 last_error", m2.get("last_error") is None)
    check("记 self_recovery 事件", any(e["ev"] == "self_recovery" and e.get("milestone") == "M2" for e in _events(sd)))

    # 自救后再撞上限（attempt 3→4 > max，used）→ 真熔断
    rc3 = state._enter_impl(sd, [m2], m2)
    check("自救后再撞上限 → 真熔断（退 3、BLOCKED）", rc3 == 3 and m2["status"] == "BLOCKED")
    check("熔断记 circuit_break 事件", any(e["ev"] == "circuit_break" and e.get("milestone") == "M2" for e in _events(sd)))

    # 退化：max=1（attempt 0→1 >= 1 但 attempt 不 >1）→ 不自救、直接熔断
    m3 = {"id": "M3", "goal": "g", "acceptance": {}, "status": "IN_PROGRESS",
          "phase": "plan", "attempt_count": 0, "max_attempts": 1, "last_error": None}
    rc4 = state._enter_impl(sd, [m3], m3)
    check("max=1 退化 → 不自救、直接熔断", rc4 == 3 and m3["status"] == "BLOCKED" and not m3.get("self_recovery_used"))

    npass = sum(1 for r in _rows if r)
    print("\n熔断前自救：%d/%d 绿" % (npass, len(_rows)))
    return 0 if npass == len(_rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
