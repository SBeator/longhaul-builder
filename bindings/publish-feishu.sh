#!/usr/bin/env bash
# bindings/publish-feishu.sh —— 把一条迭代的运行报告发成飞书云文档（markdown 正文 + 交互甘特 HTML Box）。
#
# 关键（2026-06-24 用户提的根因）：同一条迭代再次发布要**原地更新同一篇**、不要每次新建——否则用户
# 手上的链接永远是旧的。给了 existing_doc_token 就 `docs +update --command overwrite`（删旧内容塞最新、
# token/链接不变）；没给才 `docs +create` 建新。两种都把交互甘特刷成文末的妙笔 HTML Box。
#
# 能力层（reportdoc/gantt，agent 无关）与发布绑定分离；没装 lark-cli / 没妙笔脚本都 graceful 降级。
# 用法：bash publish-feishu.sh <state_dir> [existing_doc_token]   # 打印最终 doc URL
set -u
SD="${1:?usage: publish-feishu.sh <state_dir> [existing_doc_token]}"
TOK="${2:-}"
[ "${LONGHAUL_NO_PUBLISH:-}" = "1" ] && { echo "(LONGHAUL_NO_PUBLISH=1，跳过飞书发布)"; exit 0; }
[ -d "$SD" ] || { echo "(state_dir 不存在，跳过飞书发布)"; exit 0; }
command -v lark-cli >/dev/null 2>&1 || { echo "(未装 lark-cli，跳过飞书发布；md/html 已在 docs/iterations/)"; exit 0; }
ENGD="$(cd "$(dirname "$0")/../engine" && pwd)"

# 1. 生成飞书版 md（耗时段指向文末甘特、去掉本地占位/details）
MD="$(mktemp).md"; DIR="$(dirname "$MD")"; BASE="$(basename "$MD")"
python3 "$ENGD/reportdoc.py" "$SD" --stamp "$(date +%Y-%m-%d)" --flavor feishu > "$MD" 2>/dev/null \
  || { echo "(报告生成失败，跳过飞书发布)"; rm -f "$MD"; exit 0; }

# 2. 有 token → 原地覆盖（删旧内容、链接不变）；没 token → 新建
if [ -n "$TOK" ]; then
  ( cd "$DIR" && lark-cli docs +update --doc "$TOK" --command overwrite --doc-format markdown --content "@$BASE" --as user >/dev/null 2>&1 ) || true
  # overwrite 只换正文、不会设文档标题（只 docs +create 会从首个 # 取）→ 否则标题变 Untitled。
  # 单独 PATCH Page 块（block_id == document_id）把标题设成与正文 H1 一致。
  TITLE="运行报告 — $(python3 -c 'import sys;sys.path.insert(0,sys.argv[1]);import reportdoc;print(reportdoc._one_liner(sys.argv[2]))' "$ENGD" "$SD" 2>/dev/null)"
  TBODY="$(python3 -c 'import json,sys;print(json.dumps({"update_text_elements":{"elements":[{"text_run":{"content":sys.argv[1]}}]}}))' "$TITLE" 2>/dev/null)"
  [ -n "$TBODY" ] && lark-cli api PATCH "/open-apis/docx/v1/documents/$TOK/blocks/$TOK" --data "$TBODY" --as user >/dev/null 2>&1 || true
  URL="https://bytedance.larkoffice.com/docx/$TOK"
else
  RESP="$( cd "$DIR" && lark-cli docs +create --doc-format markdown --content "@$BASE" --as user 2>/dev/null )"
  read -r TOK URL <<EOF2
$(printf '%s' "$RESP" | python3 -c 'import sys,json
try:
    d=json.load(sys.stdin).get("data",{}).get("document",{}); print(d.get("document_id",""), d.get("url",""))
except Exception:
    print("", "")' 2>/dev/null)
EOF2
fi
rm -f "$MD"
[ -n "${URL:-}" ] && [ -n "${TOK:-}" ] || { echo "(飞书发布失败，md/html 已在本地)"; exit 0; }

# 3. 交互甘特刷成文末妙笔 HTML Box（overwrite 已清旧 box / 新文档本就无 box → 直接追加一个最新的）
MJS="$HOME/.claude/skills/lark-html-box/scripts/create_magic_doc.mjs"
if [ -f "$MJS" ] && command -v node >/dev/null 2>&1; then
  GH="$(mktemp).html"
  if python3 "$ENGD/gantt.py" "$SD" --title "本轮运行流水（交互甘特）" > "$GH" 2>/dev/null && grep -q lhg-canvas "$GH"; then
    node "$MJS" --html "$GH" --doc-token "$TOK" --as user >/dev/null 2>&1 || true
  fi
  rm -f "$GH"
fi
echo "$URL"
