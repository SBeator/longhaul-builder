#!/usr/bin/env bash
# bindings/publish-feishu.sh —— 把一条迭代的运行报告发成飞书云文档（markdown 正文 + 交互甘特 HTML Box）。
#
# 两个用户约束（2026-06-24）：
#  1) **原地更新同一篇**：给了 existing_doc_token 就 `docs +update --command overwrite`（删旧塞新、token/链接不变），
#     没给才 `docs +create`。否则用户手上的链接永远是旧的。
#  2) **交互甘特放进「3 · 耗时」段、不在文末**：做法＝把正文切成「§1-§3 耗时」/「§4 复盘」两段，先写前段→
#     追加甘特 HTML Box（落在 §3 之后）→再 append 后段（§4 落在 box 之后）＝box 正好嵌在耗时段里。免算 index。
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

# 1. 生成飞书版 md，切成「§1-§3 耗时」(PA) / 「§4 复盘」(PB) 两段
FULL="$(mktemp).md"
python3 "$ENGD/reportdoc.py" "$SD" --stamp "$(date +%Y-%m-%d)" --flavor feishu > "$FULL" 2>/dev/null \
  || { echo "(报告生成失败，跳过飞书发布)"; rm -f "$FULL"; exit 0; }
PA="$(mktemp).md"; PB="$(mktemp).md"
python3 - "$FULL" "$PA" "$PB" <<'PYS'
import sys
lines = open(sys.argv[1], encoding="utf-8").read().split("\n")
i = next((k for k, l in enumerate(lines) if l.strip() == "## 4 · 总结与复盘"), len(lines))
open(sys.argv[2], "w", encoding="utf-8").write("\n".join(lines[:i]).rstrip() + "\n")
open(sys.argv[3], "w", encoding="utf-8").write("\n".join(lines[i:]).strip() + "\n")
PYS
DA="$(dirname "$PA")"; BA="$(basename "$PA")"

# 2. 前段：有 token 原地覆盖（删旧塞新、链接不变）；没 token 新建
if [ -n "$TOK" ]; then
  ( cd "$DA" && lark-cli docs +update --doc "$TOK" --command overwrite --doc-format markdown --content "@$BA" --as user >/dev/null 2>&1 ) || true
  # overwrite 不像 create 自动从首个 # 取标题→会变 Untitled；单独 PATCH Page 块(block_id==document_id)设标题。
  TITLE="运行报告 — $(python3 -c 'import sys;sys.path.insert(0,sys.argv[1]);import reportdoc;print(reportdoc._one_liner(sys.argv[2]))' "$ENGD" "$SD" 2>/dev/null)"
  TBODY="$(python3 -c 'import json,sys;print(json.dumps({"update_text_elements":{"elements":[{"text_run":{"content":sys.argv[1]}}]}}))' "$TITLE" 2>/dev/null)"
  [ -n "$TBODY" ] && lark-cli api PATCH "/open-apis/docx/v1/documents/$TOK/blocks/$TOK" --data "$TBODY" --as user >/dev/null 2>&1 || true
  URL="https://bytedance.larkoffice.com/docx/$TOK"
else
  RESP="$( cd "$DA" && lark-cli docs +create --doc-format markdown --content "@$BA" --as user 2>/dev/null )"
  read -r TOK URL <<EOF2
$(printf '%s' "$RESP" | python3 -c 'import sys,json
try:
    d=json.load(sys.stdin).get("data",{}).get("document",{}); print(d.get("document_id",""), d.get("url",""))
except Exception:
    print("", "")' 2>/dev/null)
EOF2
fi
[ -n "${URL:-}" ] && [ -n "${TOK:-}" ] || { echo "(飞书发布失败，md/html 已在本地)"; rm -f "$FULL" "$PA" "$PB"; exit 0; }

# 3. 交互甘特作为妙笔 HTML Box 追加（此刻文末＝§3 耗时之后）
MJS="$HOME/.claude/skills/lark-html-box/scripts/create_magic_doc.mjs"
if [ -f "$MJS" ] && command -v node >/dev/null 2>&1; then
  GH="$(mktemp).html"
  if python3 "$ENGD/gantt.py" "$SD" --title "本轮运行流水（交互甘特）" > "$GH" 2>/dev/null && grep -q lhg-canvas "$GH"; then
    node "$MJS" --html "$GH" --doc-token "$TOK" --as user >/dev/null 2>&1 || true
  fi
  rm -f "$GH"
fi

# 4. 后段 §4 复盘：append 到文末（落在 box 之后）→ 最终顺序 §1 §2 §3+甘特 §4
DB="$(dirname "$PB")"; BB="$(basename "$PB")"
[ -s "$PB" ] && { ( cd "$DB" && lark-cli docs +update --doc "$TOK" --command append --doc-format markdown --content "@$BB" --as user >/dev/null 2>&1 ) || true; }

rm -f "$FULL" "$PA" "$PB"
echo "$URL"
