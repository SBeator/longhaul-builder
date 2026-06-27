#!/usr/bin/env python3
"""longhaul-builder — verify.py：确定性证据闸（防 reward-hacking）。

设计立场（DESIGN.md §2.4① / spec AC3）：
- verify.py 是「确定性脚本层」——它**自己真跑探针**、抓**真实退出码 + 原始输出**落 evidence/、
  **只按真实退出码裁定**。它**不是**判官 agent（那是 F4 review.py 的 AI 层），刻意不碰 AI。
- 结构性反作弊：`verify()` 入参里**没有任何「声称的裁定」字段**——调用方只能给「要跑的命令」，
  给不了「结论」。裁定 = `_decide(returncode, timed_out)` 纯函数，**绝不解析探针 stdout/stderr 文字**。
  所以探针打印 "ALL TESTS PASSED" 但 `exit 1` → verdict 必为 FAIL；打印 "FAIL" 但 `exit 0` → PASS。
- 证据来自真实运行（subprocess 捕获的真实字节），不是任何人「描述的输出」。

退出码契约（与 state.py 的 0/2/3 不冲突——state.py 不产 1）：
  0 = 探针 PASS（真实退出码 == 0），证据已写
  1 = 探针 FAIL（非零 / 超时 / 命令不存在等），证据已写
  2 = verify.py 用法错（缺参数 / state_dir 不存在 / 空白 probe）——没跑探针

POSIX-only：超时真杀**进程组**依赖 `os.killpg` + `start_new_session=True`（POSIX）。

agent/基建无关：纯标准库。复用 state.append_event 写 events.jsonl（不重造事件流）。
"""

import argparse
import hashlib
import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time

# 复用 state.py 的 append_event/_now（events.jsonl 单文件，inline 会让格式漂移；门1 已裁定 import）。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import state  # noqa: E402

DEFAULT_TIMEOUT = 600
DEFAULT_MAX_BYTES = 1_000_000

#: #1 进度感知超时（卡死检测）：driver 长跑不再用"硬墙超时"——只在「既无文件改动又无输出」
#: 持续 stuck_timeout 秒才判死（慢但在产出的不杀），跑满 ceiling 秒才撞兜底天花板。
_POLL_INTERVAL = 20
_PRUNE_DIRS = {".git", ".longhaul", "node_modules", ".next", "dist", "build",
               "__pycache__", ".venv", "venv", ".turbo", "target", ".cache"}


def _killpg(proc):
    """超时/卡死真杀整个进程组（含 --shell 拉起的孙子进程），不留孤儿。"""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _max_mtime(root):
    """工作区最近文件改动时间（wall mtime；剪掉 .git/.longhaul/node_modules 等重/无关目录）。"""
    latest = 0.0
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _PRUNE_DIRS and not d.startswith(".longhaul")]
            for fn in filenames:
                try:
                    m = os.stat(os.path.join(dirpath, fn)).st_mtime
                    if m > latest:
                        latest = m
                except OSError:
                    pass
    except OSError:
        pass
    return latest


def _wait_with_stuck_detection(proc, ceiling, stuck_timeout, progress_dir, max_bytes):
    """进度感知等待：边跑边看「文件改动 + 输出」两路进展信号。

    无任何进展持续 stuck_timeout 秒 → 卡死，杀；跑满 ceiling 秒 → 兜底天花板，杀。
    返回 (out_bytes, exit_code|None, timed_out)。慢但在持续产出的 driver 不会被误杀
    （修最初"硬墙 1500s 把还在写代码的 driver 也杀掉"的白烧，#1）。
    """
    chunks, last_out = [], [time.monotonic()]

    def _drain():
        try:
            for line in iter(proc.stdout.readline, b""):
                chunks.append(line)
                last_out[0] = time.monotonic()
        except Exception:
            pass

    reader = threading.Thread(target=_drain, daemon=True)
    reader.start()
    t0 = time.monotonic()
    last_file_m = _max_mtime(progress_dir)
    last_progress = t0
    poll = max(1, min(_POLL_INTERVAL, stuck_timeout, ceiling))
    timed_out = False
    while True:
        try:
            proc.wait(timeout=poll)
            break                                   # driver 自己结束了
        except subprocess.TimeoutExpired:
            pass
        now = time.monotonic()
        cur = _max_mtime(progress_dir)
        if cur > last_file_m:                        # 有文件改动 = 在产出
            last_file_m = cur
            last_progress = now
        progress = max(last_progress, last_out[0])   # 文件 或 输出 任一路有动静都算进展
        if now - progress >= stuck_timeout:
            timed_out = True
            _killpg(proc)
            break                                    # 卡死：既无文件改动也无输出
        if now - t0 >= ceiling:
            timed_out = True
            _killpg(proc)
            break                                    # 兜底天花板（防"一直碰文件但永不收敛"）
    try:
        proc.wait(timeout=5)
    except Exception:
        pass
    reader.join(timeout=5)
    out = b"".join(chunks)
    exit_code = None if timed_out else proc.returncode
    return out, exit_code, timed_out


# ---- 裁定纯函数（反作弊根）-------------------------------------------------

def _decide(exit_code, timed_out: bool) -> str:
    """裁定**仅**是 (returncode, timed_out) 的纯函数；probe 的 stdout/stderr 文字**绝不参与**。

    PASS iff 探针真实退出码 == 0 且未超时。其余一律 FAIL（非零 / 超时 / 信号负码 / None）。
    严格 `== 0`（int 比较，不做真值判断，避免 None/"" 被当 falsy 误判）。这是结构性反作弊的核心：
    一个想作弊的探针唯一能做的是让命令真的以 0 退出——而那正是我们要的（探针真过）。
    """
    if timed_out:
        return "FAIL"
    return "PASS" if exit_code == 0 else "FAIL"


# ---- 跑探针：捕获真实输出 + 超时真杀进程组 ----------------------------------

def _run_cmd(argv_or_str, use_shell: bool, cwd: str, env, timeout, max_bytes,
             stuck_timeout=None, progress_dir=None):
    """跑一个命令，返回 (exit_code|None, raw_bytes, timed_out, duration_ms)。**共享给 review.py 复用。**

    超时真杀**进程组**（门1 REQUIRED_CHANGE）：不依赖 subprocess.run(timeout=)（只杀直接子进程、
    会留孤儿如 --shell 起的 node/pytest）。改用 Popen(start_new_session=True) 开新会话/进程组，
    TimeoutExpired 时 os.killpg(os.getpgid(pid), SIGKILL) 整组杀，再 communicate() drain 部分输出。

    命令不存在/不可执行（FileNotFoundError/PermissionError）→ 返回 exit_code=None（由上层判 FAIL/降级，不崩）。

    这是 verify.py（确定性探针）与 review.py（可配置判官）共用的"跑命令+超时杀进程组"唯一实现
    （DESIGN「能力层别重复造机器」）：两边只是对返回值的语义解读不同，跑命令的机器是同一台。
    """
    if use_shell:
        popen_arg = argv_or_str if isinstance(argv_or_str, str) else " ".join(argv_or_str)
    else:
        popen_arg = shlex.split(argv_or_str) if isinstance(argv_or_str, str) else list(argv_or_str)

    t0 = time.monotonic()
    try:
        proc = subprocess.Popen(
            popen_arg,
            shell=use_shell,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,   # stdout/stderr interleaved（忠实现场，对齐 既有单文件证据）
            start_new_session=True,     # POSIX：开新进程组，超时可整组杀，不留孤儿
        )
    except (FileNotFoundError, PermissionError, NotADirectoryError, OSError):
        # 命令找不到 / 不可执行 → 不抛栈、不崩；exit_code=None 让上层判 FAIL/降级。
        duration_ms = int((time.monotonic() - t0) * 1000)
        return None, b"", False, duration_ms

    timed_out = False
    if stuck_timeout and progress_dir:
        # #1：driver 长跑走「进度感知」——慢但在产出不杀，只杀真卡死 / 撞兜底天花板。
        out, exit_code, timed_out = _wait_with_stuck_detection(
            proc, timeout, stuck_timeout, progress_dir, max_bytes)
    else:
        # 探针/判官等短命令：保持硬墙超时（确定性、短，不需要进度感知）。
        try:
            out, _ = proc.communicate(timeout=timeout)
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            # 真杀整个进程组（含 shell 拉起的孙子进程），再 drain 已产出的 partial output。
            _killpg(proc)
            try:
                out, _ = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                out = b""
            exit_code = None  # 超时 = 无有效退出码

    duration_ms = int((time.monotonic() - t0) * 1000)
    raw = out or b""
    if len(raw) > max_bytes:
        # 截断只影响输出体积，**不影响裁定**（verify 裁定只看退出码，与输出量无关）。
        note = ("\n[... TRUNCATED at %d bytes, original was %d bytes ...]\n"
                % (max_bytes, len(raw))).encode("utf-8")
        raw = raw[:max_bytes] + note
    return exit_code, raw, timed_out, duration_ms


def _run_probe(argv_or_str, use_shell: bool, cwd: str, env, timeout, max_bytes):
    """跑探针（薄封装 _run_cmd，行为不变）。保留此名只为可读性，逻辑全在共享的 _run_cmd。"""
    return _run_cmd(argv_or_str, use_shell, cwd, env, timeout, max_bytes)


# ---- 证据落盘（原子写，沿用 state._atomic_write 思路）-----------------------

def _evidence_text(milestone_id, name, probe_repr, cwd, use_shell, timeout,
                   raw_bytes, duration_ms, timed_out, exit_code) -> str:
    """组装证据文件文本（沿用既有证据约定：原始输出 + 末尾 EXIT_CODE= 锚点）。"""
    head = (
        f"=== VERIFY {milestone_id} :: {name} @ {state._now()} ===\n"
        f"PROBE: {probe_repr}\n"
        f"CWD: {cwd}\n"
        f"SHELL: {use_shell}\n"
        f"TIMEOUT: {timeout}\n"
        f"--- STDOUT+STDERR (interleaved, raw, verbatim) ---\n"
    )
    body = raw_bytes.decode("utf-8", errors="surrogateescape")
    tail = (
        f"\n--- END OUTPUT ---\n"
        f"DURATION_MS={duration_ms}\n"
        f"TIMED_OUT={'true' if timed_out else 'false'}\n"
        f"EXIT_CODE={exit_code if exit_code is not None else 'None'}\n"
    )
    return head + body + tail


def _atomic_write_text(path: str, text: str) -> None:
    """先写 .tmp 再 os.replace，保证 reviewer 不读到半截证据（复用 state._atomic_write 思路）。"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


# ---- 主 API -----------------------------------------------------------------

def verify(state_dir, milestone_id, probe_cmd, name="probe", timeout=DEFAULT_TIMEOUT,
           cwd=None, env=None, use_shell=False, max_bytes=DEFAULT_MAX_BYTES) -> dict:
    """确定性跑探针、抓真实证据、**只按真实退出码裁定**。返回 result dict（keys 稳定）。

    注意：入参里**没有任何「声称的裁定」字段**——调用方给不了结论，只能给要跑的命令（结构性反作弊）。
    cwd 默认 = 被建项目根 = dirname(abspath(state_dir))（.longhaul/ 的父目录，对齐 prompts.py 推法）。
    """
    if cwd is None:
        cwd = os.path.dirname(os.path.abspath(state_dir))

    exit_code, raw, timed_out, duration_ms = _run_probe(
        probe_cmd, use_shell, cwd, env, timeout, max_bytes)
    verdict = _decide(exit_code, timed_out)  # 唯一裁定点：纯函数，不读输出文字

    probe_repr = probe_cmd if isinstance(probe_cmd, str) else repr(list(probe_cmd))
    if timed_out:
        reason = f"timeout {timeout}s → FAIL"
    elif exit_code is None:
        reason = "command not found / not executable → FAIL"
    else:
        reason = f"exit {exit_code} → {verdict}"

    # 证据文件 evidence/<id>/<name>.txt（原子写）
    ev_dir = os.path.join(state_dir, state.EVIDENCE_DIR, milestone_id)
    ev_path = os.path.join(ev_dir, f"{name}.txt")
    ev_text = _evidence_text(milestone_id, name, probe_repr, cwd, use_shell, timeout,
                             raw, duration_ms, timed_out, exit_code)
    _atomic_write_text(ev_path, ev_text)
    ev_abs = os.path.realpath(ev_path)  # 绝对 realpath，便于 reviewer/loop 直接打开
    sha256 = hashlib.sha256(open(ev_path, "rb").read()).hexdigest()

    result = {
        "verdict": verdict,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "evidence_path": ev_abs,
        "sha256": sha256,
        "duration_ms": duration_ms,
        "milestone": milestone_id,
        "name": name,
        "probe": probe_repr,
        "reason": reason,
        "ts": state._now(),
    }

    # 审计：① per-milestone verify.jsonl（验收流水）② 全局 events.jsonl（F8 timeline 主消费源）
    _append_audit(state_dir, ev_dir, result)
    return result


def _append_audit(state_dir, ev_dir, result: dict) -> None:
    """① evidence/<id>/verify.jsonl 追加一行 ② state.append_event 写 events.jsonl（不重造事件流）。"""
    audit = {
        "ts": result["ts"], "milestone": result["milestone"], "name": result["name"],
        "probe": result["probe"], "exit_code": result["exit_code"], "verdict": result["verdict"],
        "duration_ms": result["duration_ms"], "timed_out": result["timed_out"],
        "evidence_path": result["evidence_path"], "sha256": result["sha256"],
    }
    os.makedirs(ev_dir, exist_ok=True)
    with open(os.path.join(ev_dir, "verify.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(audit, ensure_ascii=False) + "\n")
    # events.jsonl 复用 state.append_event（格式与 state.py 一致；events.jsonl 必存在父目录）。
    try:
        state.append_event(state_dir, "verify", milestone=result["milestone"],
                           name=result["name"], verdict=result["verdict"],
                           exit_code=result["exit_code"], duration_ms=result["duration_ms"],
                           timed_out=result["timed_out"], evidence=result["evidence_path"])
    except OSError:
        pass  # events.jsonl 写不了不该阻断验收本身（证据已落）


# ---- CLI --------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="verify.py",
        description="确定性证据闸：跑探针、抓真实退出码+原始输出落 evidence/、只按退出码裁定")
    ap.add_argument("state_dir")
    ap.add_argument("milestone_id")
    ap.add_argument("--probe", required=True, help="要跑的探针命令（CLI 默认按一句 shell 跑）")
    ap.add_argument("--name", default="probe", help="证据文件名（区分同 milestone 多次 verify）")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    ap.add_argument("--cwd", default=None)
    ap.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    # CLI 给人用：默认 --shell（"命令行就是 shell"直觉）；--no-shell 走 shlex 拆分。
    ap.add_argument("--shell", dest="shell", action="store_true", default=True)
    ap.add_argument("--no-shell", dest="shell", action="store_false")
    args = ap.parse_args(argv)

    # 用法错（退出码 2，没跑探针、不造目录）：state_dir 不存在 / 空白 probe。
    if not os.path.isdir(args.state_dir):
        print(f"error: state_dir not found: {args.state_dir}", file=sys.stderr)
        return 2
    if not args.probe or not args.probe.strip():
        print("error: --probe is empty/blank", file=sys.stderr)
        return 2

    result = verify(args.state_dir, args.milestone_id, args.probe, name=args.name,
                    timeout=args.timeout, cwd=args.cwd, use_shell=args.shell,
                    max_bytes=args.max_bytes)
    # stdout：一行 JSON（供 loop.py 解析）；探针原始输出已落证据，不喷 stdout 污染这行。
    print(json.dumps(result, ensure_ascii=False))
    # stderr：一行人读摘要。
    print(f"{result['verdict']} {args.milestone_id} exit={result['exit_code']} "
          f"→ {result['evidence_path']}", file=sys.stderr)
    return 0 if result["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
