#!/usr/bin/env python3
"""state.py 的 dogfood 自测：确定性走一遍状态机 + 熔断，产出四列证据表。

引擎自己的部件也按"证据优先、四列证据表、没证据不许声称通过"来验。
运行：python3 engine/test_state.py  → 退出码 0 全过 / 1 有不一致。
"""
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import state  # noqa: E402

rows = []  # (用例, 输入, 期望, 实际, 一致?)


def run(*argv):
    """跑一条 state CLI，返回 (exit_code, stdout)。"""
    buf = io.StringIO()
    code = 1
    with redirect_stdout(buf):
        try:
            code = state.main(list(argv))
        except SystemExit as e:  # argparse 错误
            code = int(e.code) if e.code else 0
    return code, buf.getvalue().strip()


def check(case, inp, expected, actual):
    ok = (expected == actual)
    rows.append((case, inp, str(expected), str(actual), "✅" if ok else "❌"))
    return ok


def main():
    d = tempfile.mkdtemp(prefix="lhb-test-")
    run("init", d, "--one-liner", "测试需求")
    cur = state.load_cursor(d)
    check("init 后 phase", "init", "age", cur.get("phase"))

    # 写 milestones 文件并载入
    msfile = os.path.join(d, "ms.json")
    with open(msfile, "w", encoding="utf-8") as f:
        json.dump({"milestones": [
            {"id": "M1", "goal": "后端结算算法", "acceptance": {"type": "tdd", "probe": "pytest"}, "max_attempts": 3},
            {"id": "M2", "goal": "前端页面", "acceptance": {"type": "web-e2e", "probe": "browser"}, "max_attempts": 3},
        ]}, f)
    run("set-milestones", d, "--file", msfile)
    check("set-milestones 后 phase", "2 milestones", "build", state.load_cursor(d).get("phase"))

    # 程序计数器：next 应指向 M1
    _, out = run("next", d)
    check("next 指向", "首个 TODO", "M1", json.loads(out)["milestone"]["id"])

    # claim → 进 plan，attempt 不变（F2 计数点已从 claim 移到"进 impl"）
    run("claim", d, "M1")
    m1 = state._find(state.load_milestones(d), "M1")
    # F2: intentional semantic change — claim 不再 +1；attempt 只在进 impl 时 +1（_enter_impl 单点计数）。
    # 旧断言期望 claim 后 A==1，迁移为：claim 后 A 仍为 0，且 phase=plan。
    check("claim M1 后 attempt(不再+1)", "claim", 0, m1["attempt_count"])
    check("claim M1 后 phase", "claim", "plan", m1.get("phase"))
    check("claim M1 后 status", "claim", "IN_PROGRESS", m1["status"])
    # F2: 把"A==1"的语义覆盖迁到"门1放行进 impl 后"——首次进 impl 才 +1。
    run("advance-phase", d, "M1")
    run("gate-pass", d, "M1", "--gate", "plan")
    m1 = state._find(state.load_milestones(d), "M1")
    check("门1放行后 attempt", "gate-pass plan", 1, m1["attempt_count"])
    check("门1放行后 phase", "gate-pass plan", "impl", m1.get("phase"))

    # fail → 留 IN_PROGRESS+impl 重驱（未超上限）
    run("advance-phase", d, "M1")  # impl→impl_review（先交门2）
    run("fail", d, "M1", "--error", "测试没过")
    m1 = state._find(state.load_milestones(d), "M1")
    # F2: intentional semantic change — fail 不再回 TODO，而是留 status=IN_PROGRESS+phase=impl，
    # 让 _next_todo 仍重发该 milestone（活循环把门2打回的实现再次派给 driver 重驱的路径）。
    check("fail M1 后留 IN_PROGRESS 重驱", "fail attempt1", "IN_PROGRESS", m1["status"])
    check("fail M1 后 phase=impl", "fail attempt1", "impl", m1.get("phase"))

    # 再 complete → 推进到 M2（complete 任意相位均可收口）
    run("complete", d, "M1")
    _, out = run("next", d)
    check("complete M1 后 next", "complete", "M2", json.loads(out)["milestone"]["id"])

    # M2 完成 → done
    run("claim", d, "M2")
    run("complete", d, "M2")
    _, out = run("next", d)
    check("全完成后 state", "complete all", "done", json.loads(out)["state"])

    # 熔断：M3 连续失败到上限 → BLOCKED + claim 退出码 3
    msfile2 = os.path.join(d, "ms2.json")
    with open(msfile2, "w", encoding="utf-8") as f:
        json.dump({"milestones": [
            {"id": "M3", "goal": "会卡住的活", "max_attempts": 2},
        ]}, f)
    run("set-milestones", d, "--file", msfile2)
    run("claim", d, "M3"); run("fail", d, "M3", "--error", "环境死结")  # attempt1
    code, _ = run("claim", d, "M3")  # attempt2
    run("fail", d, "M3", "--error", "还是死结")  # attempt2 达上限 → BLOCKED
    m3 = state._find(state.load_milestones(d), "M3")
    check("熔断后 status", "fail 到上限", "BLOCKED", m3["status"])
    code2, _ = run("claim", d, "M3")  # 已 BLOCKED 再 claim
    check("熔断后 claim 退出码", "claim blocked", 3, code2)

    # 打印四列证据表
    print("\n用例 | 输入 | 期望 | 实际 | 一致")
    print("--- | --- | --- | --- | ---")
    allok = True
    for c, i, e, a, ok in rows:
        print(f"{c} | {i} | {e} | {a} | {ok}")
        allok = allok and ok == "✅"
    print(f"\n{'ALL PASS ✅' if allok else 'FAIL ❌'} ({sum(1 for r in rows if r[4]=='✅')}/{len(rows)})")
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
