#!/usr/bin/env python3
"""longhaul-builder — loop.py：确定性自驱 tick（THE CRUX · F5 / AC5 / DESIGN §2.3·§2.6）。

loop.py 是**一个很小的确定性编排循环**（非 AI）。一个 `tick` 只做 5 件事、推**恰好一相位**然后退出：
1. 读 cursor/milestones 确定当前在哪个 milestone 的哪个 phase（程序计数器，确定性捡起，禁自由重规划）。
2. 按 phase 渲染 prompt（prompts.render，**显式**传 mode）→ 调一个**可配置的 driver shell 命令**写文件。
3. 跑确定性闸 verify.py（仅 impl_review、且有可执行探针时）+ AI 判官 review.py。
4. 把 (verify 退出码 + review 裁定) **确定性映射**到 state.py 的 gate 动词，由 state.py 改状态。
5. 每个子步把 started/duration 记进 events.jsonl（复用 state.append_event）。

红线（边界）：
- ❌ 不自己改状态文件——state.py 是唯一写状者（loop 只调其子命令）。例外：loop 自己维护的 cursor
  侧计数（infra_retries / infra_blocked / replan_count）是 loop 的私有账，state.py 不碰这几段。
- ❌ 不自己裁定真伪——verify(确定性)/review(质量)。loop 只搬运退出码/裁定。
- ❌ 不绑死 agent——driver/judge 命令由 flag/env/空哨兵默认注入；本文件不出现具体 agent 名。
- ❌ 不是长脚本——一个 tick 只推一相位；分几步来自状态文件（AI 规划产物），不写死在脚本里。
- ✅ F6：每 tick 开头（守卫之后）消费 `.longhaul/inbox/`（pause/resume/abort/redirect/respec）。
- ✅ F7 看门狗（watchdog · AC7 · DESIGN §2.5）：派活前取 lease、`finally` 释放；下一 tick 在 inbox
  消费 + 守卫之后、派活之前做一次 TTL sweep——孤儿 lease（心跳过期 **且** pid 不在世）→ reclaim+rerun，
  活 lease（心跳新鲜 **或** 同机 pid 在世）绝不误回收；reclaim 反复触发撞 max_reclaims 软上限（第三维，
  正交于 attempt/infra/replan，**不烧 product attempt**）→ 升级。不造常驻监工——sweep 是下一 tick 顺手做的。

退出码契约（供 loop.sh / cron 读）：
  0 = 本 tick 成功推进一相位 / 无 actionable / pause no-op（稳定态）
  2 = loop 用法错（state_dir 不存在 / 无 milestones / 非法 phase 落到 loop）
  3 = 产品熔断 BLOCKED（某 milestone 超 max_attempts，由 state.py 触发）
  4 = 基建第二维熔断 / replan 软上限熔断（driver/judge 一直坏 或 判官/redirect 一直 reopen）→ 升级人
  5 = 人工 abort via inbox（ABORT_EXIT；独立于 4 的机器熔断，loop.sh/cron/看板可判别"人主动停"）
  6 = P0 未确认（P0_GATE_EXIT；§1.5 硬门：放行 build 前必须人 `state.py p0-confirm`，循环拒绝派 build 活）

agent/基建无关：纯标准库。复用 prompts.render / verify.verify+_run_cmd / review.review / state.* CLI。
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import prompts  # noqa: E402  渲染 driver prompt（单一事实源）
import state    # noqa: E402  状态台账（唯一写状者）+ events
import verify   # noqa: E402  确定性证据闸 + _run_cmd（跑命令+超时杀进程组，不重造）
import review   # noqa: E402  可配置 AI 判官适配器

DEFAULT_MAX_INFRA_RETRIES = 5
DEFAULT_MAX_REPLANS = 5
# #1：driver 超时改为「进度感知」。DRIVER_TIMEOUT 现在是**兜底天花板**（慢但在产出的不撞它）；
# 真正的判死靠 STUCK_TIMEOUT（既无文件改动又无输出多久＝卡死）。天花板默认调宽到 1h，
# 卡死窗默认 10min。探针/判官仍是硬墙短超时。
DEFAULT_DRIVER_TIMEOUT = int(os.environ.get("LONGHAUL_DRIVER_TIMEOUT", "3600"))
DEFAULT_DRIVER_STUCK_TIMEOUT = int(os.environ.get("LONGHAUL_DRIVER_STUCK_TIMEOUT", "600"))
DEFAULT_PROBE_TIMEOUT = int(os.environ.get("LONGHAUL_PROBE_TIMEOUT", "600"))
DEFAULT_REVIEW_TIMEOUT = int(os.environ.get("LONGHAUL_REVIEW_TIMEOUT", "600"))

#: F7 看门狗（watchdog · AC7 · DESIGN §2.5）默认值。
#: lease TTL = 心跳新鲜免查 pid 的快路径阈值 + pid 复用误判的第二道闸（取宽松 step 上界）。
DEFAULT_LEASE_TTL = 1800            # 秒；过期判据 = now - heartbeat > ttl
DEFAULT_MAX_RECLAIMS = 3            # 第三维软上限：可复现崩溃几次后升级（正交于 attempt/infra/replan）
LEASE_OWNER = "loop-tick"          # 审计用逻辑 owner 名（固定串，非进程名）

#: F6 干预 inbox（intervention inbox · AC6 · DESIGN §2.6）。
INBOX_DIR = "inbox"                       # state_dir/inbox/（人/绑定往这写一文件一消息）
#: D 簇新增 resolve/confirm/reject——人对 driver 举旗(NEEDS_CONFIRM)的异步回插。
KINDS = ("pause", "resume", "abort", "redirect", "respec", "resolve", "confirm", "reject", "split")
#: 人工 abort 专用退出码（区别于 infra/replan 升级的 4，让 loop.sh/cron/看板判别"人主动停"）。
ABORT_EXIT = 5

#: F8 P0 硬门专用退出码（§1.5 必停门之一）：P0 未确认时循环拒绝派 build 活、等人 `state.py p0-confirm`。
#: 与 abort(5)/熔断(4) 区分开，让 loop.sh/cron/看板能识别"卡在 P0 门、等人确认"这个独立态。
P0_GATE_EXIT = 6

#: driver 命令未配置时的空哨兵默认——**不硬绑任何 agent**（P0-2，镜像 review.DEFAULT_JUDGE_CMD）。
DEFAULT_DRIVER_CMD = ""

#: driver 调用的两类返回：成功退出 vs 基建故障（命令找不到/超时/未配置）。
RC_OK = "ok"
RC_INFRA = "infra"


# ---- driver 命令解析（与 review.resolve_judge_cmd 同构）----------------------

def resolve_driver_cmd(explicit=None, env=None, phase=None) -> str:
    """driver 命令解析（#10a 分阶段可配不同 agent）：

    分阶段 env `LONGHAUL_DRIVER_CMD__<phase>`（最具体，如 __plan / __impl）
      > CLI/API 显式 > 通用 env `LONGHAUL_DRIVER_CMD` > 空哨兵默认（P0-2）。
    没配分阶段就回落通用槽，**完全向后兼容**（phase=None 即旧行为）。未来 test/e2e 阶段同理扩展。
    全空 → "" ；invoke_driver 据此降级为 INFRA_FAIL（明确报"未配置"，不乱猜 agent）。
    """
    env = os.environ if env is None else env
    if phase:
        per = env.get("LONGHAUL_DRIVER_CMD__%s" % phase)
        if per:
            return per
    return explicit or env.get("LONGHAUL_DRIVER_CMD") or DEFAULT_DRIVER_CMD


PLAN_PANEL_DELIM = "|||"


def resolve_plan_panel(env=None):
    """#10b plan 多 agent 协同：`LONGHAUL_PLAN_PANEL` 用 `|||` 分隔多个 plan reviewer 命令。

    返回去空白后的命令列表。配 ≥2 个 → plan_review 走 N 人 panel（_review 里判）；
    没配 / 只 1 个 → 回落 #10a 的单审 judge（向后兼容）。
    """
    env = os.environ if env is None else env
    raw = env.get("LONGHAUL_PLAN_PANEL") or ""
    return [c.strip() for c in raw.split(PLAN_PANEL_DELIM) if c.strip()]


# ---- cursor 侧 loop 私有账（infra_retries / infra_blocked / replan_count）-----
# 这几段是 loop 维护的（基建第二维熔断 + replan 软上限），state.py 不碰。

def _cursor(state_dir):
    return state.load_cursor(state_dir)


def _save_cursor(state_dir, cur):
    state.save_cursor(state_dir, cur)


def _infra_blocked(cur):
    return cur.get("infra_blocked") or []


def _reset_infra(state_dir, mid):
    """某 milestone 任一子步成功推进后，清零它的**瞬态计数**（infra_retries + F7 reclaim_count）。

    ADOPT-3（gate-1）：每个清 infra 的成功点（plan advance / impl advance / 两个 gate-pass）都**必须**
    同时清 reclaim_count[mid]——否则一次 reclaim 会跨过一个成功相位残留，下次崩溃叠加上去会**误升级**
    （把"成功推进过一次"的瞬时崩溃错误地累进 crash-loop 判定）。与 infra_retries 同款"成功即清瞬态"语义。
    """
    cur = _cursor(state_dir)
    dirty = False
    ir = cur.get("infra_retries") or {}
    if ir.get(mid):
        ir[mid] = 0
        cur["infra_retries"] = ir
        dirty = True
    rc = cur.get("reclaim_count") or {}
    if rc.get(mid):
        rc[mid] = 0
        cur["reclaim_count"] = rc
        dirty = True
    if dirty:
        _save_cursor(state_dir, cur)


# ---- 子步计时 → events.jsonl（F8 timeline 数据源）---------------------------

def _timed(state_dir, mid, phase, step, fn):
    """跑一个子步并把 started/duration 记进 events.jsonl（step_timing），返回 fn 的结果。"""
    t0 = time.monotonic()
    started = state._now()
    result = fn()
    dur = int((time.monotonic() - t0) * 1000)
    try:
        rc_repr = result if isinstance(result, (int, str)) else None
        state.append_event(state_dir, "step_timing", milestone=mid, phase=phase,
                           step=step, started=started, duration_ms=dur, rc=rc_repr)
    except OSError:
        pass
    return result, dur


# ---- state.py 子命令薄封装（loop 只调，不直接写状态文件）---------------------

def _run_state(state_dir, verb, mid, **kw):
    """调一个 state.py 子命令，返回退出码（0/2/3）。计时进 events。"""
    argv = [verb, state_dir, mid]
    if verb == "gate-pass" or verb == "gate-fail":
        argv += ["--gate", kw["gate"]]
        if kw.get("error") is not None:
            argv += ["--error", kw["error"]]
    elif verb == "reopen-plan":
        if kw.get("error") is not None:
            argv += ["--error", kw["error"]]
    rc, _ = _timed(state_dir, mid, kw.get("phase", "?"), "state:%s" % verb,
                   lambda: state.main(argv))
    return rc


# ---- driver 调用（可配置命令 + 模板占位 + 复用 verify._run_cmd）--------------

def _capture_driver_diag(state_dir, mid, mode, exit_code, raw):
    """#13：driver 非零退出 → 末尾输出落 `evidence/<mid>/driver-nonzero-exit.txt` + 返回一句话摘要供 reason。

    让"driver exited 1"不再是黑盒：build-3 里 AC1 连 5 次 exited-1 才熔断、却看不到为啥；有了这个，
    连续非零退出的 reason 直接带末尾报错（command not found / ModuleNotFound / permission denied…），
    熔断升级时人一眼能定位是环境还是命令问题。
    """
    s = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else (raw or "")
    tail = s[-2000:].strip()
    try:
        ev = os.path.join(state_dir, "evidence", mid)
        os.makedirs(ev, exist_ok=True)
        with open(os.path.join(ev, "driver-nonzero-exit.txt"), "w", encoding="utf-8") as f:
            f.write("mode=%s exit_code=%s\n\n--- driver 末尾输出 ---\n%s\n" % (mode, exit_code, tail))
    except OSError:
        pass
    last = ""
    for ln in reversed(tail.splitlines()):
        if ln.strip():
            last = ln.strip()[:160]
            break
    if last:
        return "；末尾：%s（详见 evidence/%s/driver-nonzero-exit.txt）" % (last, mid)
    return "（无输出，见 evidence/%s/driver-nonzero-exit.txt）" % mid


def invoke_driver(driver_cmd, prompt_text, state_dir, mid, mode, timeout, dry_run=False,
                  stuck_timeout=None):
    """渲好的 prompt 写临时文件 → 替换占位 → 复用 verify._run_cmd 跑。返回 (RC_OK|RC_INFRA, reason)。

    占位：{prompt_file}/{state_dir}/{milestone_id}/{mode}（F1 carry-forward：mode 显式传）。
    成功退出(exit 0)=RC_OK；命令找不到/超时/未配置/非零退出=RC_INFRA（driver 应正常跑完；它"业务失败"
    由后续 verify/review 判，不在 driver 步判）。dry_run 时只返回将派的命令，不真跑。
    """
    if not (driver_cmd or "").strip():
        return RC_INFRA, "no driver command configured; set --driver-cmd or $LONGHAUL_DRIVER_CMD"

    fd, prompt_file = tempfile.mkstemp(prefix="lhb-driver-prompt-", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(prompt_text)
        repl = {
            "prompt_file": prompt_file,
            "state_dir": os.path.abspath(state_dir),
            "milestone_id": mid,
            "mode": mode,
        }
        try:
            cmd = driver_cmd.format(**repl)
        except (KeyError, IndexError, ValueError) as e:
            return RC_INFRA, "bad driver_cmd template (%s): %s" % (type(e).__name__, e)

        if dry_run:
            return RC_OK, "DRY-RUN would run driver: %s" % cmd

        proj_dir = os.path.dirname(os.path.abspath(state_dir))
        exit_code, raw, timed_out, _dur = verify._run_cmd(
            cmd, use_shell=True, cwd=proj_dir,
            env=None, timeout=timeout, max_bytes=verify.DEFAULT_MAX_BYTES,
            stuck_timeout=stuck_timeout, progress_dir=proj_dir if stuck_timeout else None)
        _ti, _to = verify._extract_tokens(raw)   # #11：记 driver 这步 token 用量（含失败/超时步的浪费）
        if _ti or _to:
            try:
                state.append_event(state_dir, "token_usage", milestone=mid, phase=mode,
                                   role="driver", tokens_in=_ti, tokens_out=_to)
            except OSError:
                pass
        if timed_out:
            # #1：区分「卡死被早杀」(无文件/输出进展) 与「撞兜底天花板」——前者下次靠 _resume_note 喂 diff 续跑。
            if stuck_timeout and _dur is not None and _dur < timeout * 1000 * 0.9:
                return RC_INFRA, ("driver stuck: 无文件改动也无输出 %ss（已杀，下次喂 diff 续跑）"
                                  % stuck_timeout)
            return RC_INFRA, "driver timed out after %ss（撞兜底天花板）" % timeout
        if exit_code is None:
            return RC_INFRA, "driver command not found / not executable"
        if exit_code != 0:
            # 非零退出 = driver 没正常跑完（崩/127/2）→ 基建路径（不烧 attempt）。
            # #13：把末尾输出落证据 + 摘进 reason，让"driver exited N"不再黑盒——连续非零退出能直接查根因。
            return RC_INFRA, "driver exited %s (non-zero)%s" % (
                exit_code, _capture_driver_diag(state_dir, mid, mode, exit_code, raw))
        return RC_OK, "driver exit 0"
    finally:
        try:
            os.remove(prompt_file)
        except OSError:
            pass


# ---- 基建第二维熔断 + replan 软上限（cursor 私有账，正交于 attempt_count）-----

def infra_retry(state_dir, mid, reason, max_infra_retries, phase):
    """第二维：driver/judge 坏 → 计数 +1，达上限把 mid 进 infra_blocked 升级（return 4），否则原地重试(0)。

    **绝不**调任何会触发 _enter_impl 的 state 动词（不烧 attempt_count）。
    """
    cur = _cursor(state_dir)
    ir = cur.get("infra_retries") or {}
    n = ir.get(mid, 0) + 1
    ir[mid] = n
    cur["infra_retries"] = ir
    state.append_event(state_dir, "infra_retry", milestone=mid, phase=phase, count=n, reason=reason)
    if n >= max_infra_retries:
        ib = _infra_blocked(cur)
        if mid not in ib:
            ib.append(mid)
        cur["infra_blocked"] = ib
        cur["next_action"] = "%s 基建熔断升级人工：%s" % (mid, reason)
        _save_cursor(state_dir, cur)
        state.append_event(state_dir, "infra_break", milestone=mid, count=n, reason=reason)
        print("INFRA BREAK: %s infra_retries=%d >= max=%d; escalate (%s)"
              % (mid, n, max_infra_retries, reason), file=sys.stderr)
        return 4
    cur["next_action"] = "%s 基建重试 %d/%d：%s" % (mid, n, max_infra_retries, reason)
    _save_cursor(state_dir, cur)
    print("infra-retry %s %d/%d: %s" % (mid, n, max_infra_retries, reason), file=sys.stderr)
    return 0


def _bump_replan(state_dir, mid, max_replans, phase):
    """reopen-plan 软上限（ADOPT）：reopen-plan 绕过 attempt+infra 两熔断 → 判官一直 REOPEN_PLAN = livelock。

    返回 True = 已超软上限（调用方应升级，**不**再 reopen-plan）；False = 未超（可继续 reopen）。
    """
    cur = _cursor(state_dir)
    rc = cur.get("replan_count") or {}
    n = rc.get(mid, 0) + 1
    rc[mid] = n
    cur["replan_count"] = rc
    _save_cursor(state_dir, cur)
    state.append_event(state_dir, "replan", milestone=mid, phase=phase, count=n)
    return n >= max_replans


def _escalate_replan(state_dir, mid, max_replans, reason):
    """replan 软上限触发：把 mid 进 infra_blocked + 升级事件 + return 4（同 infra 维度升级人工）。"""
    cur = _cursor(state_dir)
    ib = _infra_blocked(cur)
    if mid not in ib:
        ib.append(mid)
    cur["infra_blocked"] = ib
    cur["next_action"] = "%s reopen-plan 软上限升级人工：%s" % (mid, reason)
    _save_cursor(state_dir, cur)
    state.append_event(state_dir, "replan_break", milestone=mid,
                       count=cur.get("replan_count", {}).get(mid), reason=reason)
    print("REPLAN BREAK: %s replans >= max=%d; escalate (%s)" % (mid, max_replans, reason),
          file=sys.stderr)
    return 4


# ---- F7 看门狗：lease + heartbeat + TTL sweep + rerun（AC7 · DESIGN §2.5）-----
# lease 存 cursor.lease 段（loop 私有循环控制位，不属 milestone 状态机；同 infra_blocked/paused）。
# 派活前 acquire、派活后 finally release；下一 tick 在 inbox 消费 + 守卫之后、派活之前 sweep。
# 活性判据 = heartbeat-OR-pid（见 is_lease_live）；reclaim 计第三维 reclaim_count（不烧 attempt）。

def resolve_lease_ttl(explicit=None, env=None) -> int:
    """lease TTL 解析：CLI/API 显式 > 环境 LONGHAUL_LEASE_TTL > DEFAULT_LEASE_TTL（镜像 resolve_driver_cmd）。"""
    if explicit is not None:
        return int(explicit)
    env = os.environ if env is None else env
    v = env.get("LONGHAUL_LEASE_TTL")
    if v and str(v).strip():
        try:
            return int(v)
        except ValueError:
            pass
    return DEFAULT_LEASE_TTL


def _parse_ts(s):
    """解析 lease 时间戳（state._now() 的 '%Y-%m-%dT%H:%M:%SZ' UTC 串）→ aware datetime；坏串→None。"""
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def pid_alive(pid) -> bool:
    """POSIX liveness 探测（纯标准库）：os.kill(pid, 0) = 只探测、不真发信号。

    ProcessLookupError → 该 pid 无活进程（死）。
    PermissionError    → 有进程但本进程无权给它发信号（别的用户的进程）→ **保守视为活**（不误杀）。
      ⚠️ 已知 MVP 取舍（gate-1 ADOPT-4）：多用户主机上，一个崩溃进程的 pid 被**另一个用户**的无关进程
      复用时，os.kill 会抛 PermissionError 而被这里读成"活"→ 该尸体 lease 永不被回收（漏杀而非误杀）。
      本设计偏保守漏杀（误杀正在跑的活 step 比晚回收尸体危险得多），故接受此取舍；非静默 bug。
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False            # 无此进程 → 死
    except PermissionError:
        return True             # 有进程但无权发信号（别的用户）→ 保守视为活（见上方取舍注释）
    return True


def is_lease_live(lease, ttl_default=DEFAULT_LEASE_TTL) -> bool:
    """活性判据（crux）：live = 心跳新鲜(<ttl) **OR** (同机 AND pid 在世)；
    dead = 心跳过期(>ttl) **AND** (跨机 OR pid 不在世)。

    心跳新鲜 → 直接判活（快路径，免查 pid）。心跳过期才查 pid 兜底——这正是防"同步 tick 阻塞在长 step
    无法刷心跳 → 心跳显得过期但进程活得好好的 → 被纯心跳误杀"的关键（DESIGN §2.5 / gate-1 ADOPT-1）。
    跨机（host≠本机）时 pid 无意义、直接不信 pid（走判死分支）。
    """
    if not isinstance(lease, dict):
        return False
    ttl = lease.get("ttl") or ttl_default
    hb = _parse_ts(lease.get("heartbeat"))
    now = datetime.now(timezone.utc)
    if hb is not None:
        age = (now - hb).total_seconds()
        if age <= ttl:
            return True                          # ① 心跳够新 → 活（无需查 pid）
    # ② 心跳过期/缺失 → 查 pid 兜底（仅同机有意义；跨机 pid 直接不信）
    if lease.get("host") == socket.gethostname() and pid_alive(lease.get("pid")):
        return True                              # 同机 + pid 在世 → 活（绝不误杀慢但活的 step）
    return False                                 # 心跳过期 且 (跨机 或 pid 不在世) → 判死


def acquire_lease(state_dir, mid, phase, opts):
    """派活前取一把 lease 写进 cursor.lease（owner/pid/host/milestone/phase/acquired=heartbeat=now/ttl）。

    若 cursor 已有一把**活**且 owner 不是本进程的 lease（pid 在世且 host 同机但 pid≠本进程）→ 理论上 flock
    已挡，防御性地**不覆盖**、记 lease_contended 事件后正常继续（同步形态不触发；留作异步/绕锁防御）。
    """
    cur = _cursor(state_dir)
    existing = cur.get("lease")
    if isinstance(existing, dict) and is_lease_live(existing, opts.get("lease_ttl", DEFAULT_LEASE_TTL)):
        same_host = existing.get("host") == socket.gethostname()
        if same_host and existing.get("pid") != os.getpid():
            state.append_event(state_dir, "lease_contended", milestone=mid,
                               held_by_pid=existing.get("pid"), my_pid=os.getpid())
            return  # 不覆盖别人的活 lease（flock 已挡，这里只是防御）
    now = state._now()
    cur["lease"] = {
        "owner": LEASE_OWNER,
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "milestone": mid,
        "phase": phase,
        "acquired": now,
        "heartbeat": now,
        "ttl": opts.get("lease_ttl", DEFAULT_LEASE_TTL),
    }
    _save_cursor(state_dir, cur)


def release_lease(state_dir, mid=None):
    """释放 lease：cursor.pop('lease') + save（放 finally 保证正常完成必清——AC7 隐含点）。

    幂等：无 lease 时 no-op。只清自己刚取的（同步串行下 lease 至多一把，pop 即清）。
    """
    cur = _cursor(state_dir)
    if cur.pop("lease", None) is not None:
        _save_cursor(state_dir, cur)


def _bump_reclaim(state_dir, mid, max_reclaims):
    """第三维 reclaim 软上限：判孤儿即 +1，返回 (n, escalate?)。n>=max → escalate=True（调用方升级、不再 rerun）。"""
    cur = _cursor(state_dir)
    rc = cur.get("reclaim_count") or {}
    n = rc.get(mid, 0) + 1
    rc[mid] = n
    cur["reclaim_count"] = rc
    _save_cursor(state_dir, cur)
    return n, (n >= max_reclaims)


def _escalate_reclaim(state_dir, mid, max_reclaims, reason):
    """reclaim 软上限触发：mid 进 infra_blocked（复用 F5 升级人工清单，_tick_body 已跳过）+ reclaim_break + return 4。"""
    cur = _cursor(state_dir)
    ib = _infra_blocked(cur)
    if mid not in ib:
        ib.append(mid)
    cur["infra_blocked"] = ib
    cur["next_action"] = "%s 看门狗 reclaim 软上限升级人工：%s" % (mid, reason)
    _save_cursor(state_dir, cur)
    state.append_event(state_dir, "reclaim_break", milestone=mid,
                       count=cur.get("reclaim_count", {}).get(mid), reason=reason)
    print("RECLAIM BREAK: %s reclaims >= max=%d; escalate (%s)" % (mid, max_reclaims, reason),
          file=sys.stderr)
    return 4


def sweep_stale_lease(state_dir, opts):
    """TTL sweep（守卫 + inbox 消费之后、派活之前调一次）。返回 None | 'escalate'。

    判孤儿（心跳过期 且 pid 不在世/跨机）→ reclaim：清 lease + 记 watchdog_reclaim + reclaim_count+1，
    让该 milestone 在**当前 attempt 内**重跑那一相位（不改 milestone status/phase、不烧 product attempt——
    它已是 IN_PROGRESS@phase，_next_todo 重发、claim 幂等续跑、driver 覆盖式重跑该相位，同 TC3）。
    撞 max_reclaims → escalate（升级人工，return 'escalate'）。活 lease 绝不回收（AC7「不被误回收」）。
    """
    cur = _cursor(state_dir)
    # —— F6 carry-forward：paused/aborted 的 active driver 是「故意空转」不是「死了」，绝不 sweep ——
    # （consume_inbox 已在 sweep 之前对 paused/abort 早退；这里是显式的第二道防线 belt-and-suspenders）
    if cur.get("aborted"):
        return None
    if cur.get("paused"):
        return None
    lease = cur.get("lease")
    if not lease:
        return None                              # 无 lease（旧 .longhaul / 正常释放后）→ 零成本跳过
    if is_lease_live(lease, opts.get("lease_ttl", DEFAULT_LEASE_TTL)):
        return None                              # ★活 lease 绝不回收（心跳新鲜 OR 同机 pid 在世）

    # —— 到这：lease 既心跳过期(>ttl) 又 (pid 不在世 或 跨机) → 判孤儿 ——
    mid = lease.get("milestone")
    phase = lease.get("phase")
    hb = _parse_ts(lease.get("heartbeat"))
    age = int((datetime.now(timezone.utc) - hb).total_seconds()) if hb else None

    n, escalate = _bump_reclaim(state_dir, mid, opts.get("max_reclaims", DEFAULT_MAX_RECLAIMS))
    if escalate:
        release_lease(state_dir, mid)            # 清掉，免得下个 tick 又判孤儿
        sig = _escalate_reclaim(state_dir, mid, opts.get("max_reclaims", DEFAULT_MAX_RECLAIMS),
                                "可复现崩溃：reclaim 反复触发（疑似 crash-loop）")
        return "escalate" if sig == 4 else None

    # reclaim：清 lease + 审计事件（不改 milestone status/phase——它已 IN_PROGRESS@phase，靠 rerun 续跑）
    release_lease(state_dir, mid)
    state.append_event(state_dir, "watchdog_reclaim", milestone=mid, phase=phase,
                       dead_pid=lease.get("pid"), dead_host=lease.get("host"),
                       stale_secs=age, reclaim_count=n)
    print("watchdog reclaim: %s @%s (dead pid=%s, stale=%ss, reclaim %d/%d)"
          % (mid, phase, lease.get("pid"), age, n, opts.get("max_reclaims", DEFAULT_MAX_RECLAIMS)),
          file=sys.stderr)
    return None


# ---- verify 探针解析（NL probe → 可执行命令 gap，§2.4）-----------------------

def resolve_probe(m):
    """从 milestone 取可执行探针：acceptance.probe_cmd 非空 → 用它；否则 None（跳 verify 靠 review）。"""
    acc = (m or {}).get("acceptance") or {}
    pc = acc.get("probe_cmd")
    return pc if (pc and str(pc).strip()) else None


# ---- A 簇：impl 改动机器捕获（不信 driver 自写"改了啥"，让 report.py 有真实来源）-------

def _git(proj, *args, timeout=20):
    """跑一条 git 子命令，返回 (returncode, stdout, stderr)；git 不可用/异常 → (127, "", ...)。"""
    try:
        r = subprocess.run(["git", "-C", proj, *args], capture_output=True,
                           text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except (OSError, subprocess.SubprocessError) as e:
        return 127, "", "git unavailable: %s" % e


def _capture_changed_files(state_dir, mid, baseline):
    """impl 结束后**机器捕获**本步改了哪些文件 → evidence/<mid>/changed-files.txt（report.py 读它）。

    用 git diff（相对实施开始时的 baseline），committed/uncommitted 都算。非 git 仓则诚实写明。
    绝不信 driver 自报"改了啥"——这是 A 簇"报告由证据机器渲染、不靠 agent 自觉"的根。
    """
    proj = os.path.dirname(os.path.abspath(state_dir))
    ev = os.path.join(state_dir, "evidence", mid)
    out = os.path.join(ev, "changed-files.txt")
    try:
        os.makedirs(ev, exist_ok=True)
    except OSError:
        return
    rc0, _o, _e = _git(proj, "rev-parse", "--is-inside-work-tree")
    if rc0 != 0:
        try:
            with open(out, "w", encoding="utf-8") as f:
                f.write("(项目非 git 仓，无法机器捕获改动文件)\n")
        except OSError:
            pass
        return
    lines = ["# 本 milestone impl 改动（git 机器捕获，非 driver 自写）", ""]
    parts = []
    if baseline:
        lines.append("## 相对实施开始 %s 的变更（已提交+工作区改动+新增未跟踪）" % baseline[:12])
        _, names, _ = _git(proj, "diff", "--name-status", baseline)
        _, stat, _ = _git(proj, "diff", "--stat", baseline)
        parts.append(names.strip())
        # git diff 不含未跟踪文件——driver 新建但没 commit 的文件靠这条补上。
        _, others, _ = _git(proj, "ls-files", "--others", "--exclude-standard")
        untracked = [ln for ln in others.splitlines() if ln.strip()]
        if untracked:
            parts.append("\n".join("A(未跟踪)\t" + u for u in untracked))
        parts.append(stat.strip())
    else:
        lines.append("## 工作区当前变更（无 baseline：实施前仓库无提交）")
        _, names, _ = _git(proj, "status", "--short")  # status --short 本就含未跟踪(??)
        parts.append(names.strip())
    body = "\n".join(p for p in parts if p)
    lines.append(body or "(无文件改动——driver 可能未改文件，或改动已在 baseline 之前提交)")
    try:
        with open(out, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError:
        return
    state.append_event(state_dir, "changed_files_captured", milestone=mid,
                       baseline=(baseline or "")[:12])


# ---- D 簇：driver 举旗检测（非阻塞举旗 + 异步确认 + 回插）---------------------

def _detect_and_raise_flag(state_dir, mid):
    """impl 后检测 driver 留下的 `evidence/<mid>/flag.json`（D 簇举旗：降级/偏离）。

    有则调 state.flag 把该 milestone 标 NEEDS_CONFIRM（非阻塞：cursor 推进到下一个能做的）+ 归档旗子
    （改名，免得该 milestone 被 resolve 重跑后旧旗重触发）。返回 True = 举了旗（loop 应跳过 impl_review）。
    """
    fpath = os.path.join(state_dir, "evidence", mid, "flag.json")
    if not os.path.isfile(fpath):
        return False
    flag = safe_load_json(fpath) or {}
    kind = flag.get("kind")
    if kind not in ("blocked-workaround", "spec-divergence"):
        kind = "spec-divergence"   # kind 不规范也当偏离处理（宁可举旗等人，不放过无声降级）
    summary = (flag.get("summary") or flag.get("detail") or "")[:300]
    rc = state.main(["flag", state_dir, mid, "--kind", kind, "--summary", summary])
    try:
        stamp = state._now().replace(":", "").replace("-", "")
        os.rename(fpath, os.path.join(state_dir, "evidence", mid, "flag-consumed-%s.json" % stamp))
    except OSError:
        pass
    return rc == 0


# ---- 各相位子例程（每相位做"该相位一件事"，做完调 state 动词，tick 退出）-----

def _resume_note(state_dir, mid, mode):
    """P0「超时不白跑」：impl 重试时告诉 driver「工作区已有部分进展、接着做完别重来」。

    仅在 mode=implement 且本步发生过 infra_retry（上次被超时/崩溃中断）且工作区确有未提交改动时，
    返回一段醒目的续跑指令；否则空串（driver.md 的 {{resume_context}} 渲染成空）。
    修复"driver 改的是真文件、超时被杀代码还在盘上，却拿同 prompt 从头重来"的白跑（2026-06-24）。
    """
    if mode != "implement":
        return ""
    n = (_cursor(state_dir).get("infra_retries") or {}).get(mid, 0)
    if n <= 0:
        return ""
    proj = os.path.dirname(os.path.abspath(state_dir))
    changed = []
    rc, head, _ = _git(proj, "rev-parse", "HEAD")
    if rc == 0 and head.strip():
        _, names, _ = _git(proj, "diff", "--name-only", head.strip())
        changed += [l.strip() for l in names.splitlines() if l.strip()]
    _, others, _ = _git(proj, "ls-files", "--others", "--exclude-standard")
    changed += [l.strip() for l in others.splitlines() if l.strip()]
    # 排除框架自己的状态目录（.longhaul/，像 .git——不是 driver 的代码进展，列出来反误导）
    sdir = os.path.basename(os.path.normpath(state_dir))
    changed = [f for f in changed if f != sdir and not f.startswith(sdir + "/")]
    changed = list(dict.fromkeys(changed))   # 去重保序
    if not changed:
        return ""   # 上次刚启动就被杀、没留下进展 → 当全新做
    flist = "\n".join("  - " + f for f in changed[:40])
    more = "" if len(changed) <= 40 else "\n  …（共 %d 个，git status 看全）" % len(changed)
    return ("## ⚠️ 续跑：上次被中断，接着做完、绝不从头重来\n"
            "上次实施被**超时/崩溃中断**（本步第 %d 次基建重试）。**工作区已有未提交的部分进展**，改动文件：\n"
            "%s%s\n"
            "**先 `git status` / `git diff` 看你上次做到哪**，然后**在已有进展上接着做完**——绝不删掉重写、绝不从零重来。\n"
            % (n, flist, more))


def _driver_ctx(state_dir, mid, mode):
    """driver prompt 的 ctx：键名严格对齐 driver.md 占位（F1：拼错会静默 no-op）。"""
    return {
        "project_path": os.path.dirname(os.path.abspath(state_dir)),
        "state_dir": os.path.abspath(state_dir),
        "carry_forward": "(loop tick — 见 events.jsonl / notes.md)",
        "mode": mode,
        "resume_context": _resume_note(state_dir, mid, mode),   # P0 超时续跑（非重试时为空）
    }


def _phase_plan(state_dir, m, opts):
    """plan：render driver(plan-only) → 跑 driver → advance-phase（plan→plan_review）。"""
    mid = m["id"]
    prompt = prompts.render(m, "driver", _driver_ctx(state_dir, mid, "plan-only"))
    if opts["dry_run"]:
        print("[dry-run] phase=plan → driver(mode=plan-only) → advance-phase (plan→plan_review)")
        return 0
    (rc, _reason), _ = _timed(state_dir, mid, "plan", "driver",
                              lambda: invoke_driver(resolve_driver_cmd(opts["driver_cmd"], phase="plan"),
                                                    prompt, state_dir, mid,
                                                    "plan-only", opts["driver_timeout"],
                                                    stuck_timeout=opts.get("driver_stuck_timeout",
                                                                           DEFAULT_DRIVER_STUCK_TIMEOUT)))
    if rc == RC_INFRA:
        return infra_retry(state_dir, mid, "driver(plan) infra: %s" % _reason,
                           opts["max_infra_retries"], "plan")
    _reset_infra(state_dir, mid)
    # 减返工：driver 在出方案阶段就发现要偏离 spec / 有重大存疑 → 此时举旗（写 flag.json），
    # impl 前先让人确认，省掉"写完整步 impl 才发现走偏再推倒"的返工（2026-06-24）。
    if _detect_and_raise_flag(state_dir, mid):
        return 0   # 已标 NEEDS_CONFIRM + 推进 cursor，跳过门1/impl、非阻塞等人确认
    return _run_state(state_dir, "advance-phase", mid, phase="plan")


def _phase_plan_review(state_dir, m, opts):
    """plan_review：review(plan_review) → APPROVE→gate-pass plan / REVISE→gate-fail plan / 3→infra。"""
    mid = m["id"]
    if opts["dry_run"]:
        print("[dry-run] phase=plan_review → review(plan_review) → "
              "APPROVE→gate-pass plan | REVISE→gate-fail plan | ERROR→infra_retry")
        return 0
    (res, rexit), _ = _timed(
        state_dir, mid, "plan_review", "review",
        lambda: _review(state_dir, mid, "plan_review", opts))
    if rexit == 0:  # APPROVE / APPROVE_WITH_CONDITIONS
        _reset_infra(state_dir, mid)
        _maybe_carry(state_dir, mid, res, "plan_review")
        return _run_state(state_dir, "gate-pass", mid, gate="plan", phase="plan_review")
    if rexit == 1:  # REVISE → reopen-plan（不烧 attempt）
        _reset_infra(state_dir, mid)
        return _run_state(state_dir, "gate-fail", mid, gate="plan",
                          error="门1 REVISE", phase="plan_review")
    if rexit == 3:  # ERROR/降级/未配置 → 第二维
        return infra_retry(state_dir, mid, "judge(plan_review) degraded: %s" % res.get("reason"),
                           opts["max_infra_retries"], "plan_review")
    return 2  # rexit 2: usage → loop 用法错


def _phase_impl(state_dir, m, opts):
    """impl：render driver(implement) → 跑 driver → advance-phase（impl→impl_review）。"""
    mid = m["id"]
    prompt = prompts.render(m, "driver", _driver_ctx(state_dir, mid, "implement"))
    if opts["dry_run"]:
        print("[dry-run] phase=impl → driver(mode=implement) → advance-phase (impl→impl_review)")
        return 0
    # A 簇：实施前记 git baseline（HEAD），实施后据它机器捕获本步改动文件。
    _proj = os.path.dirname(os.path.abspath(state_dir))
    _brc, _bout, _ = _git(_proj, "rev-parse", "HEAD")
    baseline = _bout.strip() if _brc == 0 and _bout.strip() else None
    (rc, _reason), _ = _timed(state_dir, mid, "impl", "driver",
                              lambda: invoke_driver(resolve_driver_cmd(opts["driver_cmd"], phase="impl"),
                                                    prompt, state_dir, mid,
                                                    "implement", opts["driver_timeout"],
                                                    stuck_timeout=opts.get("driver_stuck_timeout",
                                                                           DEFAULT_DRIVER_STUCK_TIMEOUT)))
    if rc == RC_INFRA:
        return infra_retry(state_dir, mid, "driver(impl) infra: %s" % _reason,
                           opts["max_infra_retries"], "impl")
    _reset_infra(state_dir, mid)
    _capture_changed_files(state_dir, mid, baseline)  # A 簇：机器捕获"改了哪些文件"
    if _detect_and_raise_flag(state_dir, mid):        # D 簇：driver 举旗了？
        return 0  # 已标 NEEDS_CONFIRM + 推进 cursor，跳过 impl_review、非阻塞往后跑、等人确认
    return _run_state(state_dir, "advance-phase", mid, phase="impl")


def _phase_impl_review(state_dir, m, opts):
    """impl_review：verify(if probe_cmd; FAIL→gate-fail impl 先否决) THEN review → PASS→gate-pass impl /
    FAIL→gate-fail impl(默认) 或 reopen-plan(逃生口) / 3→infra。"""
    mid = m["id"]
    probe = resolve_probe(m)
    if opts["dry_run"]:
        msg = "[dry-run] phase=impl_review → "
        msg += ("verify(probe='%s') THEN " % probe) if probe else "(no probe_cmd → skip verify) "
        msg += "review(impl_review) → PASS→gate-pass impl | FAIL→gate-fail impl | ERROR→infra"
        print(msg)
        return 0

    # 1) 确定性闸 verify（先于 review，能独立否决；反作弊根）
    if probe:
        (vres, vexit), _ = _timed(
            state_dir, mid, "impl_review", "verify",
            lambda: _verify(state_dir, mid, probe, opts))
        if vexit == 1:  # 探针真没过（按真实退出码）→ gate-fail impl（烧 attempt），judge 不被调
            return _run_state(state_dir, "gate-fail", mid, gate="impl",
                              error="probe FAIL: %s" % vres.get("reason"), phase="impl_review")
        if vexit == 2:  # probe 配置坏（空/路径错）→ 视作 infra
            return infra_retry(state_dir, mid, "probe usage error", opts["max_infra_retries"],
                               "impl_review")
        # vexit == 0 (PASS) → 继续调 judge
        _reset_infra(state_dir, mid)
    else:
        state.append_event(state_dir, "verify_skipped", milestone=mid, phase="impl_review",
                           reason="no acceptance.probe_cmd → review-only")

    # 2) AI 判官 review
    (res, rexit), _ = _timed(
        state_dir, mid, "impl_review", "review",
        lambda: _review(state_dir, mid, "impl_review", opts))
    if rexit == 0:  # PASS / PASS_WITH_NITS → gate-pass impl（= complete）
        _reset_infra(state_dir, mid)
        _maybe_carry(state_dir, mid, res, "impl_review")
        return _run_state(state_dir, "gate-pass", mid, gate="impl", phase="impl_review")
    if rexit == 1:  # FAIL
        _reset_infra(state_dir, mid)
        if _is_reopen_plan(res):  # 逃生口=方案错 → reopen-plan（不烧 attempt，但走软上限）
            if _bump_replan(state_dir, mid, opts["max_replans"], "impl_review"):
                return _escalate_replan(state_dir, mid, opts["max_replans"],
                                        "judge 反复判 REOPEN_PLAN（疑似 livelock）")
            return _run_state(state_dir, "reopen-plan", mid, error="门2 逃生口：方案本身需重开",
                              phase="impl_review")
        return _run_state(state_dir, "gate-fail", mid, gate="impl",
                          error="门2 FAIL：实现需返工", phase="impl_review")
    if rexit == 3:  # ERROR/降级 → 第二维
        return infra_retry(state_dir, mid, "judge(impl_review) degraded: %s" % res.get("reason"),
                           opts["max_infra_retries"], "impl_review")
    return 2


# ---- verify / review 调用（返回 (result_dict, exit_code)）--------------------

def _verify(state_dir, mid, probe, opts):
    """跑 verify.verify（确定性，按真实退出码裁定），返回 (result, exit 0|1|2)。"""
    res = verify.verify(state_dir, mid, probe, name="impl_probe",
                        timeout=opts["probe_timeout"], use_shell=True)
    exit_code = 0 if res["verdict"] == "PASS" else 1
    return res, exit_code


def _review(state_dir, mid, kind, opts):
    """跑 review（可配置判官），返回 (result, exit 0|1|3) 按 review._exit_code_for。

    #10b：plan_review 且配了 ≥2 人 panel（LONGHAUL_PLAN_PANEL）→ 走多 agent panel 聚合；
    否则走 #10a 的单审 judge（向后兼容）。
    """
    panel = opts.get("plan_panel") or []
    if kind == "plan_review" and len(panel) >= 2:
        res = review.review_panel(state_dir, mid, kind, judge_cmds=panel,
                                  ctx={"mode": "plan-only"}, timeout=opts["review_timeout"])
    else:
        res = review.review(state_dir, mid, kind=kind, judge_cmd=opts["judge_cmd"],
                            ctx={"mode": "plan-only" if kind == "plan_review" else "implement"},
                            timeout=opts["review_timeout"])
    return res, review._exit_code_for(res)


def _is_reopen_plan(res):
    """门2 逃生口判定：review.parsed 含 reopen_plan:true，或 raw 含 REOPEN_PLAN 标记。

    parsed=None（judge 只吐 VERDICT 块、无 JSON）时看 raw 文本标记；都无 → 走默认（回 impl）。
    """
    parsed = res.get("parsed")
    if isinstance(parsed, dict) and parsed.get("reopen_plan") in (True, "true", "True"):
        return True
    raw = res.get("raw") or ""
    return "REOPEN_PLAN" in raw


def _maybe_carry(state_dir, mid, res, kind):
    """PASS_WITH_NITS / APPROVE_WITH_CONDITIONS → 把 nit/condition 落 notes.md carry-forward。"""
    v = res.get("verdict")
    if v not in ("PASS_WITH_NITS", "APPROVE_WITH_CONDITIONS"):
        return
    note_path = os.path.join(state_dir, "notes.md")
    try:
        with open(note_path, "a", encoding="utf-8") as f:
            f.write("\n## carry-forward（%s · %s · %s）\n> %s\n"
                    % (mid, kind, v, (res.get("raw") or "")[:500]))
    except OSError:
        pass
    state.append_event(state_dir, "carry_forward", milestone=mid, kind=kind, verdict=v)


# ---- F6 干预 inbox（intervention inbox · AC6 · DESIGN §2.6）-----------------
# 投递口只认文件（Layer-1）：人/绑定往 state_dir/inbox/ 原子写一个 json（一文件一消息）。
# loop 每 tick 开头（守卫之后、派活之前）消费：sorted(投递时间序) → apply（幂等）→ 原子归档。
# 效果落地仍走 state.py 既有动词（reopen-plan 仅 review 相位）/ save_milestones；
# pause/abort 是 loop 私有循环控制位（同 infra_blocked/replan_count，不属 milestone 状态机）。

def _inbox_dir(state_dir):
    return os.path.join(state_dir, INBOX_DIR)


def safe_load_json(path):
    """读一个 inbox 消息文件；解析失败 / 非 dict → None（调用方据此 quarantine，绝不 crash tick）。"""
    try:
        with open(path, encoding="utf-8") as f:
            obj = json.load(f)
        return obj if isinstance(obj, dict) else None
    except (OSError, ValueError):
        return None


def _archive(path, dest_dir):
    """原子归档（os.rename）到 processed/ 或 rejected/；同名重投加随机后缀，绝不覆盖审计。"""
    os.makedirs(dest_dir, exist_ok=True)
    base = os.path.basename(path)
    target = os.path.join(dest_dir, base)
    if os.path.exists(target):
        stem, ext = os.path.splitext(base)
        fd, target = tempfile.mkstemp(prefix=stem + "-", suffix=ext, dir=dest_dir)
        os.close(fd)
    os.rename(path, target)
    return target


def _append_note(m, msg_id, text, ts=None):
    """把一条结构化 note append 进 milestone 的 `note`（list of {id,ts,text}）。

    幂等：已含该 msg_id 则跳过（崩溃在 apply→archive 之间重放无双效，§3.0）。
    """
    notes = m.get("note")
    if not isinstance(notes, list):
        notes = []
    if any(isinstance(n, dict) and n.get("id") == msg_id for n in notes):
        m["note"] = notes
        return False
    notes.append({"id": msg_id, "ts": ts or state._now(), "text": text})
    m["note"] = notes
    return True


def _msg_id(msg, fallback):
    mid = msg.get("id")
    return mid if (mid and str(mid).strip()) else fallback


# ---- redirect 的 reopen 走 replan 软上限（§3.3：洪水不能 livelock）----------

def _redirect_reopen(state_dir, mid, opts, phase):
    """仅在 phase ∈ {plan_review, impl_review} 调：复用 F5 软上限账，撞上限升级（返回 4）。

    返回 0 = 已 reopen（未超上限）；4 = 撞 max_replans → _escalate_replan 升级人工。
    """
    if _bump_replan(state_dir, mid, opts["max_replans"], phase):
        return _escalate_replan(state_dir, mid, opts["max_replans"],
                                "redirect 洪水触发 reopen 软上限")
    return _run_state(state_dir, "reopen-plan", mid,
                      error="人工 redirect：换做法重开方案", phase=phase)


# ---- 每个 kind 的 apply（幂等；返回 'paused'|'abort'|'escalate'|None）-------

def _apply_pause(state_dir, msg, opts):
    cur = _cursor(state_dir)
    cur["paused"] = True
    _save_cursor(state_dir, cur)
    return "paused"


def _apply_resume(state_dir, msg, opts):
    cur = _cursor(state_dir)
    cur["paused"] = False
    _save_cursor(state_dir, cur)
    return None


def _apply_abort(state_dir, msg, opts):
    cur = _cursor(state_dir)
    cur["aborted"] = True
    cur["next_action"] = "人工 abort（via inbox）—— 循环已停止，等人处理"
    _save_cursor(state_dir, cur)
    return "abort"


def _apply_redirect(state_dir, msg, opts):
    """某 milestone 换做法：append 结构化 note + 按 (status,phase) 路由（仅 review 相位 reopen）。

    返回 'escalate'（撞软上限升级）或 None。redirect 不存在的 milestone → 抛 KeyError（上层 quarantine）。
    """
    mid = msg.get("milestone")
    instruction = msg.get("instruction") or ""
    if not mid:
        raise ValueError("redirect missing 'milestone'")
    ms = state.load_milestones(state_dir)
    m = state._find(ms, mid)  # 找不到 → KeyError → 上层 quarantine
    msg_id = _msg_id(msg, "redirect")
    _append_note(m, msg_id, instruction)
    status, phase = m["status"], m["phase"]
    force = bool(msg.get("force"))

    if status == "DONE":
        if force:
            # 唯一把 DONE 退回处：inbox 显式授权的状态写（DONE/done 相位 reopen 非法 → 走 loop 私有路径）。
            m["status"] = "IN_PROGRESS"
            state._set_phase(m, "plan")
            state.save_milestones(state_dir, ms)
            state.append_event(state_dir, "redirect_force_reopen", milestone=mid, id=msg_id)
            # force 重开也计一次软上限（防 force 洪水）；超则升级。
            if _bump_replan(state_dir, mid, opts["max_replans"], "plan"):
                return "escalate" if _escalate_replan(
                    state_dir, mid, opts["max_replans"], "force-redirect 洪水") == 4 else None
            return None
        state.save_milestones(state_dir, ms)
        cur = _cursor(state_dir)
        cur["next_action"] = "%s 已 DONE，收到 redirect（保守不自动重开；需 force:true 才退回）" % mid
        _save_cursor(state_dir, cur)
        state.append_event(state_dir, "redirect_on_done", milestone=mid, id=msg_id)
        return None

    if status == "BLOCKED":
        # 解熔断活锁：清该 mid 的 infra/replan/reclaim 账给新机会；相位保持 blocked、不 reopen（非法相位）。
        # F7：reclaim_count 也加进"给新机会时清零的账"（plan §4），否则 redirect 解熔断后残留 reclaim 会误升级。
        cur = _cursor(state_dir)
        for k in ("infra_retries", "replan_count", "reclaim_count"):
            d = cur.get(k) or {}
            if mid in d:
                d[mid] = 0
                cur[k] = d
        ib = _infra_blocked(cur)
        if mid in ib:
            ib.remove(mid)
            cur["infra_blocked"] = ib
        _save_cursor(state_dir, cur)
        state.save_milestones(state_dir, ms)
        state.append_event(state_dir, "redirect_unblock", milestone=mid, id=msg_id)
        return None

    if status == "NEEDS_CONFIRM":
        # 🔧 redirect 打到"正等你确认举旗"的步 = 不接受举旗、改用这个做法 → 视同 reject+换方向：
        # 回 plan、带上面已 append 的 redirect note、清旗重做。修复"redirect 被静默吞、永不到 driver"
        # （NEEDS_CONFIRM 永远不被 _next_todo 选中 → note 渲染不进 prompt）（2026-06-23 review）。
        m["status"] = "IN_PROGRESS"
        state._set_phase(m, "plan")
        m["last_error"] = "人 redirect 覆盖举旗：回原方案带新指示重做"
        m.pop("flag", None)
        state.save_milestones(state_dir, ms)
        cur = _cursor(state_dir)
        cur["active_milestone"] = mid
        cur["phase"] = "build"
        cur["next_action"] = "%s redirect 覆盖举旗，回 plan 带新指示重做" % mid
        _save_cursor(state_dir, cur)
        state.append_event(state_dir, "redirect_on_needs_confirm", milestone=mid, id=msg_id)
        return None

    # IN_PROGRESS / TODO：先把 note 落盘（不论后续是否 reopen）。
    state.save_milestones(state_dir, ms)
    if phase in ("plan_review", "impl_review"):
        rc = _redirect_reopen(state_dir, mid, opts, phase)
        return "escalate" if rc == 4 else None
    # plan / impl / TODO 等非 review 相位：只 append note，driver 下次跑到该相位自然吸收。
    state.append_event(state_dir, "redirect_note", milestone=mid, phase=phase, id=msg_id)
    return None


def _apply_respec(state_dir, msg, opts):
    """改需求（MVP）：append spec.md 决策日志 + respec_requested 事件 + cursor.respec_pending（非阻塞）。"""
    instruction = msg.get("instruction") or ""
    msg_id = _msg_id(msg, "respec")
    spec_path = os.path.join(state_dir, state.RUN_FILES["spec"])
    try:
        existing = open(spec_path, encoding="utf-8").read() if os.path.exists(spec_path) else ""
    except OSError:
        existing = ""
    marker = "## 干预记录（inbox respec）"
    block = "" if marker in existing else "\n%s\n" % marker
    if ("[respec %s]" % msg_id) not in existing:   # 带 id 去重（崩溃重放无双效）
        block += "- [respec %s @%s] %s\n" % (msg_id, state._now(), instruction)
    if block:
        state._atomic_write(spec_path, existing + block)
    cur = _cursor(state_dir)
    cur["respec_pending"] = True
    _save_cursor(state_dir, cur)
    state.append_event(state_dir, "respec_requested", id=msg_id, instruction=instruction[:200])

    # 可选：affects:[...] 列出的 milestone 各 append 一条 note（同 redirect 链路到 driver；不改相位）。
    affects = msg.get("affects") or []
    if affects:
        ms = state.load_milestones(state_dir)
        changed = False
        for mid in affects:
            try:
                m = state._find(ms, mid)
            except KeyError:
                continue
            if _append_note(m, msg_id, "受 respec 影响，下次出方案请复核 spec 决策日志：%s" % instruction):
                changed = True
        if changed:
            state.save_milestones(state_dir, ms)
    return None


# ---- D 簇：人对 driver 举旗(NEEDS_CONFIRM)的异步回插（薄封装，转移逻辑都在 state.py）----

def _apply_resolve(state_dir, msg, opts):
    """场景1：人已解决 driver 举旗的阻塞 → state.resolve（回 impl 带提示重跑）。"""
    mid = msg.get("milestone")
    if not mid:
        raise ValueError("resolve missing 'milestone'")
    argv = ["resolve", state_dir, mid]
    if msg.get("instruction"):
        argv += ["--instruction", msg["instruction"]]
    state.main(argv)
    return None


def _apply_confirm(state_dir, msg, opts):
    """场景2接受：人确认 driver 的偏离方案 OK → state.confirm（→DONE 推进）。"""
    mid = msg.get("milestone")
    if not mid:
        raise ValueError("confirm missing 'milestone'")
    state.main(["confirm", state_dir, mid])
    return None


def _apply_reject(state_dir, msg, opts):
    """场景2驳回：人不接受偏离 → state.reject（回 plan 按原方案重做）。"""
    mid = msg.get("milestone")
    if not mid:
        raise ValueError("reject missing 'milestone'")
    argv = ["reject", state_dir, mid]
    if msg.get("instruction"):
        argv += ["--instruction", msg["instruction"]]
    state.main(argv)
    return None


def _apply_split(state_dir, msg, opts):
    """item11 举旗式拆分：人确认拆分 → state.split（把太大的 milestone 拆成子步、继续跑）。"""
    mid = msg.get("milestone")
    into = msg.get("into")
    if not mid or not into:
        raise ValueError("split missing 'milestone' or 'into'")
    state.main(["split", state_dir, mid, "--into", into])
    return None


_APPLY = {
    "pause": _apply_pause,
    "resume": _apply_resume,
    "abort": _apply_abort,
    "redirect": _apply_redirect,
    "respec": _apply_respec,
    "resolve": _apply_resolve,
    "confirm": _apply_confirm,
    "reject": _apply_reject,
    "split": _apply_split,
}


def apply_message(state_dir, msg, opts):
    """按 kind 分派到 _apply_*；返回循环控制信号 'paused'|'abort'|'escalate'|None。"""
    return _APPLY[msg["kind"]](state_dir, msg, opts)


def consume_inbox(state_dir, opts):
    """消费 state_dir/inbox/ 的全部待处理消息（守卫之后、派活之前调）。

    返回循环控制信号：None（无/正常） | 'paused' | 'abort' | 'escalate'(redirect 撞软上限→tick 退 4)。
    沿文件名时间序逐条 apply → 原子归档（先 apply 再 archive + 每 kind 幂等 → 崩溃至多重放一条无害）。
    inbox/ 不存在 = 没人投过 = 零成本跳过（向后兼容：无 inbox/ 的 .longhaul 行为完全不变）。
    """
    # abort 是终态：一旦 cursor.aborted，后续每 tick 开头直接停（不再消费/派活）。
    if _cursor(state_dir).get("aborted"):
        return "abort"
    inbox = _inbox_dir(state_dir)
    if not os.path.isdir(inbox):
        return None
    processed = os.path.join(inbox, "processed")
    rejected = os.path.join(inbox, "rejected")
    ctl = None
    for fn in sorted(os.listdir(inbox)):
        path = os.path.join(inbox, fn)
        if fn.startswith(".") or not fn.endswith(".json") or not os.path.isfile(path):
            continue  # 跳子目录 / 半写 .tmp / 非 json
        msg = safe_load_json(path)
        if msg is None or msg.get("kind") not in KINDS:
            _archive(path, rejected)
            state.append_event(state_dir, "inbox_rejected", file=fn)
            continue
        try:
            sig = apply_message(state_dir, msg, opts)
        except Exception as e:  # noqa: BLE001  一条坏消息绝不 crash 整个 tick
            _archive(path, rejected)
            state.append_event(state_dir, "inbox_error", file=fn, err=str(e))
            continue
        _archive(path, processed)  # ⭐ 先 apply 再 archive（崩溃至多重放一条，配合幂等无害）
        state.append_event(state_dir, "inbox_consumed",
                           kind=msg["kind"], id=_msg_id(msg, fn), file=fn)
        if sig == "abort":
            ctl = "abort"
            break                  # abort 终态：停止消费后续
        if sig == "escalate":
            ctl = "escalate"
            break                  # redirect 撞软上限：立即止血，本 tick 升级、不再派活
        # pause/resume 由 _apply_pause/_apply_resume 直接改 cursor.paused（持久态）；
        # 最终 paused 信号在循环后按 cursor 派生，不在此累积（见下）。
    if ctl in ("abort", "escalate"):
        return ctl
    # 🔒 paused 是持久态（与 aborted 对称），不是一次性信号：每拍都按 cursor.paused 早退，直到 resume
    # 清掉。修复"pause 只挡收到 pause 那一拍、下一拍无新消息就偷偷恢复派活"（2026-06-23 review）。
    return "paused" if _cursor(state_dir).get("paused") else None


# ---- inbox 投递口（Layer-1 helper：原子写一个文件，**不消费**）-------------

def _drop_message(state_dir, kind, _name=None, **fields):
    """往 state_dir/inbox/ 原子投递一条消息（写 .tmp → os.rename，同目录 rename 原子）。

    人/脚本/外部绑定的投递口；只写文件、不触发消费（消费只在 tick）。返回写入的文件名。
    测试与生产共用此路径（保证测的就是真投递逻辑）。
    """
    inbox = _inbox_dir(state_dir)
    os.makedirs(inbox, exist_ok=True)
    if _name is None:
        ts = state._now().replace("-", "").replace(":", "")  # 20260623T091500Z
        rand = os.urandom(2).hex()
        _name = "%s-%s-%s.json" % (ts, kind, rand)
    msg = {"kind": kind}
    msg.update({k: v for k, v in fields.items() if v is not None})
    msg.setdefault("id", os.path.splitext(_name)[0])
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=inbox)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(msg, f, ensure_ascii=False)
    os.rename(tmp, os.path.join(inbox, _name))
    return _name


# ---- tick（核心）-----------------------------------------------------------

_DISPATCH = {
    "plan": _phase_plan,
    "plan_review": _phase_plan_review,
    "impl": _phase_impl,
    "impl_review": _phase_impl_review,
}


def tick(state_dir, opts):
    """一个 tick：捡起当前活 → claim(幂等) → 按 milestone.phase（真相源，重读）派一相位 → 退出。"""
    if not os.path.isdir(state_dir):
        print("error: state_dir not found: %s" % state_dir, file=sys.stderr)
        return 2
    if not os.path.exists(os.path.join(state_dir, "milestones.json")):
        print("error: no milestones.json in %s" % state_dir, file=sys.stderr)
        return 2

    state.append_event(state_dir, "tick_start", state_dir=os.path.abspath(state_dir))

    # ---- F6：守卫通过后才消费 inbox（在两个 tick 之间生效，绝不打断在跑的 step）----
    ctl = consume_inbox(state_dir, opts)
    if ctl == "abort":
        state.append_event(state_dir, "run_aborted", reason="human abort via inbox")
        print("ABORT: human abort via inbox; stopping loop", file=sys.stderr)
        return ABORT_EXIT                                  # = 5（人工 abort 专码）
    if ctl == "escalate":
        state.append_event(state_dir, "tick_end", duration_ms=0, rc=4)
        return 4                                           # redirect 撞软上限：升级人工
    if ctl == "paused":
        state.append_event(state_dir, "tick_paused")
        print("tick: paused (via inbox); no-op")
        return 0                                           # 本 tick no-op，稳定态退 0

    # ---- F8 P0 硬门（§1.5 必停门之一）：放行 build 派活之前必须人确认 P0 清零 ----
    # 放在 inbox 消费之后（abort/pause/respec 仍可被吸收）、派 build 活之前——拒绝把 build 派给 driver。
    # 向后兼容：state.is_p0_confirmed 对「已进 build / 已起步」的旧 run 默认放行（不破坏 已有项目）。
    if not state.is_p0_confirmed(state_dir):
        state.append_event(state_dir, "p0_gate_block", reason="P0 not confirmed; awaiting human")
        print("P0 NOT CONFIRMED: refusing to dispatch build work; "
              "run `python3 engine/state.py p0-confirm %s` after human P0 review" % state_dir,
              file=sys.stderr)
        return P0_GATE_EXIT                                # = 6，loop.sh/cron 据此停在 P0 门

    # ---- F7 看门狗 TTL sweep（inbox 消费 + 守卫之后、派活之前）----
    # paused/abort 已在 consume_inbox 早退、根本到不了这里；sweep 内再显式短路（belt-and-suspenders）。
    if sweep_stale_lease(state_dir, opts) == "escalate":   # 撞 max_reclaims → 升级人工
        state.append_event(state_dir, "tick_end", duration_ms=0, rc=4)
        return 4

    t0 = time.monotonic()
    rc = _tick_body(state_dir, opts)
    state.append_event(state_dir, "tick_end", duration_ms=int((time.monotonic() - t0) * 1000), rc=rc)
    return rc


def _tick_body(state_dir, opts):
    milestones = state.load_milestones(state_dir)
    cur = _cursor(state_dir)
    blocked_set = set(_infra_blocked(cur))

    # 1) 确定性捡起当前活（程序计数器）；过滤掉 infra/replan 熔断的 milestone（防活锁）。
    nxt = state._next_todo(milestones)
    while nxt is not None and nxt["id"] in blocked_set:
        # 临时把它当 SKIPPED 跳过（只在内存里跳，不写状态）——找下一个真正 actionable 的。
        rest = [m for m in milestones if m["id"] != nxt["id"]]
        nxt = state._next_todo(rest)
    if nxt is None:
        state.append_event(state_dir, "tick_idle", reason="no actionable (all DONE/BLOCKED)")
        print("tick: no actionable milestone (all DONE/BLOCKED/infra-blocked)")
        return 0
    mid = nxt["id"]

    # 2) claim（幂等）：TODO→IN_PROGRESS+phase=plan；已 IN_PROGRESS→只刷 cursor（phase/attempt 不动）。
    if not opts["dry_run"]:
        claim_rc = _run_state(state_dir, "claim", mid, phase="?")
        if claim_rc == 3:  # claim 时已耗尽 → 已 BLOCKED（产品熔断）
            return 3

    # 3) 重读该 milestone 的当前 phase（真相源），按 phase 分派。
    m = state._find(state.load_milestones(state_dir), mid)
    phase = m["phase"]
    fn = _DISPATCH.get(phase)
    if fn is None:
        state.append_event(state_dir, "loop_error", milestone=mid, phase=phase,
                           reason="non-dispatchable phase落到 loop")
        print("error: non-dispatchable phase %r for milestone %s" % (phase, mid), file=sys.stderr)
        return 2

    # 4) F7：派活前取 lease、`finally` 释放（无论推进/重试/熔断都释放——正常完成必清 lease）。
    #    dry-run 不派真活、无崩溃可言 → 不取 lease（保 --dry-run 纯只读语义）。
    if opts["dry_run"]:
        return fn(state_dir, m, opts)
    acquire_lease(state_dir, mid, phase, opts)
    try:
        return fn(state_dir, m, opts)
    finally:
        release_lease(state_dir, mid)


# ---- CLI --------------------------------------------------------------------

def _opts_from_args(args):
    return {
        "driver_cmd": getattr(args, "driver_cmd", None),  # 原始显式；分阶段解析在调用点(resolve_driver_cmd phase=)
        "judge_cmd": getattr(args, "judge_cmd", None),  # review.resolve_judge_cmd 处理 env/默认/分阶段(kind=)
        "plan_panel": resolve_plan_panel(),  # #10b：plan 多 agent panel（≥2 个才生效）
        "driver_timeout": getattr(args, "driver_timeout", DEFAULT_DRIVER_TIMEOUT),
        "driver_stuck_timeout": getattr(args, "driver_stuck_timeout", DEFAULT_DRIVER_STUCK_TIMEOUT),
        "probe_timeout": getattr(args, "probe_timeout", DEFAULT_PROBE_TIMEOUT),
        "review_timeout": getattr(args, "review_timeout", DEFAULT_REVIEW_TIMEOUT),
        "max_infra_retries": getattr(args, "max_infra_retries", DEFAULT_MAX_INFRA_RETRIES),
        "max_replans": getattr(args, "max_replans", DEFAULT_MAX_REPLANS),
        "max_reclaims": getattr(args, "max_reclaims", DEFAULT_MAX_RECLAIMS),  # F7 第三维软上限
        "lease_ttl": resolve_lease_ttl(getattr(args, "lease_ttl", None)),     # F7 lease TTL（env 兜底）
        "dry_run": getattr(args, "dry_run", False),
    }


def cmd_tick(args):
    return tick(args.state_dir, _opts_from_args(args))


def cmd_inbox(args):
    """投递一条 inbox 消息（Layer-1 投递口：人/脚本/外部绑定调）。只写文件、不消费。"""
    if args.kind not in KINDS:
        print("error: kind must be one of %s" % (KINDS,), file=sys.stderr)
        return 2
    if not os.path.isdir(args.state_dir):
        print("error: state_dir not found: %s" % args.state_dir, file=sys.stderr)
        return 2
    fn = _drop_message(args.state_dir, args.kind,
                       milestone=getattr(args, "milestone", None),
                       instruction=getattr(args, "instruction", None),
                       into=getattr(args, "into", None),
                       force=True if getattr(args, "force", False) else None)
    print("dropped inbox message: %s/%s/%s" % (args.state_dir, INBOX_DIR, fn))
    return 0


def cmd_status(args):
    """status [--next-json]。

    **--next-json 必须委托 state.py `next`（cmd_next，emits {"state":"done"|"blocked"|"actionable"...}）**
    —— loop.sh 收口守卫 grep 的就是 next 的格式（REQUIRED gate-1）。不带 --next-json 时走 state.py status。
    """
    if getattr(args, "next_json", False):
        return state.main(["next", args.state_dir])  # 1:1 委托 cmd_next，不是 cmd_status
    return state.main(["status", args.state_dir])


def main(argv=None):
    ap = argparse.ArgumentParser(prog="loop.py",
                                 description="longhaul-builder 确定性自驱 tick（一个 tick 推一相位）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("tick", help="跑恰好一个相位的推进然后退出")
    t.add_argument("state_dir")
    t.add_argument("--driver-cmd", default=None,
                   help="driver 命令模板（{prompt_file}/{state_dir}/{milestone_id}/{mode}）；"
                        "优先级 > $LONGHAUL_DRIVER_CMD > 空哨兵默认")
    t.add_argument("--judge-cmd", default=None, help="judge 命令模板（透传 review.py）")
    t.add_argument("--driver-timeout", type=int, default=DEFAULT_DRIVER_TIMEOUT,
                   help="driver 兜底天花板秒（进度感知后这是上限，非硬墙）")
    t.add_argument("--driver-stuck-timeout", type=int, default=DEFAULT_DRIVER_STUCK_TIMEOUT,
                   help="driver 卡死窗秒：既无文件改动也无输出多久判死（慢但在产出不杀）")
    t.add_argument("--probe-timeout", type=int, default=DEFAULT_PROBE_TIMEOUT)
    t.add_argument("--review-timeout", type=int, default=DEFAULT_REVIEW_TIMEOUT)
    t.add_argument("--max-infra-retries", type=int, default=DEFAULT_MAX_INFRA_RETRIES)
    t.add_argument("--max-replans", type=int, default=DEFAULT_MAX_REPLANS)
    t.add_argument("--max-reclaims", type=int, default=DEFAULT_MAX_RECLAIMS,
                   help="F7 看门狗第三维软上限：可复现崩溃被 reclaim 几次后升级人工（默认 3）")
    t.add_argument("--lease-ttl", type=int, default=None,
                   help="F7 lease TTL（秒）；优先级 > $LONGHAUL_LEASE_TTL > 默认 %d" % DEFAULT_LEASE_TTL)
    t.add_argument("--dry-run", action="store_true",
                   help="只打印将派的命令 + 将调的 gate 动词，不真跑 driver / 不改状态")
    t.set_defaults(fn=cmd_tick)

    s = sub.add_parser("status", help="薄封装 state.py（--next-json 委托 next 供 loop.sh 收口守卫）")
    s.add_argument("state_dir")
    s.add_argument("--next-json", dest="next_json", action="store_true",
                   help="委托 state.py next：输出 {\"state\":\"done|blocked|actionable\"...}")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("inbox", help="投递一条干预 inbox 消息（只写文件、不消费；消费在 tick）")
    s.add_argument("state_dir")
    s.add_argument("kind", choices=KINDS, help="pause|resume|abort|redirect|respec|resolve|confirm|reject|split")
    s.add_argument("--milestone", default=None, help="目标 milestone id（redirect/resolve/confirm/reject/split 用）")
    s.add_argument("--instruction", default=None, help="一句话指示（redirect/respec/resolve/reject 用）")
    s.add_argument("--into", default=None, help="拆分子目标，分号分隔（split 用），如 '后端骨架;前端页面'")
    s.add_argument("--force", action="store_true", help="redirect on DONE 时强制退回 plan")
    s.set_defaults(fn=cmd_inbox)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
