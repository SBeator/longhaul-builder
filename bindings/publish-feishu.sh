#!/usr/bin/env bash
# bindings/publish-feishu.sh —— item10 绑定：把运行报告 md 发成飞书云文档。
#
# 能力层（reportdoc.py 产 md/html）与发布绑定**分离**：没装 lark-cli 也能用，只是不发飞书。
# 装了 lark-cli 就用 `docs +create --doc-format markdown` 从 md 建一篇飞书文档，打印 doc URL。
# best-effort：任何失败都不影响构建（报告 md/html 已落在 docs/iterations/）。
#
# 用法：bash publish-feishu.sh <report.md> "<标题>"
set -u
MD="${1:?usage: publish-feishu.sh <report.md> [title]}"
TITLE="${2:-运行报告}"
[ -f "$MD" ] || { echo "(报告文件不存在，跳过飞书发布)"; exit 0; }
command -v lark-cli >/dev/null 2>&1 || { echo "(未装 lark-cli，跳过飞书发布；md/html 已在 docs/iterations/)"; exit 0; }
# lark-cli 的 @file 只收 cwd 下相对路径——cd 到 md 所在目录再用 basename。
DIR="$(cd "$(dirname "$MD")" && pwd)"; BASE="$(basename "$MD")"
( cd "$DIR" && lark-cli docs +create --title "$TITLE" --doc-format markdown --content "@$BASE" --as user 2>/dev/null ) \
  | grep -oE 'https://[a-zA-Z0-9./_-]*docx[a-zA-Z0-9./_-]*' | head -1 || true
