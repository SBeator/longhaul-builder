#!/usr/bin/env bash
# bindings/publish-feishu.sh —— 把运行报告发成飞书云文档（markdown 正文 + 交互甘特 HTML Box）。
#
# 能力层（reportdoc.py 产 md/html、gantt.py 产交互甘特）与发布绑定**分离**：没装 lark-cli 也能用，
# 只是不发飞书。装了就：
#   1) lark-cli docs +create 从 md 建飞书文档（标题来自 md 首个 # 标题）；
#   2) 装了妙笔脚本(lark-html-box) + 有甘特数据 → 把交互甘特作为「妙笔 HTML Box」追加到文末，
#      这就是「耗时」段在飞书里的对应呈现（hover 看每步详情、横向缩放）。
# best-effort：任何一步失败都不影响构建（md/html 已落在 docs/iterations/<迭代>/）。
#
# 用法：bash publish-feishu.sh <report.md> [state_dir]   # 给了 state_dir 才追加交互甘特
set -u
MD="${1:?usage: publish-feishu.sh <report.md> [state_dir]}"
SD="${2:-}"
[ "${LONGHAUL_NO_PUBLISH:-}" = "1" ] && { echo "(LONGHAUL_NO_PUBLISH=1，跳过飞书发布)"; exit 0; }
[ -f "$MD" ] || { echo "(报告文件不存在，跳过飞书发布)"; exit 0; }
command -v lark-cli >/dev/null 2>&1 || { echo "(未装 lark-cli，跳过飞书发布；md/html 已在 docs/iterations/)"; exit 0; }

DIR="$(cd "$(dirname "$MD")" && pwd)"; BASE="$(basename "$MD")"
# 1. 从 markdown 建飞书文档（@file 只收 cwd 下相对路径，cd 进去）
RESP="$( cd "$DIR" && lark-cli docs +create --doc-format markdown --content "@$BASE" --as user 2>/dev/null )"
read -r TOKEN URL <<EOF2
$(printf '%s' "$RESP" | python3 -c 'import sys,json
try:
    d=json.load(sys.stdin).get("data",{}).get("document",{})
    print(d.get("document_id",""), d.get("url",""))
except Exception:
    print("", "")' 2>/dev/null)
EOF2
[ -n "${URL:-}" ] || { echo "(飞书建文档失败，md/html 已在本地)"; exit 0; }

# 2. 交互甘特作为 HTML Box 追加到文末（飞书里的"耗时"呈现）
MJS="$HOME/.claude/skills/lark-html-box/scripts/create_magic_doc.mjs"
ENGD="$(cd "$(dirname "$0")/../engine" && pwd)"
if [ -n "$SD" ] && [ -n "${TOKEN:-}" ] && [ -f "$MJS" ] && command -v node >/dev/null 2>&1; then
  GH="$(mktemp).html"
  if python3 "$ENGD/gantt.py" "$SD" --title "本轮运行流水（交互甘特）" > "$GH" 2>/dev/null && grep -q lhg-canvas "$GH"; then
    node "$MJS" --html "$GH" --doc-token "$TOKEN" --as user >/dev/null 2>&1 || true
  fi
  rm -f "$GH"
fi
echo "$URL"
