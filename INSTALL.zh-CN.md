[English](./INSTALL.md) · **中文**

# 安装与使用 —— 在一台新机器上用 longhaul-builder 跑长任务

一句话：把这套装上后，**你只跟 AI 说话**（直接用 Claude Code，或把 agent 接到一个聊天入口（IM bot）），AI 自己把"老化需求→拆 milestone→无人值守自驱→报告→验收"全跑了，你不碰命令行。

## 1. 前置（这台机器要有）
- 至少一个 agent CLI，已登录（driver/judge 用它 headless 跑）：
  - **claude CLI** —— `claude --version` 出来、`echo hi | claude -p` 能回；和/或
  - **codex CLI** —— `codex --version` 出来、`echo hi | codex exec` 能回。
  - （想用"异构互审"——执行 Claude、审查 Codex——就两个都装。）
- **git** / **python3**（纯标准库，无三方依赖）。
- **cron** 或愿意开个前台 while 循环（长任务自驱靠它每几分钟敲一拍）。
- 可选：一个聊天入口（把 agent 接到你的 IM），想从聊天里用、或想自驱时主动推通知再配。

## 2. 安装（一次，一条命令——仓本身就是 skill）
```bash
# 直接把仓 clone 进 Claude 的 skills 目录：clone 完即装好（SKILL.md + engine + bindings + lhb 全在里面）
git clone https://github.com/SBeator/longhaul-builder ~/.claude/skills/longhaul
export PATH="$HOME/.claude/skills/longhaul/bin:$PATH"     # 让 lhb 上 PATH（写进 ~/.bashrc 持久化）
```
就这样。任意 Claude agent 现在**自带 longhaul 能力**（skill 已就位）；`lhb` 命令也可用。
更新：`cd ~/.claude/skills/longhaul && git pull`。（想从聊天里用：把这台机器的 agent 接到你的 IM 即可，流程一样。）

## 3. 用法

### 用法 A（最省事，推荐）：只跟 AI 说话
在 Claude Code（或你接好的聊天入口里 @ 这个 agent）说：
> 「我要用 longhaul 做个新项目：<介绍背景、要做成什么>」

AI（带上 `longhaul` skill）会：① 跟你来回把**需求聊清**（这是你唯一要深度参与的地方）→ ② 自动建 spec、拆 milestone → ③ 让你确认 **P0** → ④ 挂上自驱、走开 → ⑤ 中途要澄清/卡住来找你、随时可丢话干预 → ⑥ 全做完把成果+证据给你**最终验收**。
人只在 **2 个必停点**出现：开头定需求（P0）、结尾验收。

### 用法 B（你想自己跑底层命令）
```bash
lhb new    myproj "做一个 XXX"         # 建项目 + spec 骨架（再把 .longhaul/spec.md 填实）
lhb plan   myproj                      # spec → milestones（朴素骨架，按"可独立验收单元"重拆）
lhb agents myproj --driver claude --judge codex   # 定执行者/审查者角色（持久化）
lhb confirm myproj                     # ★P0：确认放行
lhb run    myproj --watch              # 前台自驱到 done/blocked（或 --cron 打印 crontab 行后走开）
lhb status myproj ; lhb timeline myproj   # 看进度 / 执行流水(时间+耗时)
lhb say    myproj redirect --milestone M2 --instruction "换个做法…"   # 中途干预
```

## 4. 多 agent：执行者 / 审查者（Claude + Codex）
支持 **Claude** 和 **Codex** 两个 agent，一个当**执行者(driver)**、一个当**审查者(judge)**，谁是谁随你定（也可同一个）。
```bash
lhb agents myproj --driver claude --judge codex   # 推荐：执行 Claude、审查 Codex（异构互审）
lhb agents myproj --driver codex  --judge claude   # 或反过来
```
- **首次定好就持久化**（写进 `myproj/.longhaul/agents.env`），整个项目之后一直用这套，**cron 也读它**——不用每次再定。
- 缺省（没 `lhb agents` 过）：**执行 Claude + 审查 Codex**（异构互审；codex 没装则审查退回 claude）。
- 换模型：`LONGHAUL_CLAUDE_MODEL` / `LONGHAUL_CODEX_MODEL`。临时换某次：直接覆盖 `LONGHAUL_DRIVER_CMD`/`LONGHAUL_JUDGE_CMD`。
- 分阶段配不同 agent（#10a）：在通用槽之上按阶段覆盖——`LONGHAUL_DRIVER_CMD__plan` / `__impl`、`LONGHAUL_JUDGE_CMD__plan_review` / `__impl_review`（分阶段槽最优先，没配回落通用槽，向后兼容）。
- 自驱时在 done/blocked 主动推通知：`export LONGHAUL_NOTIFY_CMD="bash <root>/bindings/notify.sh {event} {message} {state_dir}"`（在 notify.sh 里接你自己的渠道：webhook / 自定义发送脚本 / 默认写 notify.log）。

## 5. 心智模型（为什么这么设计）
- 常驻的只有最便宜的两样：**cron 心跳 + 磁盘文件**；AI 从不常驻，只"起来干一小步就死"。状态全在 `.longhaul/`，**崩了能续、换 agent 不重来**。
- 每个 milestone 走**两道门**：先出方案→独立判官审方案，再实现→独立判官审实现（+`verify.py` 真跑探针按退出码裁定）。**测试全绿≠通过**，方案/代码不合理一样打回。
- 卡住有**熔断**（超重试上限标 BLOCKED 喊人，不无限烧）。
- 单一事实源：[DESIGN.md](./DESIGN.md)。
