#!/usr/bin/env bash
# 坏 driver stub（基建故障）：直接非零退出（127=命令不存在的经典码），不写任何文件 →
# loop 视作 INFRA_FAIL，走第二维 infra_retry（attempt_count 绝不动）。测基建熔断。
# 用法：stub_driver_broken.sh <mode> <state_dir> <milestone_id> <project_dir>
echo "stub_driver_broken: simulated infra failure (exit 127)" >&2
exit 127
