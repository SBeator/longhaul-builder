#!/usr/bin/env python3
"""test_stage_agents.py —— #10a 分阶段可配不同 agent 的地基。

能力：每个阶段（plan / impl 的 driver，plan_review / impl_review 的 judge）可各配一个命令槽，
没配就回落通用槽 → **完全向后兼容**。未来 test/e2e/验收 阶段同理扩展。

立场（镜像 test_loop 红线）：只在 tempfile.mkdtemp() 里造 state，绝不碰 LIVE .longhaul；
不跑真 agent —— driver 用 stub 捕获、judge 用 monkeypatch 捕获 kind。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import loop    # noqa: E402
import review  # noqa: E402
import state   # noqa: E402

_rows = []


def check(name, ok):
    _rows.append(bool(ok))
    print(("  ✓ " if ok else "  ✗ ") + name)


class _NS:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _mk():
    proj = tempfile.mkdtemp(prefix="lhb-stage-")
    sd = os.path.join(proj, ".longhaul")
    state.main(["init", sd, "--one-liner", "分阶段 agent 测试靶子"])
    msf = os.path.join(proj, "ms.json")
    json.dump({"milestones": [
        {"id": "M1", "goal": "造个文件证明 driver 跑过",
         "acceptance": {"type": "integration", "probe": "test -f", "probe_cmd": "true"},
         "max_attempts": 3}]}, open(msf, "w", encoding="utf-8"), ensure_ascii=False)
    state.main(["set-milestones", sd, "--file", msf])
    state.main(["p0-confirm", sd, "--by", "test"])
    return sd


def main():
    # ---- 1) driver 解析：分阶段 env > 显式 CLI > 通用 env > 空哨兵 ----
    check("driver:全空→空串(降级)", loop.resolve_driver_cmd(env={}) == "")
    check("driver:通用 env", loop.resolve_driver_cmd(env={"LONGHAUL_DRIVER_CMD": "G"}) == "G")
    check("driver:显式 CLI > 通用 env",
          loop.resolve_driver_cmd("X", env={"LONGHAUL_DRIVER_CMD": "G"}) == "X")
    check("driver:分阶段 env > 显式 CLI",
          loop.resolve_driver_cmd("X", env={"LONGHAUL_DRIVER_CMD": "G", "LONGHAUL_DRIVER_CMD__impl": "I"},
                                  phase="impl") == "I")
    check("driver:该阶段未配→回落通用(向后兼容)",
          loop.resolve_driver_cmd(env={"LONGHAUL_DRIVER_CMD": "G", "LONGHAUL_DRIVER_CMD__impl": "I"},
                                  phase="plan") == "G")
    check("driver:phase=None 即旧行为(不看分阶段槽)",
          loop.resolve_driver_cmd(env={"LONGHAUL_DRIVER_CMD__impl": "I"}) == "")

    # ---- 2) judge 解析：分阶段 kind > 显式 > 通用 > 空哨兵（与 driver 同构）----
    check("judge:通用 env", review.resolve_judge_cmd(env={"LONGHAUL_JUDGE_CMD": "G"}) == "G")
    check("judge:分阶段 kind > 显式",
          review.resolve_judge_cmd("X", env={"LONGHAUL_JUDGE_CMD": "G", "LONGHAUL_JUDGE_CMD__plan_review": "P"},
                                   kind="plan_review") == "P")
    check("judge:该 kind 未配→回落通用",
          review.resolve_judge_cmd(env={"LONGHAUL_JUDGE_CMD": "G", "LONGHAUL_JUDGE_CMD__plan_review": "P"},
                                   kind="impl_review") == "G")
    check("judge:kind=None 即旧行为", review.resolve_judge_cmd("X", env={}) == "X")

    # ---- 3) 接线：_phase_plan/_phase_impl 各按对应阶段解析 driver（端到端捕获实际命令）----
    sd = _mk()
    m = state.load_milestones(sd)[0]
    opts = loop._opts_from_args(_NS())   # driver_cmd 默认 None → 走 env 分阶段
    os.environ["LONGHAUL_DRIVER_CMD__plan"] = "PLAN_CMD_X"
    os.environ["LONGHAUL_DRIVER_CMD__impl"] = "IMPL_CMD_Y"
    cap = []
    real_invoke = loop.invoke_driver
    # stub：捕获被解析出的命令，回 INFRA 让相位函数在 advance 前提前返回（不需要真跑/真转移）
    loop.invoke_driver = lambda cmd, *a, **k: (cap.append(cmd), (loop.RC_INFRA, "stub"))[1]
    try:
        loop._phase_plan(sd, m, opts)
        check("接线:_phase_plan 用 plan 阶段 driver", cap and cap[-1] == "PLAN_CMD_X")
        loop._phase_impl(sd, m, opts)
        check("接线:_phase_impl 用 impl 阶段 driver", cap and cap[-1] == "IMPL_CMD_Y")
    finally:
        loop.invoke_driver = real_invoke
        os.environ.pop("LONGHAUL_DRIVER_CMD__plan", None)
        os.environ.pop("LONGHAUL_DRIVER_CMD__impl", None)

    # ---- 4) 接线：review() 把 kind 透传给 resolve_judge_cmd（分阶段 judge 真生效）----
    seen = []
    real_resolve = review.resolve_judge_cmd
    review.resolve_judge_cmd = lambda explicit=None, env=None, kind=None: (seen.append(kind), "")[1]
    try:
        review.review(sd, "M1", kind="plan_review")
    finally:
        review.resolve_judge_cmd = real_resolve
    check("接线:review() 把 kind 透给 resolve_judge_cmd", seen == ["plan_review"])

    # ---- 5) 测试独立 agent（课题）：resolve_test_cmd 优先级 + test 模板可渲染 ----
    check("test_cmd:未配→空(不启用=老行为)", loop.resolve_test_cmd(env={}) == "")
    check("test_cmd:LONGHAUL_TEST_CMD 兜底", loop.resolve_test_cmd(env={"LONGHAUL_TEST_CMD": "T"}) == "T")
    check("test_cmd:分阶段 __test 最优先",
          loop.resolve_test_cmd(env={"LONGHAUL_TEST_CMD": "T", "LONGHAUL_DRIVER_CMD__test": "X"}) == "X")
    import prompts  # noqa: E402
    check("test 模板可渲染(独立测试 agent)",
          "独立" in prompts.render({"id": "M1", "goal": "g", "acceptance": {"probe": "p"}}, "test", {}))

    npass = sum(1 for r in _rows if r)
    print("\n分阶段可配 agent(#10a)：%d/%d 绿" % (npass, len(_rows)))
    return 0 if npass == len(_rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
