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
- 然后**和人一起把 spec.md 填实**（你写、人确认）：User Stories / **设计 / 架构** / Acceptance Criteria（可测）/ 验收探针 / 成熟度门 P0/P1/P2 / Assumptions。把 `[NEEDS CLARIFICATION]` 一条条问掉。
- **★把"设计/架构"冻进 spec（ai-cockpit 复盘补的关键）**：和人确认整体设计时，**把确认过的设计稿/架构图、视觉/形态意图、以及关键取舍（哪些原生做深、哪些先 iframe 嵌）都写进 `spec.md` 的「## 设计 / 架构」节**——别让这些只停在聊天里（否则 loop 只对文字 spec 建、成品会偏离设计）。尤其"半数模块是 iframe 嵌旧站"这类取舍，必须当面跟人讲清、写进 spec，别让人以为全原生。门2 reviewer 会据这节核对成品是否忠于设计。
- 关键：**别急着往下**。一直聊到人说"需求定了"。这是质量的地基，错在这里后面全返工。

### 2. 拆 milestone（★拆解是你 agent 的判断活，别照搬 plan.py）
- `lhb plan <dir>` 只给个**朴素起点**：它机械地 **1 条 Acceptance Criterion → 1 个 milestone**，几乎总会**过度拆分**（比如"一个函数 + 测试"的 spec 有 7 条 AC，它就吐 7 个 milestone——但合理只该是 2 个：函数、测试）。
- **所以默认你要重新拆**：把它合并/重排成「**可独立验收的工作单元**」——一个 milestone = 一块能单独写测试、单独跑探针、单独验收的东西（典型：核心算法 / API / 存储 / 前端 / 测试套件…），不是"每条验收标准一个"。每条 milestone 必须带**可执行探针** `acceptance.probe_cmd`（自驱时 `verify.py` 真跑它、按真实退出码裁定，堵 AI"嘴上说过了"），不要 NL 探针。
- **★前端/UI 类 milestone 必须定成 `acceptance.type: "web-e2e"` + 配真浏览器探针**：`probe_cmd` 跑 playwright 之类（出真实退出码 + **把截图存进 `evidence/<M>/`**），可用 `<root>/bindings/e2e-playwright.sh` 模板。**别用"静态断言 HTML/JS 文本 + 打 API"冒充 UI 验收**——那样浏览器里渲染对不对、按钮点了动不动根本没验（ai-cockpit 踩过：12 个 UI milestone 全 tdd、零真浏览器）。`lhb confirm` 放行前会兜底校验：web 项目若一个 E2E 门都没有，直接挡住。**🔒 防假绿**：探针别只 `wait_for_selector`（空壳/占位/"加载中"也满足 ＝ 假绿）——要断言**真内容渲染**（`e2e-playwright.sh` 已默认查非空内容 + 占位标记，列表类设 `LONGHAUL_E2E_MIN_COUNT` 逼真渲染条目数）。**🔒 防覆盖不全**：集成/smoke 探针要覆盖**全部**端点/路由、或显式报覆盖率，别只验易测子集（如只验零参 GET）蒙混成"全过"。门2 judge 会盯这两条。
- 跟人对一下你的拆解，再写回 `milestones.json`。**改完务必重跑** `python3 <root>/engine/state.py set-milestones <dir>/.longhaul --file <dir>/.longhaul/milestones.json`，让台账与文件、计数一致（别只手改文件——否则审计日志里 milestone 数会和实际对不上，像这次出现过 milestones_set=7 但实际 2 的不一致）。
- **★末尾留一个「集成 + 全局观感对齐」milestone**：不只是功能 e2e——还要**对照 spec 的「设计/架构」节过一遍整体观感/画风一致性、出对比截图、独立判一次**（防"每步只顾自己那块、整体涌现式偏离设计稿"，正是 ai-cockpit 的病）。

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
**每当一个 milestone 跑完（或卡住/举旗），主动给人发一份进度报告**——别闷头跑到最后才说。
- **报告内容不要你手写、不要凭印象**：直接跑 `lhb report <dir> --milestone <当前M>`，把它的**输出原样转发**给人。它从 `.longhaul/evidence/` 把这些**机器渲染**好了：① 其他 milestone 简报 ② 当前 milestone 详述（目标/验收类型 · 方案摘要 · **改了哪些文件**(loop git diff 真捕获的) · 测试红→绿(真 EXIT_CODE) · 门1+门2 判官裁定+理由 · 探针 exit+sha · **附图清单** · 各阶段耗时）。
- **附图（★尤其 E2E 截图）**：跑 `lhb report <dir> --images <当前M>` 拿到该步的图片证据路径，发报告时**把这些图附上**（用你接入的 IM 的附图能力——多数 IM 机器人都有发图/上传图片的接口或 `--images` 之类参数）。纯后端、没截图的步就不附。
- 全部做完时，`lhb report <dir>` 给整体 + `lhb timeline <dir>` 给完整流水时间线。
- ⚠️ **绝不允许**发"M1 做完了 / 见 evidence/"这种没内容的卡片——报告正文一律走 `lhb report` 的真实渲染（它就是为了堵这个塌方而存在的）。
- 接了通知渠道（设了 `LONGHAUL_NOTIFY_CMD`）的话，循环在 done/blocked 会自动播报兜底（也可把 `lhb report --images` 的路径经 `LONGHAUL_NOTIFY_IMAGES` 交给通知绑定附图）。

看状态：`lhb status <dir>`。流水：`lhb timeline <dir>`。

### 5.5 干预与举旗（★全程人只跟你说话，命令你来跑）
人**从不直接跑 lhb**——人在 IM 里用大白话说，你（收到消息的 agent）识别意图、调对应 `lhb say` 命令；下一拍自动吸收、不打断在跑的步骤。

**A. driver 举旗（降级/偏离）怎么流转给人**：driver 不得不降级、或发现更优但偏离 spec 的方案时，会写 `evidence/<M>/flag.json`；循环把该 milestone 标 **NEEDS_CONFIRM** 并**非阻塞继续往后跑**（不硬停）。`lhb run` 每拍会**自动把新举旗播报给人**（🚩 + kind + 摘要）。你转达时讲清：哪个 M、kind（blocked-workaround / spec-divergence）、降级/偏离了啥、需要人做什么。

**B. 人的大白话 → 你调的命令**（人不用记 milestone 号——待确认举旗通常唯一，你用 `python3 <root>/engine/state.py flags <dir>` 对上是哪个 M）：

| 人想干什么 | 人怎么说（举例） | 你调的命令 |
| --- | --- | --- |
| 暂停 | 先停一下／别跑了 | `lhb say <dir> pause` |
| 继续 | 继续吧／接着跑 | `lhb say <dir> resume` |
| 终止 | 这个不做了／终止 | `lhb say <dir> abort` |
| 改某步做法 | M2 别用 X，改用 Y | `lhb say <dir> redirect --milestone M2 --instruction "改用 Y"` |
| 改需求/范围 | 需求加一条：还要 Z | `lhb say <dir> respec --instruction "新增 Z"` |
| 〔举旗〕已解决它卡住的问题 | M5 那问题解决了，按…继续 | `lhb say <dir> resolve --milestone M5 --instruction "…"` |
| 〔举旗〕接受它的偏离方案 | M7 那更好的方案可以，继续 | `lhb say <dir> confirm --milestone M7` |
| 〔举旗〕驳回它的偏离 | M7 不行，回原方案 | `lhb say <dir> reject --milestone M7 --instruction "回原方案"` |

- **resolve** 人解决了 driver 举的阻塞 → 该 milestone 回实施、带人的提示重跑｜**confirm** 接受偏离 → 直接 DONE 推进｜**reject** 驳回偏离 → 回出方案、按原 spec 重做。
- **收尾守门**：所有 milestone 跑完但还有没确认的举旗时，循环**不会判 done**——会停在 `needs_confirm`、把待确认清单给人（别让"举了旗没人理"蒙混过关）。
- 卡住（BLOCKED=真熔断超重试上限）：把原因 + `lhb report`/`lhb timeline` 一起告诉人，等人给方向 → `lhb say ... respec/redirect/resolve` 喂回去 → 继续。

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
lhb timeline <dir> [--milestone M]                   # 执行流水（每阶段 时间+耗时）
lhb report <dir> [--milestone M] [--images M]        # ★进度报告：从证据机器渲染(方案/改动/测试/判官/附图/耗时)，原样转发给人
lhb say    <dir> <pause|resume|abort|redirect|respec|resolve|confirm|reject> [--milestone M] [--instruction "..."]
```
设计/为什么这么做的单一事实源：`<root>/DESIGN.md`。
