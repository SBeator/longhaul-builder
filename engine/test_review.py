#!/usr/bin/env python3
"""review.py 的 dogfood 自测：可配置判官适配器的解析/降级/分层/反作弊，产出四列证据表。

立场（DESIGN §2.5 / spec AC4 / P0-2）：
- judge 一律用**本地 stub 脚本**（写进 tmp、零网络零 LLM）；review.py 不关心命令内部是 LLM 还是 echo。
- C1（门1阻塞）：parse_verdict 必须识别 rubric 真实输出的**纯文本 `VERDICT: X` 块**（impl_review.md /
  plan_review.md 让判官输出的就是它，不是 JSON）；JSON `{"verdict":...}` 仅作 fallback。TC2b 喂"恰好
  rubric 块格式"断言能解析出 verdict——这是门1 catch 的核心 bug（JSON-only stub 测不出）。
- C2：未配置判官（空默认）→ 降级 exit 3 / ok=False（基建故障，**不烧 attempt**），绝不 exit 1。
- exit 分层：0=PASS-ish / 1=判官说FAIL/REVISE(产品) / 2=usage / 3=判官坏/超时/畸形/未配置(基建)。

运行：python3 engine/test_review.py  → 退出码 0 全过 / 1 有不一致。
"""
import io
import json
import os
import stat
import sys
import tempfile
import time
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import review  # noqa: E402  (RED 阶段此 import 失败 = 预期的红)

rows = []  # (用例, 输入, 期望, 实际, 一致?)


def run(*argv):
    """跑一条 review CLI，返回 (exit_code, stdout)。"""
    buf = io.StringIO()
    code = 1
    with redirect_stdout(buf):
        try:
            code = review.main(list(argv))
        except SystemExit as e:  # argparse 用法错
            code = int(e.code) if e.code else 0
    return code, buf.getvalue().strip()


def check(case, inp, expected, actual):
    ok = (expected == actual)
    rows.append((case, inp, str(expected), str(actual), "✅" if ok else "❌"))
    return ok


def _mk_statedir(with_milestone=True):
    """造一个最小 state_dir：含 milestones.json（review 要按 id 取并渲染 rubric）+ spec.md。"""
    root = tempfile.mkdtemp(prefix="lhb-review-test-")
    sd = os.path.join(root, ".longhaul")
    os.makedirs(os.path.join(sd, "evidence", "F1"), exist_ok=True)
    with open(os.path.join(sd, "spec.md"), "w", encoding="utf-8") as f:
        f.write("# spec\nAC1 ...\n")
    if with_milestone:
        ms = {"milestones": [{
            "id": "F1",
            "goal": "渲染器把三套 prompt 抽成模板文件",
            "acceptance": {"type": "tdd", "probe": "python3 engine/test_prompts.py"},
            "status": "IN_PROGRESS", "phase": "impl_review",
            "attempt_count": 1, "max_attempts": 3,
        }]}
        with open(os.path.join(sd, "milestones.json"), "w", encoding="utf-8") as f:
            json.dump(ms, f)
    return root, sd


def _seed_verify_evidence(sd, mid="F1"):
    """造一点 verify.py 风格的证据（verify.jsonl 用键 `sha256`，对齐 F3 carry-forward）。"""
    evd = os.path.join(sd, "evidence", mid)
    os.makedirs(evd, exist_ok=True)
    with open(os.path.join(evd, "green.txt"), "w", encoding="utf-8") as f:
        f.write("ALL PASS\nEXIT_CODE=0\n")
    with open(os.path.join(evd, "verify.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"name": "green", "verdict": "PASS", "exit_code": 0,
                            "sha256": "deadbeef", "duration_ms": 12}) + "\n")
    return evd


_STUB_DIR = tempfile.mkdtemp(prefix="lhb-review-stubs-")
_stub_seq = [0]


def _mk_stub(body):
    """把一段 python 写成可执行 stub 脚本，返回路径。judge_cmd 模板 = 'python3 {stub} {prompt_file}'。

    body 是脚本主体（可用 sys.argv[1] 拿 prompt_file 路径）。占位真实走一遍替换路径。
    """
    _stub_seq[0] += 1
    path = os.path.join(_STUB_DIR, f"stub_{_stub_seq[0]}.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write("#!/usr/bin/env python3\nimport sys\n" + body + "\n")
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    return path


def _cmd(stub):
    return "python3 " + stub + " {prompt_file}"


def main():
    # ===== TC1 mock judge 出纯 JSON → 解析出 verdict（agent 无关基本面）=====
    _, sd = _mk_statedir(); _seed_verify_evidence(sd)
    stub = _mk_stub("print('{\"verdict\":\"PASS\",\"findings\":[]}')")
    r = review.review(sd, "F1", kind="impl_review", judge_cmd=_cmd(stub))
    check("TC1 纯JSON verdict", "judge打印JSON", "PASS", r["verdict"])
    check("TC1 纯JSON ok", "judge打印JSON", True, r["ok"])

    # ===== TC2 散文里包一个 JSON（真 agent 常先讲再给 JSON）=====
    _, sd = _mk_statedir(); _seed_verify_evidence(sd)
    stub = _mk_stub("print('我分析了三层...\\n最后结论:\\n{\"verdict\":\"FAIL\",\"reason\":\"层③代码烂\"}')")
    r = review.review(sd, "F1", kind="impl_review", judge_cmd=_cmd(stub))
    check("TC2 散文包JSON 抽verdict", "prose+JSON", "FAIL", r["verdict"])
    check("TC2 散文包JSON ok", "prose+JSON", True, r["ok"])

    # ===== TC2b C1 核心：判官按 rubric 真实输出"纯文本 VERDICT 块"（零 JSON）=====
    # impl_review.md 让判官输出的就是这个块。JSON-only 解析器在这会永远降级 ERROR（门1 catch 的 bug）。
    _, sd = _mk_statedir(); _seed_verify_evidence(sd)
    rubric_block = (
        "层①证据：green.txt 真实存在 EXIT_CODE=0。\\n"
        "层②复跑：我跑了 pytest，exit 0。\\n"
        "层③代码质量：结构清晰。\\n"
        "VERDICT: PASS_WITH_NITS\\n"
        "RERUN: python3 engine/test_prompts.py → exit 0\\n"
        "NITS: 命名可更一致\\n"
        "REASON: 三层全过，仅一个非阻塞 nit\\n"
    )
    stub = _mk_stub("print('" + rubric_block + "')")
    r = review.review(sd, "F1", kind="impl_review", judge_cmd=_cmd(stub))
    check("TC2b C1 rubric块 verdict(无JSON也认)", "VERDICT:块", "PASS_WITH_NITS", r["verdict"])
    check("TC2b C1 rubric块 ok", "VERDICT:块", True, r["ok"])

    # ===== TC3 markdown ```json fenced``` JSON =====
    _, sd = _mk_statedir(); _seed_verify_evidence(sd)
    stub = _mk_stub("print('```json\\n{\"verdict\":\"PASS\"}\\n```')")
    r = review.review(sd, "F1", kind="impl_review", judge_cmd=_cmd(stub))
    check("TC3 fenced JSON ok", "```json```", True, r["ok"])
    check("TC3 fenced JSON verdict", "```json```", "PASS", r["verdict"])

    # ===== TC4 多个 JSON → 取最后那个（结论）=====
    _, sd = _mk_statedir(); _seed_verify_evidence(sd)
    stub = _mk_stub("print('{\"verdict\":\"FAIL\"}\\n改完后:\\n{\"verdict\":\"PASS\"}')")
    r = review.review(sd, "F1", kind="impl_review", judge_cmd=_cmd(stub))
    check("TC4 多JSON取最后(结论)", "两个JSON", "PASS", r["verdict"])

    # ===== TC5 纯散文无裁定 → 降级 ERROR ok=False 不崩 =====
    _, sd = _mk_statedir(); _seed_verify_evidence(sd)
    stub = _mk_stub("print('我觉得还行吧没给结论')")
    crashed = False
    try:
        r = review.review(sd, "F1", kind="impl_review", judge_cmd=_cmd(stub))
    except Exception:
        crashed = True; r = {"verdict": "?", "ok": True}
    check("TC5 纯散文不崩", "no verdict", False, crashed)
    check("TC5 纯散文 verdict=ERROR", "no verdict", "ERROR", r["verdict"])
    check("TC5 纯散文 ok=False", "no verdict", False, r["ok"])

    # ===== TC6 空输出 → 降级 ERROR =====
    _, sd = _mk_statedir(); _seed_verify_evidence(sd)
    stub = _mk_stub("pass")
    r = review.review(sd, "F1", kind="impl_review", judge_cmd=_cmd(stub))
    check("TC6 空输出 verdict=ERROR", "empty", "ERROR", r["verdict"])
    check("TC6 空输出 ok=False", "empty", False, r["ok"])

    # ===== TC7 非法 JSON（截断）+ 无 VERDICT 块 → 降级 ERROR 不崩 =====
    _, sd = _mk_statedir(); _seed_verify_evidence(sd)
    stub = _mk_stub("print('{\"verdict\":\"PA')")  # 截断
    crashed = False
    try:
        r = review.review(sd, "F1", kind="impl_review", judge_cmd=_cmd(stub))
    except Exception:
        crashed = True; r = {"verdict": "?", "ok": True}
    check("TC7 截断JSON不崩", "broken json", False, crashed)
    check("TC7 截断JSON verdict=ERROR", "broken json", "ERROR", r["verdict"])

    # ===== TC8 未知 verdict 词（不在白名单）→ 降级 ERROR（不被当 PASS）=====
    _, sd = _mk_statedir(); _seed_verify_evidence(sd)
    stub = _mk_stub("print('{\"verdict\":\"MAYBE\"}')")
    r = review.review(sd, "F1", kind="impl_review", judge_cmd=_cmd(stub))
    check("TC8 未知词 verdict=ERROR", "MAYBE", "ERROR", r["verdict"])
    check("TC8 未知词 ok=False(不当PASS)", "MAYBE", False, r["ok"])

    # ===== TC9 judge 非零退出但有合法裁定 → 仍解析（exit_code≠裁定语义）=====
    _, sd = _mk_statedir(); _seed_verify_evidence(sd)
    stub = _mk_stub("print('VERDICT: PASS'); sys.exit(2)")
    r = review.review(sd, "F1", kind="impl_review", judge_cmd=_cmd(stub))
    check("TC9 judge退码2但裁定PASS", "VERDICT+exit2", "PASS", r["verdict"])
    check("TC9 judge退码被记录=2", "VERDICT+exit2", 2, r["exit_code"])
    check("TC9 退码不覆盖白名单裁定(ok)", "VERDICT+exit2", True, r["ok"])

    # ===== TC10 judge hang → 超时 → ERROR + 进程组被杀（同 verify TC6 法证）=====
    _, sd = _mk_statedir(); _seed_verify_evidence(sd)
    woke = os.path.join(sd, "tc10_woke.txt")
    stub = _mk_stub("import time\ntime.sleep(5)\nopen('" + woke + "','w').write('woke')")
    r = review.review(sd, "F1", kind="impl_review", judge_cmd=_cmd(stub), timeout=1)
    check("TC10 超时 timed_out", "sleep5 t=1", True, r["timed_out"])
    check("TC10 超时 verdict=ERROR", "sleep5 t=1", "ERROR", r["verdict"])
    check("TC10 超时 ok=False", "sleep5 t=1", False, r["ok"])
    time.sleep(5)  # 等够 sleep 该睡醒的时间
    check("TC10 进程组被杀(woke 不出现)", "killpg", False, os.path.exists(woke))

    # ===== TC11 judge 命令不存在 → 降级 ERROR 不抛栈 =====
    _, sd = _mk_statedir(); _seed_verify_evidence(sd)
    crashed = False
    try:
        r = review.review(sd, "F1", kind="impl_review",
                          judge_cmd="no_such_judge_xyz_42 {prompt_file}")
    except Exception:
        crashed = True; r = {"verdict": "?", "ok": True}
    check("TC11 缺命令不崩", "no_such_judge", False, crashed)
    check("TC11 缺命令 verdict=ERROR", "no_such_judge", "ERROR", r["verdict"])

    # ===== TC12 P0-2：env LONGHAUL_JUDGE_CMD 注入可换（不传 flag）=====
    _, sd = _mk_statedir(); _seed_verify_evidence(sd)
    stubA = _mk_stub("print('VERDICT: PASS')")
    old = os.environ.get("LONGHAUL_JUDGE_CMD")
    os.environ["LONGHAUL_JUDGE_CMD"] = _cmd(stubA)
    try:
        r = review.review(sd, "F1", kind="impl_review")  # judge_cmd=None → 走 env
    finally:
        if old is None:
            os.environ.pop("LONGHAUL_JUDGE_CMD", None)
        else:
            os.environ["LONGHAUL_JUDGE_CMD"] = old
    check("TC12 env 注入命中", "env=stubA", "PASS", r["verdict"])

    # ===== TC13 P0-2：flag 优先于 env =====
    _, sd = _mk_statedir(); _seed_verify_evidence(sd)
    stubFail = _mk_stub("print('VERDICT: FAIL')")
    stubPass = _mk_stub("print('VERDICT: PASS')")
    old = os.environ.get("LONGHAUL_JUDGE_CMD")
    os.environ["LONGHAUL_JUDGE_CMD"] = _cmd(stubFail)
    try:
        r = review.review(sd, "F1", kind="impl_review", judge_cmd=_cmd(stubPass))
    finally:
        if old is None:
            os.environ.pop("LONGHAUL_JUDGE_CMD", None)
        else:
            os.environ["LONGHAUL_JUDGE_CMD"] = old
    check("TC13 flag 优先于 env", "flag=PASS,env=FAIL", "PASS", r["verdict"])

    # ===== TC14 plan_review 词域隔离 =====
    _, sd = _mk_statedir(); _seed_verify_evidence(sd)
    stub = _mk_stub("print('VERDICT: APPROVE')")
    r = review.review(sd, "F1", kind="plan_review", judge_cmd=_cmd(stub))
    check("TC14a plan域 APPROVE ok", "kind=plan APPROVE", True, r["ok"])
    check("TC14a plan域 verdict", "kind=plan APPROVE", "APPROVE", r["verdict"])
    # 越域：plan 阶段给 impl 域的 PASS → 当未知词降级
    stub2 = _mk_stub("print('VERDICT: PASS')")
    r2 = review.review(sd, "F1", kind="plan_review", judge_cmd=_cmd(stub2))
    check("TC14b 越域词(plan收PASS)降级", "kind=plan,PASS越域", "ERROR", r2["verdict"])

    # ===== TC15 verdict dict 永远良构（遍历多种结局都含全部稳定键）=====
    _, sd = _mk_statedir(); _seed_verify_evidence(sd)
    KEYS = {"verdict", "ok", "kind", "parsed", "raw", "judge_cmd", "exit_code",
            "timed_out", "duration_ms", "milestone", "ts"}
    results = [
        review.review(sd, "F1", "impl_review", judge_cmd=_cmd(_mk_stub("print('VERDICT: PASS')"))),
        review.review(sd, "F1", "impl_review", judge_cmd=_cmd(_mk_stub("print('garbage')"))),
        review.review(sd, "F1", "impl_review", judge_cmd=_cmd(_mk_stub("print('{\"verdict\":\"PA')"))),
    ]
    wellformed = all(
        KEYS.issubset(set(x.keys())) and isinstance(x["verdict"], str) and isinstance(x["ok"], bool)
        for x in results)
    check("TC15 dict 永远良构(键齐+类型对)", "遍历PASS/garbage/broken", True, wellformed)

    # ===== TC16 CLI stdout 首行可 json.loads 含 verdict =====
    _, sd = _mk_statedir(); _seed_verify_evidence(sd)
    stub = _mk_stub("print('VERDICT: PASS')")
    code, out = run(sd, "F1", "--kind", "impl_review", "--judge-cmd", _cmd(stub))
    first = out.splitlines()[0] if out else ""
    parsed = None
    try:
        parsed = json.loads(first)
    except Exception:
        parsed = None
    check("TC16 CLI首行可json.loads", "CLI PASS", True, parsed is not None)
    check("TC16 含verdict键", "CLI PASS", True, bool(parsed) and "verdict" in parsed)

    # ===== TC17 CLI 退出码分层 PASS→0 / FAIL→1 / ERROR→3 =====
    _, sd = _mk_statedir(); _seed_verify_evidence(sd)
    code_pass, _ = run(sd, "F1", "--kind", "impl_review",
                       "--judge-cmd", _cmd(_mk_stub("print('VERDICT: PASS')")))
    check("TC17 PASS→exit0", "CLI PASS", 0, code_pass)
    code_fail, _ = run(sd, "F1", "--kind", "impl_review",
                       "--judge-cmd", _cmd(_mk_stub("print('VERDICT: FAIL')")))
    check("TC17 FAIL→exit1", "CLI FAIL", 1, code_fail)
    code_err, _ = run(sd, "F1", "--kind", "impl_review",
                      "--judge-cmd", _cmd(_mk_stub("print('garbage no verdict')")))
    check("TC17 ERROR→exit3", "CLI garbage", 3, code_err)
    # 用法错（milestone 不存在）→ exit2
    code_use, _ = run(sd, "NOPE", "--kind", "impl_review",
                      "--judge-cmd", _cmd(_mk_stub("print('VERDICT: PASS')")))
    check("TC17 用法错(坏milestone)→exit2", "CLI bad mid", 2, code_use)
    # plan 域 REVISE → exit1（产品打回）
    code_rev, _ = run(sd, "F1", "--kind", "plan_review",
                      "--judge-cmd", _cmd(_mk_stub("print('VERDICT: REVISE')")))
    check("TC17 REVISE→exit1", "CLI REVISE", 1, code_rev)

    # ===== TC18 C2：未配置判官（空默认）→ 降级 exit3 + ok=False（基建,不烧attempt）=====
    _, sd = _mk_statedir(); _seed_verify_evidence(sd)
    old = os.environ.get("LONGHAUL_JUDGE_CMD")
    os.environ.pop("LONGHAUL_JUDGE_CMD", None)
    try:
        r = review.review(sd, "F1", kind="impl_review", judge_cmd=None)  # 无 flag、无 env、空默认
        check("TC18 未配置 verdict=ERROR", "no judge", "ERROR", r["verdict"])
        check("TC18 未配置 ok=False", "no judge", False, r["ok"])
        check("TC18 未配置 reason含提示", "no judge",
              True, "no judge command configured" in (r.get("reason") or ""))
        # C2 关键：CLI 未配置 → exit 3（基建，**不是 1**，绝不烧 attempt）
        code_nc, _ = run(sd, "F1", "--kind", "impl_review")
        check("TC18 C2 未配置CLI→exit3(非1)", "CLI no judge", 3, code_nc)
    finally:
        if old is not None:
            os.environ["LONGHAUL_JUDGE_CMD"] = old

    # ===== TC19 events.jsonl 记 review 事件（含 verdict/judge_cmd脱敏/duration）=====
    _, sd = _mk_statedir(); _seed_verify_evidence(sd)
    stub = _mk_stub("print('VERDICT: PASS')")
    review.review(sd, "F1", kind="impl_review", judge_cmd=_cmd(stub))
    ev_path = os.path.join(sd, "events.jsonl")
    last = [l for l in open(ev_path, encoding="utf-8").read().splitlines() if l.strip()][-1]
    ev = json.loads(last)
    check("TC19 events末行 ev=review", "review事件", "review", ev.get("ev"))
    check("TC19 事件含 verdict", "review事件", True, "verdict" in ev)
    check("TC19 事件含 duration_ms", "review事件", True, "duration_ms" in ev)

    # ===== TC20 review json 落盘 + 含 raw/parsed =====
    _, sd = _mk_statedir(); _seed_verify_evidence(sd)
    stub = _mk_stub("print('VERDICT: PASS')")
    r = review.review(sd, "F1", kind="impl_review", judge_cmd=_cmd(stub))
    rj = os.path.join(sd, "evidence", "F1", "review-impl_review.json")
    check("TC20 review json 落盘", "落盘", True, os.path.exists(rj))
    disk = json.loads(open(rj, encoding="utf-8").read())
    check("TC20 落盘含 raw", "落盘", True, "raw" in disk)
    check("TC20 落盘含 parsed", "落盘", True, "parsed" in disk)

    # ===== TC21 rubric 真渲染（占位被填充，非 UNSET）=====
    # stub 把收到的 prompt_file 内容回显进 stdout，断言含已填的 milestone_id/goal（证明走了 prompts.render）。
    _, sd = _mk_statedir(); _seed_verify_evidence(sd)
    echo_stub = _mk_stub(
        "txt=open(sys.argv[1],encoding='utf-8').read()\n"
        "sys.stderr.write(txt)\n"  # 把 prompt 回显到 stderr 给断言用
        "print('VERDICT: PASS')")
    r = review.review(sd, "F1", kind="impl_review", judge_cmd=_cmd(echo_stub))
    # review() 把 prompt 写进临时文件；我们直接断言渲染出的 rubric 含填充值（不靠 stub 回显文件已删）。
    # 改为断言 review 暴露的 prompt 渲染入口或落盘证据里能验证。这里用 _build_prompt_file 直检。
    body = review._render_rubric_and_evidence(sd, "F1", "impl_review", ctx=None)
    check("TC21 rubric 含填充 milestone_id", "渲染", True, "F1" in body)
    check("TC21 rubric 含填充 goal", "渲染", True, "渲染器把三套 prompt" in body)
    check("TC21 rubric 无 UNSET 残留(关键占位)", "渲染", False, "milestone_id:UNSET" in body)
    check("TC21 prompt 含证据索引(sha256)", "渲染", True, "sha256" in body)

    # ===== TC22 judge_cmd 含密钥 → 落盘/事件里被脱敏 =====
    _, sd = _mk_statedir(); _seed_verify_evidence(sd)
    stub = _mk_stub("print('VERDICT: PASS')")
    secret_cmd = _cmd(stub) + " --api-key sk-supersecret123456"
    r = review.review(sd, "F1", kind="impl_review", judge_cmd=secret_cmd)
    check("TC22 返回 judge_cmd 脱敏", "sk-key", False, "sk-supersecret123456" in r["judge_cmd"])
    rj = os.path.join(sd, "evidence", "F1", "review-impl_review.json")
    disk_raw = open(rj, encoding="utf-8").read()
    check("TC22 落盘无明文key", "sk-key", False, "sk-supersecret123456" in disk_raw)
    ev_raw = open(os.path.join(sd, "events.jsonl"), encoding="utf-8").read()
    check("TC22 事件无明文key", "sk-key", False, "sk-supersecret123456" in ev_raw)

    # ===== TC22b raw 字段也脱敏（判官可能 echo 含 key 的调用进 stdout）=====
    _, sd = _mk_statedir(); _seed_verify_evidence(sd)
    leaky = _mk_stub("print('调用了 --api-key sk-leakedFromStdout99'); print('VERDICT: PASS')")
    r = review.review(sd, "F1", kind="impl_review", judge_cmd=_cmd(leaky))
    check("TC22b raw 字段脱敏", "raw含key", False, "sk-leakedFromStdout99" in r["raw"])

    # ===== TC23 未知占位模板（{bogus}）→ format KeyError 捕获 → 降级 ERROR 不崩 =====
    _, sd = _mk_statedir(); _seed_verify_evidence(sd)
    stub = _mk_stub("print('VERDICT: PASS')")
    crashed = False
    try:
        r = review.review(sd, "F1", kind="impl_review",
                          judge_cmd=_cmd(stub) + " {bogus_placeholder}")
    except Exception:
        crashed = True; r = {"verdict": "?", "ok": True}
    check("TC23 未知占位不崩", "{bogus}", False, crashed)
    check("TC23 未知占位 verdict=ERROR", "{bogus}", "ERROR", r["verdict"])

    # ===== TC23b 字面 { （str.format 报错）→ 优雅降级不崩（suggestion）=====
    _, sd = _mk_statedir(); _seed_verify_evidence(sd)
    crashed = False
    try:
        # 模板里有个孤立 '{' 会让 str.format 抛 ValueError
        r = review.review(sd, "F1", kind="impl_review",
                          judge_cmd="python3 " + _mk_stub("print('VERDICT: PASS')") +
                                    " {prompt_file} --note 'has a { brace'")
    except Exception:
        crashed = True; r = {"verdict": "?", "ok": True}
    check("TC23b 字面{ 不崩", "literal {", False, crashed)
    check("TC23b 字面{ verdict=ERROR", "literal {", "ERROR", r["verdict"])

    # ===== TC24 parse_verdict 直测：VERDICT 块 vs JSON fallback vs 白名单 =====
    check("TC24 parse VERDICT块(impl)", "VERDICT: FAIL",
          "FAIL", review.parse_verdict("blah\nVERDICT: FAIL\nREASON: x", "impl_review")["verdict"])
    check("TC24 parse JSON fallback", '{"verdict":"PASS"}',
          "PASS", review.parse_verdict('noise {"verdict":"PASS"} tail', "impl_review")["verdict"])
    check("TC24 parse 越域当 None", "plan收PASS",
          None, review.parse_verdict("VERDICT: PASS", "plan_review"))
    check("TC24 parse 无裁定 None", "no verdict",
          None, review.parse_verdict("just prose", "impl_review"))

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
