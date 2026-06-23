---
name: longhaul
description: 当用户想让 AI **自主、长期、无人值守地把一个项目从头做完**时用——不用记 skill 名，下面这些自然说法任一就触发：「帮我自主实现这个项目」「帮我自动实现 X」「自主把 X 做出来」「让 AI 自己把 X 做完」「帮我长期/端到端做个 X」「无人值守做个项目」「帮我把这个需求做成项目」「用 longhaul/起个长跑任务」。它把一句话/一段背景做成无人值守长跑构建：跟用户聊清需求(P0)→建 spec、拆 milestone→挂真 Claude(执行)+Codex(审查)自驱循环（出方案→独立判官审方案→TDD红绿→跑证据→独立判官审实现）→看进度/接澄清/最后人验收。和"一次性写段代码/直接帮我实现个函数"不同：它是**长时间自己推进、崩了能续、卡住熔断**的长跑构建，适合中到大、要分多步、跑很久的项目。
---

# longhaul —— 让"你说话 → agent 驱动整套自主长跑构建"

这个 skill 让**一个 Claude agent**（就是你）把 longhaul-builder 这套能力开起来：人只跟你说话，**所有脚本你来跑**，人不碰命令行。能力层（`engine/`）是 agent 无关的纯机器；你是那层"薄绑定"——把人话翻译成对 `lhb` / `engine` 的调用。

## 这套东西在干嘛（一句话）
一句话需求 → 老化成冻结 spec → 拆成一串可独立验收的 milestone → **cron/循环每几分钟敲一拍哑脚本**：每拍起一个**短命的真 Claude** 干一小步（出方案→审方案→TDD实现→跑证据→独立判官审），过了推进、不过重试、卡住熔断。状态全在磁盘文件（`.longhaul/`），崩了能续、换 agent 不重来。人只在 **2 个必停点**出现：① P0（开头把需求定清）② 最终验收；中途随时可丢一句话干预。

## 前置
- `lhb` 在 PATH（或知道 skill 根目录 `<root>/bin/lhb`）。`<root>` = 本 skill 目录（仓即 skill）。
- 至少一个 agent CLI 已登录：`claude`（headless `-p`）和/或 `codex`（headless `codex exec`）。driver/judge 各用一个。
- `git` / `python3`（纯标准库）/ `cron`(或前台 while)。
- 如果把这个 agent 接到了某个聊天入口（IM bot），人就能从聊天里跟你说话——流程完全一样。

## 你的 playbook（人只说话，你执行）

### 1. 老化 / 把需求聊清（★第 1 个必停：P0）—— 这是你唯一要和人深聊的地方
- 人给你一句话 + 一段背景。你**苏格拉底式追问**，直到需求可测可量：要做成什么、给谁用、什么算"做完了"（验收标准 + 每条怎么验=验收探针）、范围里有什么/没什么、有哪些可逆假设你替他拍了。
- 先 `lhb new <项目目录> "<一句话>"` 生成 spec 骨架（标准章节都在 `<dir>/.longhaul/spec.md`）。
- 然后**和人一起把 spec.md 填实**（你写、人确认）：User Stories / Acceptance Criteria（可测）/ 验收探针 / 成熟度门 P0/P1/P2 / Assumptions。把 `[NEEDS CLARIFICATION]` 一条条问掉。
- 关键：**别急着往下**。一直聊到人说"需求定了"。这是质量的地基，错在这里后面全返工。

### 2. 拆 milestone（★拆解是你 agent 的判断活，别照搬 plan.py）
- `lhb plan <dir>` 只给个**朴素起点**：它机械地 **1 条 Acceptance Criterion → 1 个 milestone**，几乎总会**过度拆分**（比如"一个函数 + 测试"的 spec 有 7 条 AC，它就吐 7 个 milestone——但合理只该是 2 个：函数、测试）。
- **所以默认你要重新拆**：把它合并/重排成「**可独立验收的工作单元**」——一个 milestone = 一块能单独写测试、单独跑探针、单独验收的东西（典型：核心算法 / API / 存储 / 前端 / 测试套件…），不是"每条验收标准一个"。每条 milestone 必须带**可执行探针** `acceptance.probe_cmd`（自驱时 `verify.py` 真跑它、按真实退出码裁定，堵 AI"嘴上说过了"），不要 NL 探针。
- 跟人对一下你的拆解，再写回 `milestones.json`。**改完务必重跑** `python3 <root>/engine/state.py set-milestones <dir>/.longhaul --file <dir>/.longhaul/milestones.json`，让台账与文件、计数一致（别只手改文件——否则审计日志里 milestone 数会和实际对不上，像这次出现过 milestones_set=7 但实际 2 的不一致）。

### 3. P0 硬门确认（★必停）
- 人点头需求 + 拆解都 OK → `lhb confirm <dir>`。**在此之前自驱不会动**（loop 拒绝 build，退出码 6）。

### 3.5 定执行者/审查者角色（★首次定好，整个项目一直用）
- 支持两个 agent：**Claude** 和 **Codex**。一个当**执行者(driver，干活)**、一个当**审查者(judge，独立审)**，谁是谁随你/人定。
- `lhb agents <dir> --driver <claude|codex> --judge <claude|codex>`。**缺省 = 执行 Claude + 审查 Codex**（异构互审；codex 没装则审查退回 claude）。
- 推荐：**执行=Claude、审查=Codex**（异构互审更挑得出问题）；也可反过来，或同一个。
- 它写进 `<dir>/.longhaul/agents.env` 持久化——之后 `lhb run` 和 cron 都用这套，**不用每次再定**。首次问一下人偏好（或按推荐拍），定了就别中途换。

### 4. 挂上自驱（之后基本不用人）
- 短任务/想盯着看：`lhb run <dir> --watch`（前台 while，一拍拍跑到 done/blocked；**每完成一个 milestone 按第 5 步发进度报告**）。
- 长任务/真无人值守：`lhb run <dir> --cron` 打印一行 crontab，加进去就走；人离开，cron 自己跑（用持久化的角色）。
- driver/judge 用的就是 3.5 定的角色（`bindings/{claude,codex}-*.sh`）。临时换某次：覆盖 `LONGHAUL_DRIVER_CMD/JUDGE_CMD`。

### 5. 进度报告（★强制：每完成一个 milestone 就发一份，别只在最后报）
**每当一个 milestone 跑完（或卡住），主动给人发一份进度报告**——别闷头跑到最后才说。格式（人话、结构化）：
- **已完成/未开始的 milestone**：每个一句话**简报**带过。
- **当前刚完成的这个 milestone**：**详述**两块：
  - **(a) 做了什么**：这一步的项目背景 / 需求 / 方案 / 实现/改了哪些文件 / 测试与判官结论。
  - **(b) 时间与耗时**：这一步**几点开始、各阶段耗时**——直接 `lhb timeline <dir> --milestone <当前M>` 拿（它从 events.jsonl 渲染：出方案/审方案/实施/审实施 各自的开始时间+耗时）。
- 全部做完时，再用 `lhb timeline <dir>` 给一份**完整流水时间线**。
- ⚠️ 别只发"M1 进行中 / 做完了"这种高层卡片——**(a) 做了什么 + (b) 时间耗时** 这两块必须有，否则人看不到它到底做了啥、花了多久。
- 接了通知渠道（设了 `LONGHAUL_NOTIFY_CMD`）的话，循环在 done/blocked 会自动播报兜底。

看状态：`lhb status <dir>`。流水：`lhb timeline <dir>`。
- 人中途想改方向/暂停：`lhb say <dir> redirect --milestone <M> --instruction "..."`（或 pause/resume/abort/respec）。**下一拍自动吸收，不打断在跑的步骤**。
- 卡住（BLOCKED=熔断或要澄清）：把原因 + `lhb timeline` 的耗时一起告诉人，等人给方向 → `lhb say ... respec/redirect` 喂回去 → 继续。

### 6. 最终验收（★第 2 个必停）
- 全部 DONE → **别自己判"完成"**。把成果 + 证据（`.longhaul/evidence/`：每步 red/green、探针输出、判官裁定）整理给人，请人最终验收。人点头才算交付。

## 几条要守住的
- **人只在 2 处必停**（P0 / 最终验收）；其余它自己跑、人想插话随时插。别在中途反复卡人。
- **证据优先**：声称某步过了，靠的是 `verify.py` 真跑探针的退出码 + 独立判官看证据，不是 driver 自述。你转述时也据实，别替它吹"通过了"。
- **状态在文件、agent 短命**：你（编排的 agent）也可以随时下线，cron + `.longhaul/` 让它接着跑；别把自己当常驻监工。
- **凌晨/无人时**：别抛需要人答的问题阻塞；卡住就标 BLOCKED + 记原因，留给人白天处理。

## 命令速查
```
lhb new    <dir> "<一句话>"                          # 建项目 + spec 骨架
lhb plan   <dir>                                     # spec → milestones（朴素骨架，你要重拆）
lhb agents <dir> [--driver claude|codex] [--judge claude|codex]   # ★定执行者/审查者角色(持久化)
lhb confirm <dir> [--by 谁]                          # ★P0 放行
lhb run    <dir> [--watch|--cron|--once]             # 自驱
lhb status <dir>                                     # 看进度
lhb timeline <dir> [--milestone M]                   # 执行流水（每阶段 时间+耗时）—— 进度报告用
lhb say    <dir> <pause|resume|abort|redirect|respec> [--milestone M] [--instruction "..."]
```
设计/为什么这么做的单一事实源：`<root>/DESIGN.md`。
