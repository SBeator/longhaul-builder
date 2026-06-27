#!/usr/bin/env python3
"""converge.py —— 提案↔审 收敛（agent 无关，与 review_panel 同源）。

一个 proposer 写/改一份 artifact，一个独立 reviewer 反复审，循环到 reviewer APPROVE 或撞轮次上限；
撞上限仍不一致 → escalate=True（调用方升级给人裁决，绝不无限 ping-pong）。

用于 longhaul P0 的「spec 双 agent 收敛」（proposer=Claude 改 spec、reviewer=Codex 审 spec），
也可复用到任何"写→审→改"收敛场景。命令注入、不绑死任何 agent（P0-2）；跑命令复用 verify._run_cmd
（"跑命令+超时杀进程组"唯一实现，不重造）。reviewer 必须吐 `VERDICT: APPROVE|REVISE`（畸形=保守判 REVISE）。
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import verify  # noqa: E402

DEFAULT_MAX_ROUNDS = 3
_VERDICT_RE = re.compile(r"VERDICT\s*:\s*(APPROVE|REVISE)", re.I)
_FEEDBACK_RE = re.compile(r"(?:REASON|FEEDBACK|CONDITIONS)\s*:\s*(.+)", re.I | re.S)


def parse_verdict(out):
    """reviewer 输出 → (APPROVE|REVISE, feedback)。没给明确 VERDICT = REVISE（保守，继续改不放过）。"""
    m = _VERDICT_RE.search(out or "")
    verdict = m.group(1).upper() if m else "REVISE"
    fm = _FEEDBACK_RE.search(out or "")
    fb = (fm.group(1).strip() if fm else (out or "").strip())
    return verdict, fb[:4000]


def _run(cmd, cwd, timeout):
    ec, raw, to, _dur = verify._run_cmd(cmd, True, cwd, None, timeout, verify.DEFAULT_MAX_BYTES)
    return (raw or b"").decode("utf-8", "replace"), ec, to


def converge(artifact, proposer_cmd, reviewer_cmd, max_rounds=DEFAULT_MAX_ROUNDS,
             cwd=None, timeout=verify.DEFAULT_TIMEOUT, context=""):
    """proposer 改 artifact、reviewer 审，循环到 APPROVE 或撞 max_rounds。

    artifact 须已有初稿（reviewer 先审初稿＝第 1 轮）。proposer_cmd / reviewer_cmd 是 shell 模板，
    占位 `{artifact}` / `{feedback}` / `{context}`。返回：
      {converged: bool, rounds: int（讨论了几轮）, escalate: bool, verdict, history: [...每轮]}。
    """
    cwd = cwd or os.path.dirname(os.path.abspath(artifact)) or "."
    fb_file = artifact + ".review.txt"
    history = []
    verdict = "REVISE"
    for i in range(1, max_rounds + 1):
        rcmd = reviewer_cmd.format(artifact=artifact, context=context, feedback=fb_file)
        out, ec, to = _run(rcmd, cwd, timeout)
        verdict, feedback = parse_verdict(out)
        history.append({"round": i, "verdict": verdict,
                        "reviewer_ok": (ec == 0 and not to), "feedback": feedback[:600]})
        if verdict == "APPROVE":
            return {"converged": True, "rounds": i, "escalate": False,
                    "verdict": "APPROVE", "history": history}
        if i == max_rounds:
            break
        try:
            with open(fb_file, "w", encoding="utf-8") as f:
                f.write(feedback)
        except OSError:
            pass
        pcmd = proposer_cmd.format(artifact=artifact, feedback=fb_file, context=context)
        _run(pcmd, cwd, timeout)   # proposer 就地改 artifact
    return {"converged": False, "rounds": len(history), "escalate": True,
            "verdict": verdict, "history": history}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="converge.py",
                                 description="提案↔审 收敛（agent 无关）：proposer 改 artifact、reviewer 审到一致或撞上限")
    ap.add_argument("artifact", help="要收敛的文件（须已有初稿）")
    ap.add_argument("--proposer-cmd", required=True, help="改稿命令模板，占位 {artifact}/{feedback}/{context}")
    ap.add_argument("--reviewer-cmd", required=True, help="审稿命令模板（须吐 VERDICT: APPROVE|REVISE）")
    ap.add_argument("--max-rounds", type=int, default=DEFAULT_MAX_ROUNDS)
    ap.add_argument("--context", default="", help="给双方的背景文件路径（需求+澄清问答等）")
    ap.add_argument("--timeout", type=int, default=verify.DEFAULT_TIMEOUT)
    ap.add_argument("--out-json", default=None, help="把结果 JSON 落到该文件")
    a = ap.parse_args(argv)
    res = converge(a.artifact, a.proposer_cmd, a.reviewer_cmd, max_rounds=a.max_rounds,
                   timeout=a.timeout, context=a.context)
    out = json.dumps(res, ensure_ascii=False, indent=2)
    if a.out_json:
        try:
            with open(a.out_json, "w", encoding="utf-8") as f:
                f.write(out + "\n")
        except OSError:
            pass
    print(out)
    return 0 if res["converged"] else 3   # 3 = 撞上限未收敛（调用方据此升级给人）


if __name__ == "__main__":
    raise SystemExit(main())
