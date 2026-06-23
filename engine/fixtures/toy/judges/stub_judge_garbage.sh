#!/usr/bin/env bash
# 畸形 judge stub（基建故障）：打印无 VERDICT 块的乱码 → review.py 解析失败降级 ERROR(exit 3)
# → loop 走第二维 infra_retry（不烧 attempt）。测 judge 一直抖时的 infra 熔断。
echo "blah blah no verdict here just noise $RANDOM"
exit 0
