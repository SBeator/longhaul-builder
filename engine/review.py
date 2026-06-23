#!/usr/bin/env python3
"""longhaul-builder — review.py：可配置 AI 判官适配器（门1/门2 的 AI 层 · F4 / AC4）。

设计立场（DESIGN.md §2.4②③ / §2.5 两把尺子 / spec AC4 / P0-2）：
- review.py 是 **AI 判官层的薄适配器**，做四件事：① 用 prompts.py 渲染本 milestone 的 rubric
  （impl_review / plan_review）；② 收齐这一步 verify.py 落的**确定性证据**（evidence/<id>/ +
  verify.jsonl，按键 `sha256`）；③ 调一个**可配置、agent 无关的 shell 命令**(judge) 把 rubric+证据
  喂给它；④ **稳健解析**它的输出成一个**永远良构**的裁定 dict，并记审计。
- 反作弊（§2.5）：judge 看的是 verify.py 产出的**真实证据字节**，不是 driver 自述。review() 入参里
  **没有任何「声称的裁定」字段**（结构性反作弊，对齐 verify.py）。
- 不越界：**不**自己重跑探针定真伪（那是 verify.py 的活）；**不**直接改 milestones/cursor 状态
  （state.py 是唯一写状者）；只 append_event 记审计 + 落 review json。
- agent 无关（P0-2）：judge 命令由 flag/env/文档化默认注入；本文件**不出现任何具体 agent 名**，
  默认值是空哨兵（未配置→优雅降级，不乱猜某 agent）。

判官输出格式（C1，门1 catch）：
  渲染的 rubric（impl_review.md / plan_review.md）让判官输出的是**纯文本 `VERDICT: X` 块**
  （+REASON/NITS/CONDITIONS/RERUN 行），**不是 JSON**。parse_verdict 因此**首选**正则抓
  `VERDICT:\\s*<WORD>` 行（按 kind 白名单校验），JSON `{"verdict":...}` 仅作 fallback（兼容
  少数会吐 JSON 的判官）。忠实判官按 rubric 输出零 JSON 也必须能解析出 verdict。

退出码契约（与 state.py 0/2/3、verify.py 0/1/2 协调）：
  0 = 拿到可解析裁定且 ∈ PASS-ish（PASS/PASS_WITH_NITS/APPROVE/APPROVE_WITH_CONDITIONS）
  1 = 拿到可解析裁定但 = 打回（FAIL/REVISE）——产品信号，loop 据此烧 attempt
  2 = 用法错（state_dir 不存在 / kind 非法 / milestone 不在 milestones.json），没调 judge
  3 = **降级**（judge 崩/超时/输出畸形/越域/**未配置**）→ verdict=ERROR, ok=False——基建信号，
      loop 据此**不烧 attempt**、走基建重试（C2：未配置必走这条，绝不 exit 1）

agent/基建无关：纯标准库。复用 prompts.render（渲 rubric）、verify._run_cmd（跑命令+超时杀进程组，
不重造）、state.append_event（写 events.jsonl）。
"""

import argparse
import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import prompts  # noqa: E402  渲 rubric（单一事实源，不重造）
import state    # noqa: E402  append_event（events.jsonl 不重造）
import verify   # noqa: E402  复用 _run_cmd（跑命令+超时杀进程组）/ _atomic_write_text

DEFAULT_TIMEOUT = 600
DEFAULT_MAX_BYTES = 1_000_000

#: judge 命令未配置时的空哨兵默认——**不硬绑任何 agent**（P0-2）。
#: 用户照抄到 $LONGHAUL_JUDGE_CMD 的示例（agent 无关）见 docstring / README：
#:   claude -p "$(cat {prompt_file})"   或   codex exec --file {prompt_file}
DEFAULT_JUDGE_CMD = ""

#: 按 kind 的合法 verdict 白名单（域隔离：plan 阶段给 impl 域的词当未知 → 降级，反之亦然）。
VALID_VERDICTS = {
    "impl_review": ("PASS", "PASS_WITH_NITS", "FAIL"),
    "plan_review": ("APPROVE", "APPROVE_WITH_CONDITIONS", "REVISE"),
}
#: 各域的"放行类"裁定（exit 0）；其余在白名单内的词 = 产品打回（exit 1）。
PASS_ISH = {
    "impl_review": ("PASS", "PASS_WITH_NITS"),
    "plan_review": ("APPROVE", "APPROVE_WITH_CONDITIONS"),
}

#: VERDICT: 行（rubric 真实输出格式，C1 首选）。捕获冒号后整行余下内容（含可能的 ` | ` 枚举），
#: 由 parse_verdict 再判定是不是"真实裁定"还是"格式示例行"。大小写不敏感、全大写归一。
_VERDICT_LINE_RE = re.compile(r"^\s*VERDICT\s*:\s*([^\n]+)", re.MULTILINE)

#: 脱敏：常见密钥/token 形态打码（judge_cmd 与 raw 落盘/记事件前都过一遍）。
_SECRET_RES = [
    re.compile(r"(sk-)[A-Za-z0-9_\-]{6,}"),                       # OpenAI/Anthropic 风格
    re.compile(r"(--api[-_]?key[=\s]+)\S+", re.IGNORECASE),        # --api-key X / --api_key=X
    re.compile(r"(token[=:\s]+)\S+", re.IGNORECASE),               # token=... / token: ...
    re.compile(r"(Authorization[=:\s]+)\S+", re.IGNORECASE),       # Authorization: Bearer ...
]


def _now():
    return state._now()


def resolve_judge_cmd(explicit=None, env=None) -> str:
    """judge 命令解析：CLI/API 显式 > 环境 LONGHAUL_JUDGE_CMD > 空哨兵默认（P0-2）。

    三层取最先非空者；全空 → 返回 ""（review() 据此降级，明确报"未配置"，不乱猜 agent）。
    """
    env = os.environ if env is None else env
    return explicit or env.get("LONGHAUL_JUDGE_CMD") or DEFAULT_JUDGE_CMD


def _sanitize(text) -> str:
    """脱敏：把疑似密钥/token 打码（judge_cmd 与 raw 都过；证据可能被人看/导出，不留明文）。"""
    if not text:
        return text or ""
    s = str(text)
    for rx in _SECRET_RES:
        s = rx.sub(lambda m: m.group(1) + "***", s)
    return s


# ---- 解析裁定（C1：VERDICT 块首选 + JSON fallback；永远不抛）----------------

def _iter_json_objects(text):
    """括号配平扫描，从文本里切出所有顶层 {...} 候选子串（能处理嵌套/字符串内花括号）。

    不用贪婪正则（抓不准嵌套/转义）。逐字符扫，跳过字符串字面量内的 { }。
    """
    out = []
    i, n = 0, len(text)
    while i < n:
        if text[i] == "{":
            depth, j, in_str, esc, quote = 0, i, False, False, ""
            while j < n:
                c = text[j]
                if in_str:
                    if esc:
                        esc = False
                    elif c == "\\":
                        esc = True
                    elif c == quote:
                        in_str = False
                elif c in ('"', "'"):
                    in_str, quote = True, c
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        out.append(text[i:j + 1])
                        i = j
                        break
                j += 1
        i += 1
    return out


def parse_verdict(raw_text, kind):
    """从判官输出抽裁定。返回 {"verdict": <白名单内大写词>, "parsed": <dict|None>} 或 None。

    永远不抛。解析优先级（C1）：
      1) **VERDICT: <WORD> 行**（rubric 真实输出格式 —— impl_review.md/plan_review.md 让判官输出的就是它）。
         取**最后**一个 VERDICT 行（判官可能先示例格式、最后给结论）。词按 kind 白名单校验，越域/未知 → 跳过。
      2) **JSON {"verdict":...} fallback**（兼容会吐 JSON 的判官）：剥 ```fence```、括号配平抽候选、
         **从后往前** json.loads，第一个含白名单内 verdict 的采纳（parsed = 整个 JSON 对象，带 findings/nits…）。
      3) 都不成 → None（上层降级 ERROR）。
    白名单校验在两条路径都做：判官瞎写 `MAYBE`、或 plan 阶段回 `PASS`(越域) → 当未知，不放行。
    """
    if not raw_text:
        return None
    whitelist = VALID_VERDICTS.get(kind, ())

    # 1) VERDICT: 块（首选，rubric 真实格式）。
    #    🔒 防放水硬化（2026-06-23 review 修）：跳过含 '|' 的「格式示例行」—— rubric 模板自带一行
    #    `VERDICT: PASS | PASS_WITH_NITS | FAIL` 教判官输出格式，判官常把它复述在真实结论之后；
    #    旧实现"取最后一个 VERDICT 行"会把示例里的 PASS 当裁定 → 真实 FAIL 被翻成 PASS（放水）。
    #    再者：若收集到互相矛盾的多个不同裁定（判官自相矛盾）→ 含糊不放行，降级 ERROR（不擅自取乐观的）。
    verdicts = []
    for line in _VERDICT_LINE_RE.findall(raw_text):
        if "|" in line:                       # 枚举/格式示例行，非真实裁定 → 跳过
            continue
        m = re.match(r"\s*([A-Za-z_]+)", line)
        if not m:
            continue
        w = m.group(1).upper()
        if w in whitelist:
            verdicts.append(w)
    if verdicts:
        if len(set(verdicts)) > 1:
            return None                        # 判官自相矛盾（多个不同裁定）→ 降级，上层判 ERROR
        return {"verdict": verdicts[-1], "parsed": None}

    # 2) JSON fallback —— 剥代码围栏后括号配平抽候选，从后往前试。
    candidates = _iter_json_objects(raw_text)
    for cand in reversed(candidates):
        try:
            obj = json.loads(cand)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict) and "verdict" in obj:
            v = str(obj["verdict"]).strip().upper()
            if v in whitelist:
                return {"verdict": v, "parsed": obj}
    return None


# ---- 渲染 rubric + 拼证据索引 → prompt_file 文本 ----------------------------

def _read_verify_jsonl(ev_dir):
    """读 evidence/<id>/verify.jsonl 每行摘要（按键 `sha256`，对齐 F3 carry-forward——不是 evidence_sha256）。"""
    path = os.path.join(ev_dir, "verify.jsonl")
    lines = []
    if os.path.exists(path):
        for ln in open(path, encoding="utf-8").read().splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                rec = json.loads(ln)
            except ValueError:
                continue
            lines.append("  - name=%s verdict=%s exit_code=%s sha256=%s" % (
                rec.get("name"), rec.get("verdict"), rec.get("exit_code"), rec.get("sha256")))
    return lines


def _render_rubric_and_evidence(state_dir, milestone_id, kind, ctx=None) -> str:
    """渲染 rubric（prompts.render，单一事实源）+ 追加一段 EVIDENCE INDEX。返回 prompt_file 全文。

    ctx 键名对齐 impl_review.md/plan_review.md 的占位（carry-forward F1 警示：拼错会静默 no-op，
    TC21 断言渲染结果含已填充的 milestone_id/goal、无 UNSET 残留来兜住这条）。
    """
    milestone = prompts._load_milestone(state_dir, milestone_id)
    base_ctx = {
        "project_path": os.path.dirname(os.path.abspath(state_dir)),
        "state_dir": os.path.abspath(state_dir),
    }
    if ctx:
        base_ctx.update(ctx)
    rubric = prompts.render(milestone, kind, base_ctx)

    ev_dir = os.path.join(state_dir, state.EVIDENCE_DIR, milestone_id)
    files = sorted(os.listdir(ev_dir)) if os.path.isdir(ev_dir) else []
    vlines = _read_verify_jsonl(ev_dir)
    index = [
        "",
        "=== EVIDENCE FOR %s (deterministic, produced by verify.py — NOT driver self-report) ===" % milestone_id,
        "evidence_dir: %s" % os.path.abspath(ev_dir),
        "files: %s" % (", ".join(files) if files else "(none)"),
        "verify.jsonl (确定性裁定流水，按 `sha256` 读):",
    ]
    index += vlines if vlines else ["  (no verify.jsonl yet)"]
    index += [
        "请打开 evidence_dir 下文件**亲自看证据**（证据由 verify.py 确定性产出、非 driver 自述），",
        "按上面 rubric 的三层/各维度裁定，最后输出规定格式的 VERDICT 块。",
        "",
    ]
    return rubric + "\n" + "\n".join(index)


# ---- 降级出口（永远良构 dict）----------------------------------------------

def _result(verdict, ok, kind, parsed, raw, judge_cmd, exit_code, timed_out,
            duration_ms, milestone, reason, evidence_path=None) -> dict:
    """组装**永远良构**的裁定 dict（键稳定，即使 judge 崩/超时/垃圾）。judge_cmd/raw 已脱敏。"""
    return {
        "verdict": verdict,
        "ok": bool(ok),
        "kind": kind,
        "parsed": parsed,
        "raw": _sanitize(raw),
        "judge_cmd": _sanitize(judge_cmd),
        "exit_code": exit_code,
        "timed_out": bool(timed_out),
        "duration_ms": int(duration_ms),
        "milestone": milestone,
        "evidence_path": evidence_path,
        "reason": reason,
        "ts": _now(),
    }


# ---- 主 API -----------------------------------------------------------------

def review(state_dir, milestone_id, kind="impl_review", judge_cmd=None, ctx=None,
           timeout=DEFAULT_TIMEOUT, env=None, max_bytes=DEFAULT_MAX_BYTES) -> dict:
    """渲染 rubric + 收证据 → 调可配置 judge 命令 → 稳健解析 → 返回**永远良构**裁定 dict。

    **不抛异常**（除 kind 非法）：judge 起不来/超时/输出垃圾/越域/未配置，都走降级返回
    verdict=ERROR, ok=False，绝不让一次 review 把 loop 打崩（spec AC4「畸形输出稳健处理」）。

    exit_code 仅审计：它**绝不覆盖**一个白名单内已解析的裁定（呼应 verify.py「输出不决定」——
    判官退出码 ≠ 裁定。判官打印合法 VERDICT 块后 exit 2，裁定仍以 VERDICT 块为准、exit_code 只记录）。

    入参里**没有「声称的裁定」字段**——调用方只能给「要跑的 judge 命令」，给不了「结论」（结构性反作弊）。
    """
    if kind not in VALID_VERDICTS:
        raise ValueError("unknown review kind: %r; expected one of %s"
                         % (kind, tuple(VALID_VERDICTS)))
    cmd_template = resolve_judge_cmd(judge_cmd, env)

    # 渲染 rubric + 证据索引（这步出错也优雅降级，不崩）。
    try:
        prompt_body = _render_rubric_and_evidence(state_dir, milestone_id, kind, ctx)
    except Exception as e:  # milestone 缺失等 → 用法错由 CLI 拦；API 层稳健降级
        return _persist(state_dir, milestone_id, _result(
            "ERROR", False, kind, None, "", cmd_template, None, False, 0,
            milestone_id, "render failed: %s" % e))

    # C2：未配置 judge → 降级 ERROR/ok=False（基建，exit3，不烧 attempt），绝不乱猜某 agent。
    if not cmd_template.strip():
        return _persist(state_dir, milestone_id, _result(
            "ERROR", False, kind, None, "", cmd_template, None, False, 0, milestone_id,
            "no judge command configured; set --judge-cmd or $LONGHAUL_JUDGE_CMD"))

    # prompt 写临时文件（judge 从 {prompt_file} 读；最通用，不强制 stdin 语义）。
    fd, prompt_file = tempfile.mkstemp(prefix="lhb-review-prompt-", suffix=".md")
    ev_dir = os.path.join(state_dir, state.EVIDENCE_DIR, milestone_id)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(prompt_body)

        # 占位替换：只替换我们定义的命名占位；未定义占位/字面 { → format 抛错 → 降级（不崩）。
        repl = {
            "prompt_file": prompt_file,
            "evidence_dir": os.path.abspath(ev_dir),
            "state_dir": os.path.abspath(state_dir),
            "milestone_id": milestone_id,
        }
        try:
            cmd = cmd_template.format(**repl)
        except (KeyError, IndexError, ValueError) as e:
            # 未知占位 {bogus} → KeyError；字面孤立 { → ValueError —— 都优雅降级，不崩。
            return _persist(state_dir, milestone_id, _result(
                "ERROR", False, kind, None, "", cmd_template, None, False, 0, milestone_id,
                "bad judge_cmd template (%s): %s" % (type(e).__name__, e)))

        # 跑 judge：复用 verify._run_cmd（跑命令+超时杀进程组，不重造）。judge_cmd 是用户配的
        # shell 模板（同 cron 信任边界），use_shell=True；占位值全是我们生成的绝对路径，无外部不可信拼接。
        exit_code, raw_bytes, timed_out, duration_ms = verify._run_cmd(
            cmd, use_shell=True, cwd=os.path.dirname(os.path.abspath(state_dir)),
            env=None, timeout=timeout, max_bytes=max_bytes)
        raw = raw_bytes.decode("utf-8", errors="surrogateescape") if raw_bytes else ""

        if timed_out:
            return _persist(state_dir, milestone_id, _result(
                "ERROR", False, kind, None, raw, cmd_template, None, True, duration_ms,
                milestone_id, "judge timed out after %ss → degrade (infra)" % timeout))
        if exit_code is None:
            return _persist(state_dir, milestone_id, _result(
                "ERROR", False, kind, None, raw, cmd_template, None, False, duration_ms,
                milestone_id, "judge command not found / not executable → degrade (infra)"))

        # 稳健解析（VERDICT 块首选 + JSON fallback + 白名单）。
        parsed = parse_verdict(raw, kind)
        if parsed is None:
            return _persist(state_dir, milestone_id, _result(
                "ERROR", False, kind, None, raw, cmd_template, exit_code, False, duration_ms,
                milestone_id, "judge output unparseable / verdict not in whitelist → degrade"))

        verdict = parsed["verdict"]
        # exit_code 仅审计：绝不覆盖白名单内已解析裁定（判官退出码 ≠ 裁定）。
        ok = True
        reason = "verdict=%s (judge exit_code=%s, audit-only)" % (verdict, exit_code)
        return _persist(state_dir, milestone_id, _result(
            verdict, ok, kind, parsed.get("parsed"), raw, cmd_template, exit_code, False,
            duration_ms, milestone_id, reason))
    finally:
        try:
            os.remove(prompt_file)
        except OSError:
            pass


def _persist(state_dir, milestone_id, result: dict) -> dict:
    """落 review json + 追加 review.jsonl + state.append_event 写 events.jsonl（不重造事件流）。

    所有落盘内容里 judge_cmd/raw 已在 _result 里脱敏。落盘失败不阻断裁定本身（返回 result）。
    """
    kind = result["kind"]
    ev_dir = os.path.join(state_dir, state.EVIDENCE_DIR, milestone_id)
    try:
        os.makedirs(ev_dir, exist_ok=True)
        rj = os.path.join(ev_dir, "review-%s.json" % kind)
        verify._atomic_write_text(rj, json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        result["evidence_path"] = os.path.realpath(rj)
        # per-milestone review 流水（和 verify.jsonl 并列）
        with open(os.path.join(ev_dir, "review.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(result, ensure_ascii=False) + "\n")
    except OSError:
        pass
    # 全局 events.jsonl（F8 timeline 主消费源）——judge_cmd 已脱敏。
    try:
        state.append_event(state_dir, "review", milestone=milestone_id, kind=kind,
                           verdict=result["verdict"], ok=result["ok"],
                           judge_cmd=result["judge_cmd"], exit_code=result["exit_code"],
                           timed_out=result["timed_out"], duration_ms=result["duration_ms"],
                           evidence=result.get("evidence_path"))
    except OSError:
        pass
    return result


# ---- CLI --------------------------------------------------------------------

def _exit_code_for(result: dict) -> int:
    """裁定 dict → 退出码分层（§契约）。3=降级(基建)/1=产品打回/0=放行。"""
    if not result["ok"] or result["verdict"] == "ERROR":
        return 3  # 降级（含未配置/超时/畸形）——基建信号，不烧 attempt
    if result["verdict"] in PASS_ISH.get(result["kind"], ()):
        return 0  # 放行类
    return 1      # 白名单内但打回（FAIL/REVISE）——产品信号


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="review.py",
        description="可配置 AI 判官适配器：渲 rubric+收证据→调 judge 命令→稳健解析→良构裁定")
    ap.add_argument("state_dir")
    ap.add_argument("milestone_id")
    ap.add_argument("--kind", default="impl_review", choices=tuple(VALID_VERDICTS))
    ap.add_argument("--judge-cmd", default=None,
                    help="judge 命令模板（{prompt_file}/{evidence_dir}/{state_dir}/{milestone_id}）；"
                         "优先级 > $LONGHAUL_JUDGE_CMD > 空哨兵默认")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    ap.add_argument("--mode", default=None, help="透传进 ctx（plan-only|implement）")
    args = ap.parse_args(argv)

    # 用法错（退出码 2，没调 judge）：state_dir 不存在 / milestone 不在 milestones.json。
    if not os.path.isdir(args.state_dir):
        print("error: state_dir not found: %s" % args.state_dir, file=sys.stderr)
        return 2
    try:
        prompts._load_milestone(args.state_dir, args.milestone_id)
    except (OSError, KeyError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 2

    ctx = {"mode": args.mode} if args.mode else None
    result = review(args.state_dir, args.milestone_id, kind=args.kind,
                    judge_cmd=args.judge_cmd, ctx=ctx, timeout=args.timeout)
    # stdout：一行 JSON（供 loop.py 解析），和 verify.py 同构。
    print(json.dumps(result, ensure_ascii=False))
    # stderr：一行人读摘要（judge_cmd 已脱敏，截断）。
    jc = (result["judge_cmd"] or "")[:60]
    print("VERDICT %s ok=%s via %s → %s" % (
        result["verdict"], result["ok"], jc, result.get("evidence_path")), file=sys.stderr)
    return _exit_code_for(result)


if __name__ == "__main__":
    raise SystemExit(main())
