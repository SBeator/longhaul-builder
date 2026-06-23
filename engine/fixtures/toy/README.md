# toy fixture — loop.py 集成验收靶子（零 LLM、零网络）

`engine/test_loop.py` 把本目录的 stub copy 到 tempdir 再跑，**绝不在原件上跑/改**，也绝不指向 LIVE `.longhaul`。

- `seed_milestones.json` — 1 个可满足(T1) + 1 个不可满足(T2) 的机械 milestone（带可执行 `acceptance.probe_cmd`）。
- `drivers/stub_driver.sh` `<mode> <state_dir> <mid> <project>` — plan-only 覆盖式写 plan.md；implement touch `t1_done.txt` + 写 green。**覆盖式写**保 crash-resume 幂等。
- `drivers/stub_driver_lazy.sh` — implement 故意不造目标文件 → probe 永 FAIL（测确定性闸真挡）。
- `drivers/stub_driver_broken.sh` — exit 127（基建故障）→ 测 infra 第二维熔断。
- `judges/stub_judge_pass.sh` `<prompt_file>` — 按 prompt 里 kind（PLAN/IMPL REVIEW）输出 APPROVE / PASS。
- `judges/stub_judge_revise.sh` — 门1 REVISE（测 reopen-plan 不烧 attempt）。
- `judges/stub_judge_reopen.sh` — 门2 FAIL + `REOPEN_PLAN` 逃生口（测 reopen-plan 软上限 / livelock 防护）。
- `judges/stub_judge_garbage.sh` — 无 VERDICT 块 → review 降级 ERROR(3) → 测 infra 熔断。

driver/judge 全是 shell 脚本（P0-2 命令是 shell 模板；A2 stub 不烧 LLM；A3 自带 fixture 不联网）。
