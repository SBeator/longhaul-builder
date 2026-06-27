#!/usr/bin/env python3
"""prompts.py 的 dogfood 自测：模板文件化 + 渲染器，产出四列证据表。

引擎自己的部件也按"证据优先、四列证据表、没证据不许声称通过"来验（F1/AC1）。
运行：python3 engine/test_prompts.py  → 退出码 0 全过 / 1 有不一致。

TDD：本测试在 prompts.py / engine/prompts/*.md 尚不存在时先写，先看红再实现到绿。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

rows = []  # (用例, 输入, 期望, 实际, 一致?)


def check(case, inp, expected, actual):
    ok = (expected == actual)
    rows.append((case, inp, str(expected), str(actual), "✅" if ok else "❌"))
    return ok


def main():
    # 样例 milestone（含 F1/AC1 要求的关键字段）
    sample = {
        "id": "M_SAMPLE",
        "goal": "把发身份算法用 TDD 实现到绿",
        "acceptance": {"type": "tdd", "probe": "python3 test_deal.py 退出码 0"},
        "max_attempts": 3,
    }
    sample_ctx = {
        "project_path": "/tmp/example-proj",
        "state_dir": "/tmp/example-proj/.longhaul",
        "carry_forward": "上一步把 schema 定了；本步只做发牌。",
        "mode": "implement",
    }
    kinds = ("driver", "plan_review", "impl_review")

    # ---- 用例 1：三个模板文件都存在且非空 --------------------------------
    for kind in kinds:
        tpl = os.path.join(HERE, "prompts", f"{kind}.md")
        exists = os.path.exists(tpl)
        size = os.path.getsize(tpl) if exists else 0
        check(f"模板 {kind}.md 存在且非空", tpl, True, exists and size > 0)

    # ---- 用例 2：import prompts 成功（模块存在）--------------------------
    try:
        import prompts  # noqa: E402
        imported = True
    except Exception as e:  # noqa: BLE001
        prompts = None
        imported = False
        check("import prompts", "import", "no-exception", repr(e))
    check("import prompts 成功", "import engine/prompts.py", True, imported)

    if not imported:
        # 模块都没有，后续渲染断言无法跑，直接出表（红）
        return _emit()

    # ---- 用例 3：三种 kind 都渲染出非空 prompt --------------------------
    rendered = {}
    for kind in kinds:
        try:
            out = prompts.render(sample, kind, sample_ctx)
        except Exception as e:  # noqa: BLE001
            out = ""
            check(f"render {kind} 不抛异常", kind, "no-exception", repr(e))
        rendered[kind] = out or ""
        check(f"render {kind} 非空", kind, True, len(rendered[kind]) > 50)

    # ---- 用例 4：关键占位被实际填充（不残留字面量）---------------------
    # goal 应出现在三种渲染结果中（driver 直接用、review 引用 milestone）
    for kind in kinds:
        out = rendered.get(kind, "")
        check(f"{kind} 填充了 goal", "{{goal}}", True, sample["goal"] in out)
        # 不残留任何 {{goal}} / {{acceptance_probe}} 这类已提供 key 的字面占位
        check(f"{kind} 无残留 {{{{goal}}}}", "{{goal}}", False, "{{goal}}" in out)
        check(f"{kind} 无残留 {{{{acceptance_probe}}}}", "{{acceptance_probe}}",
              False, "{{acceptance_probe}}" in out)

    # acceptance.probe 至少在 driver 与 impl_review 出现（这两处要按探针干活/复跑）
    probe = sample["acceptance"]["probe"]
    check("driver 填充了 acceptance_probe", "{{acceptance_probe}}", True,
          probe in rendered.get("driver", ""))
    check("impl_review 填充了 acceptance_probe", "{{acceptance_probe}}", True,
          probe in rendered.get("impl_review", ""))

    # milestone id 应被填充
    check("driver 填充了 milestone_id", "{{milestone_id}}", True,
          sample["id"] in rendered.get("driver", ""))

    # #2 走偏前移：plan 阶段要把 spec 与真实环境「对账」、不符就 plan 期举旗（治 spec 过时拖到 impl 才炸）
    check("driver(plan) 含 spec-vs-现实对账指令", "对账", True, "对账" in rendered.get("driver", ""))
    check("plan_review 含 spec-vs-现实对账维度", "对账", True, "对账" in rendered.get("plan_review", ""))
    # #12 颗粒度 sizing：plan 期估体量、太大就主动拆（别等 impl 超时被动拆）
    check("driver(plan) 含 milestone 体量自检/拆分", "体量", True, "体量" in rendered.get("driver", ""))
    check("plan_review 含颗粒度/体量维度", "体量", True, "体量" in rendered.get("plan_review", ""))

    # ---- 用例 5：未知 kind 抛清晰错误 -----------------------------------
    raised = False
    try:
        prompts.render(sample, "bogus_kind", sample_ctx)
    except Exception:  # noqa: BLE001
        raised = True
    check("未知 kind 抛异常", "render(.., 'bogus_kind')", True, raised)

    # ---- 用例 6：ctx 省略也能渲染（未知占位降级，不崩）-----------------
    try:
        out_noctx = prompts.render(sample, "driver")
        noctx_ok = len(out_noctx) > 50
    except Exception:  # noqa: BLE001
        noctx_ok = False
    check("render 省略 ctx 不崩", "render(sample,'driver')", True, noctx_ok)

    return _emit()


def _emit():
    print("\n用例 | 输入 | 期望 | 实际 | 一致")
    print("--- | --- | --- | --- | ---")
    allok = True
    for c, i, e, a, ok in rows:
        print(f"{c} | {i} | {e} | {a} | {ok}")
        allok = allok and ok == "✅"
    npass = sum(1 for r in rows if r[4] == "✅")
    print(f"\n{'ALL PASS ✅' if allok else 'FAIL ❌'} ({npass}/{len(rows)})")
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
