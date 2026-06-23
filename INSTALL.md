**English** · [中文](./INSTALL.zh-CN.md)

# Install & Use — Running long-haul tasks with longhaul-builder on a new machine

In one line: once this is installed, **you just talk to the AI** (directly in Claude Code, or wire the agent up to a chat entry point (IM bot)), and the AI runs the whole thing itself — "soak the requirements → break into milestones → unattended self-driving → report → acceptance" — without you ever touching the command line.

## 1. Prerequisites (what this machine needs)
- At least one agent CLI, already logged in (the driver/judge run it headless):
  - **claude CLI** — `claude --version` works and `echo hi | claude -p` replies; and/or
  - **codex CLI** — `codex --version` works and `echo hi | codex exec` replies.
  - (Want "heterogeneous cross-review" — Claude executes, Codex reviews — install both.)
- **git** / **python3** (pure standard library, no third-party dependencies).
- **cron**, or willingness to run a foreground `while` loop (long-haul self-driving relies on it ticking every few minutes).
- Optional: a chat entry point (wire the agent up to your IM) — configure it only if you want to drive it from chat, or want it to proactively push notifications while self-driving.

## 2. Install (once, a single command — the repo itself is the skill)
```bash
# Clone the repo straight into Claude's skills directory: it's installed the moment the clone finishes (SKILL.md + engine + bindings + lhb are all inside)
git clone https://github.com/SBeator/longhaul-builder ~/.claude/skills/longhaul
export PATH="$HOME/.claude/skills/longhaul/bin:$PATH"     # put lhb on PATH (add to ~/.bashrc to persist)
```
That's it. Any Claude agent now **has the longhaul capability built in** (the skill is in place); the `lhb` command is available too.
Update: `cd ~/.claude/skills/longhaul && git pull`. (Want to drive it from chat: just wire this machine's agent up to your IM — same flow.)

## 3. Usage

### Usage A (least effort, recommended): just talk to the AI
In Claude Code (or @ this agent in the chat entry point you've wired up), say:
> "I want to build a new project with longhaul: <describe the background, what you want built>"

The AI (with the `longhaul` skill loaded) will: ① go back and forth with you to **nail down the requirements** (this is the only place you need to be deeply involved) → ② automatically create the spec and break it into milestones → ③ ask you to confirm **P0** → ④ hook up self-driving and walk away → ⑤ come back to you when it needs clarification or gets stuck, and you can drop in instructions to intervene anytime → ⑥ when everything's done, hand you the results + evidence for **final acceptance**.
A human only shows up at **2 mandatory stops**: setting requirements at the start (P0), and acceptance at the end.

### Usage B (you want to run the low-level commands yourself)
```bash
lhb new    myproj "build an XXX"        # create project + spec skeleton (then flesh out .longhaul/spec.md)
lhb plan   myproj                       # spec → milestones (plain skeleton; re-split by "independently acceptable unit")
lhb agents myproj --driver claude --judge codex   # set the executor/reviewer roles (persisted)
lhb confirm myproj                      # ★P0: confirm to release
lhb run    myproj --watch               # self-drive in the foreground until done/blocked (or --cron prints the crontab line, then walk away)
lhb status myproj ; lhb timeline myproj   # check progress / execution log (timestamps + durations)
lhb say    myproj redirect --milestone M2 --instruction "try a different approach…"   # mid-flight intervention
```

## 4. Multi-agent: executor / reviewer (Claude + Codex)
Supports two agents, **Claude** and **Codex** — one acts as the **executor (driver)**, the other as the **reviewer (judge)**; which is which is up to you (they can also be the same one).
```bash
lhb agents myproj --driver claude --judge codex   # recommended: Claude executes, Codex reviews (heterogeneous cross-review)
lhb agents myproj --driver codex  --judge claude   # or the other way around
```
- **Set once and it's persisted** (written to `myproj/.longhaul/agents.env`); the whole project sticks with this setup afterward, and **cron reads it too** — no need to set it again each time.
- Default (if you never ran `lhb agents`): **Claude executes + Codex reviews** (heterogeneous cross-review; if codex isn't installed, review falls back to claude).
- Switch models: `LONGHAUL_CLAUDE_MODEL` / `LONGHAUL_CODEX_MODEL`. To override just one run: override `LONGHAUL_DRIVER_CMD`/`LONGHAUL_JUDGE_CMD` directly.
- Proactively push notifications on done/blocked while self-driving: `export LONGHAUL_NOTIFY_CMD="bash <root>/bindings/notify.sh {event} {message} {state_dir}"` (wire your own channel inside notify.sh: webhook / a custom send script / or it writes to notify.log by default).

## 5. Mental model (why it's designed this way)
- The only thing kept running is the two cheapest things: a **cron heartbeat + files on disk**; the AI never stays resident, it just "wakes up, does one small step, and dies". All state lives in `.longhaul/`, so it **resumes after a crash and doesn't start over when you swap agents**.
- Every milestone goes through **two gates**: first produce a plan → an independent judge reviews the plan, then implement → an independent judge reviews the implementation (plus `verify.py` runs real probes and rules by exit code). **All tests green ≠ pass** — an unreasonable plan or code gets bounced just the same.
- Getting stuck has a **circuit breaker** (exceed the retry limit → mark BLOCKED and call a human, instead of burning forever).
- Single source of truth: [DESIGN.md](./DESIGN.md).
