#!/usr/bin/env python3
"""verify.py 的 dogfood 自测：确定性证据闸的反作弊 + 健壮性，产出四列证据表。

立场（DESIGN §2.4① / spec AC3）：裁定**只看真实退出码**，绝不读探针 stdout 里的文字。
反作弊核心 TC3（echo "ALL TESTS PASSED"; exit 1 → FAIL）+ 对称面 TC8（echo FAIL; exit 0 → PASS）
双向证明输出文本不影响裁定。纯标准库、tempfile 临时 state_dir、探针用系统自带命令（不烧 LLM）。

运行：python3 engine/test_verify.py  → 退出码 0 全过 / 1 有不一致。
"""
import hashlib
import io
import json
import os
import sys
import tempfile
import time
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import verify  # noqa: E402  (RED 阶段此 import 失败 = 预期的红)

rows = []  # (用例, 输入, 期望, 实际, 一致?)


def run(*argv):
    """跑一条 verify CLI，返回 (exit_code, stdout)。"""
    buf = io.StringIO()
    code = 1
    with redirect_stdout(buf):
        try:
            code = verify.main(list(argv))
        except SystemExit as e:  # argparse 用法错
            code = int(e.code) if e.code else 0
    return code, buf.getvalue().strip()


def check(case, inp, expected, actual):
    ok = (expected == actual)
    rows.append((case, inp, str(expected), str(actual), "✅" if ok else "❌"))
    return ok


def _mk_statedir():
    """造一个最小 state_dir（含 events.jsonl 父目录），返回 (project_root, state_dir)。"""
    root = tempfile.mkdtemp(prefix="lhb-verify-test-")
    sd = os.path.join(root, ".longhaul")
    os.makedirs(sd, exist_ok=True)
    return root, sd


def main():
    # ===== TC1 通过探针 → PASS + 证据落盘 =====
    _, sd = _mk_statedir()
    r = verify.verify(sd, "F3", "true", name="tc1", use_shell=True)
    check("TC1 pass探针 verdict", "true; --shell", "PASS", r["verdict"])
    check("TC1 pass探针 exit_code", "true", 0, r["exit_code"])
    check("TC1 证据文件存在", "true", True, os.path.exists(r["evidence_path"]))
    code, _ = run(sd, "F3", "--probe", "true", "--name", "tc1cli")
    check("TC1 verify.py 退出码=0(PASS)", "CLI true", 0, code)

    # ===== TC2 证据含真实输出 + EXIT_CODE 行（既有约定）=====
    _, sd = _mk_statedir()
    r = verify.verify(sd, "F3", "printf 'hello\\n'; exit 0", name="tc2", use_shell=True)
    body = open(r["evidence_path"], encoding="utf-8").read()
    check("TC2 证据含真实输出 hello", "printf hello", True, "hello" in body)
    check("TC2 证据含 EXIT_CODE=0 行", "printf hello", True, "\nEXIT_CODE=0" in body)

    # ===== TC3 反作弊核心：打印"通过"却退非零 → FAIL =====
    _, sd = _mk_statedir()
    r = verify.verify(sd, "F3", 'echo "ALL TESTS PASSED"; exit 1', name="tc3", use_shell=True)
    check("TC3 反作弊 verdict(退码压谎言)", 'echo PASSED;exit1', "FAIL", r["verdict"])
    check("TC3 反作弊 exit_code", "exit 1", 1, r["exit_code"])
    code, _ = run(sd, "F3", "--probe", 'echo "ALL TESTS PASSED"; exit 1', "--name", "tc3cli")
    check("TC3 verify.py 退出码=1(FAIL)", "CLI echo;exit1", 1, code)

    # ===== TC8 对称面：好文字坏文字都不影响裁定（echo FAIL; exit 0 → PASS）=====
    _, sd = _mk_statedir()
    r = verify.verify(sd, "F3", 'echo "FAIL FAIL FAIL"; exit 0', name="tc8", use_shell=True)
    check("TC8 对称反作弊 verdict(退0压坏字样)", 'echo FAIL;exit0', "PASS", r["verdict"])

    # ===== TC4 命令不存在 → FAIL 不崩（exit_code None）=====
    _, sd = _mk_statedir()
    crashed = False
    try:
        r = verify.verify(sd, "F3", "no_such_cmd_xyz_42", name="tc4", use_shell=False)
    except Exception:
        crashed = True
        r = {"verdict": "ERR", "exit_code": "EXC"}
    check("TC4 缺命令不抛异常", "no_such_cmd argv", False, crashed)
    check("TC4 缺命令 verdict=FAIL", "no_such_cmd", "FAIL", r["verdict"])
    check("TC4 缺命令 exit_code=None", "no_such_cmd", None, r["exit_code"])

    # ===== TC5 失败探针 → FAIL + 证据 =====
    _, sd = _mk_statedir()
    r = verify.verify(sd, "F3", "false", name="tc5", use_shell=True)
    check("TC5 fail探针 verdict", "false", "FAIL", r["verdict"])
    check("TC5 fail探针 exit_code!=0", "false", True, r["exit_code"] != 0)
    check("TC5 fail探针证据存在", "false", True, os.path.exists(r["evidence_path"]))
    code, _ = run(sd, "F3", "--probe", "false", "--name", "tc5cli")
    check("TC5 verify.py 退出码=1(FAIL)", "CLI false", 1, code)

    # ===== TC6 超时 → FAIL + timed_out + 真杀进程组 =====
    # 探针先写 marker 文件再 sleep；超时被进程组杀后，断言它没"睡醒"再改 marker。
    _, sd = _mk_statedir()
    marker = os.path.join(sd, "tc6_marker.txt")
    woke = os.path.join(sd, "tc6_woke.txt")
    # bash: 写 started → sleep 5 → 若睡醒则写 woke（被杀则永远到不了 woke）。
    probe6 = f"echo started > {marker}; sleep 5; echo woke > {woke}"
    r = verify.verify(sd, "F3", probe6, name="tc6", timeout=1, use_shell=True)
    check("TC6 超时 verdict=FAIL", "sleep5 timeout1", "FAIL", r["verdict"])
    check("TC6 超时 timed_out=True", "sleep5 timeout1", True, r["timed_out"])
    check("TC6 超时 证据 TIMED_OUT=true", "sleep5",
          True, "TIMED_OUT=true" in open(r["evidence_path"], encoding="utf-8").read())
    check("TC6 探针确实启动过(marker存在)", "sleep5", True, os.path.exists(marker))
    code, _ = run(sd, "F3", "--probe", "true", "--name", "tc6ok")  # 顺带确认 timeout 路径后 CLI 仍能跑
    # 进程组真被杀：等够 sleep 该睡醒的时间，woke 仍不该出现（子进程已随进程组死）。
    time.sleep(5)
    check("TC6 进程组被杀(woke 不出现=未睡醒续跑)", "killpg", False, os.path.exists(woke))

    # ===== TC7 裁定纯函数 _decide(exit_code, timed_out) =====
    check("TC7 _decide(0,False)", "(0,False)", "PASS", verify._decide(0, False))
    check("TC7 _decide(1,False)", "(1,False)", "FAIL", verify._decide(1, False))
    check("TC7 _decide(0,True)边角", "(0,True)", "FAIL", verify._decide(0, True))
    check("TC7 _decide(None,False)", "(None,False)", "FAIL", verify._decide(None, False))

    # ===== TC9 证据指纹存在且匹配实算 sha256 =====
    _, sd = _mk_statedir()
    r = verify.verify(sd, "F3", "echo fingerprint; exit 0", name="tc9", use_shell=True)
    actual_sha = hashlib.sha256(open(r["evidence_path"], "rb").read()).hexdigest()
    check("TC9 evidence_sha256 存在", "echo;exit0", True, bool(r.get("sha256")))
    check("TC9 evidence_sha256 == 实算", "echo;exit0", actual_sha, r["sha256"])

    # ===== TC10 大输出截断不影响裁定（退 0 仍 PASS）=====
    _, sd = _mk_statedir()
    # python 狂打 100KB 但 exit 0；max_bytes 设很小（200）。
    flood = "python3 -c \"import sys; sys.stdout.write('X'*100000)\"; exit 0"
    r = verify.verify(sd, "F3", flood, name="tc10", use_shell=True, max_bytes=200)
    check("TC10 大输出截断后 verdict=PASS", "flood;exit0", "PASS", r["verdict"])
    check("TC10 证据含 TRUNCATED 标记", "flood max_bytes=200",
          True, "TRUNCATED" in open(r["evidence_path"], encoding="utf-8").read())

    # ===== TC11 verify.jsonl 审计行追加（连跑两次 → 2 行）=====
    _, sd = _mk_statedir()
    verify.verify(sd, "F3", "true", name="tc11a", use_shell=True)
    verify.verify(sd, "F3", "false", name="tc11b", use_shell=True)
    jsonl = os.path.join(sd, "evidence", "F3", "verify.jsonl")
    lines = [l for l in open(jsonl, encoding="utf-8").read().splitlines() if l.strip()]
    check("TC11 verify.jsonl 行数", "连跑2次", 2, len(lines))
    rec0 = json.loads(lines[0])
    check("TC11 审计行含 verdict", "audit", True, "verdict" in rec0)
    check("TC11 审计行含 duration_ms", "audit", True, "duration_ms" in rec0)
    check("TC11 审计行含 exit_code", "audit", True, "exit_code" in rec0)

    # ===== TC12 用法错退出码 2（缺 --probe / state_dir 不存在）=====
    code, _ = run(os.path.join(tempfile.gettempdir(), "lhb-nope-xyz-404"), "F3", "--probe", "true")
    check("TC12 state_dir 不存在→退出码2", "bad state_dir", 2, code)
    _, sd = _mk_statedir()
    code, _ = run(sd, "F3", "--probe", "   ")  # 空白 probe
    check("TC12 空白 probe→退出码2", "blank probe", 2, code)

    # ===== TC13 duration_ms 被记录且 ≥0 + 证据有 DURATION_MS 行 =====
    _, sd = _mk_statedir()
    r = verify.verify(sd, "F3", "true", name="tc13", use_shell=True)
    check("TC13 duration_ms 是 int", "true", True, isinstance(r["duration_ms"], int))
    check("TC13 duration_ms >= 0", "true", True, r["duration_ms"] >= 0)
    check("TC13 证据有 DURATION_MS= 行", "true",
          True, "\nDURATION_MS=" in open(r["evidence_path"], encoding="utf-8").read())

    # ===== TC14 CLI stdout 是单行可解析 JSON 含 verdict =====
    _, sd = _mk_statedir()
    code, out = run(sd, "F3", "--probe", "true", "--name", "tc14")
    first_line = out.splitlines()[0] if out else ""
    parsed = None
    try:
        parsed = json.loads(first_line)
    except Exception:
        parsed = None
    check("TC14 CLI stdout 首行可 json.loads", "CLI true", True, parsed is not None)
    check("TC14 解析出含 verdict 键", "CLI true", True, bool(parsed) and "verdict" in parsed)

    # ===== TC15 evidence_path 是绝对路径（realpath）=====
    _, sd = _mk_statedir()
    r = verify.verify(sd, "F3", "true", name="tc15", use_shell=True)
    check("TC15 evidence_path 绝对路径", "true", True, os.path.isabs(r["evidence_path"]))

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
