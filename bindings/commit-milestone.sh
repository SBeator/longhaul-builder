#!/usr/bin/env bash
# bindings/commit-milestone.sh —— 自动 commit：每个刚完成(DONE)的 milestone 提交其代码改动。
#
# 用户预期（2026-06-24）：项目推进时，每个 milestone 的改动应被自动 commit、而不是攒到最后一把梭。
# 做法：每拍 tick 后由 loop.sh 调本绑定；本绑定找出「DONE 但还没提交」的 milestone（靠 .longhaul/
# committed.json 记账），逐个 `git add 全部(除 .longhaul) + commit`，message = "milestone <id>: <标题>"。
# 不含 .longhaul（构建状态自身不进每步代码提交，保持代码历史干净）。
#
# 能力/绑定分离：git 是环境相关 → 放绑定；引擎(loop.sh)只在 tick 成功后调用它。非 git 仓 / 没改动 /
# 没装 git 都 graceful 跳过；best-effort，绝不影响构建。可移植。
# 用法：bash commit-milestone.sh <project_dir> <state_dir>
set -u
PROJ="${1:?usage: commit-milestone.sh <project_dir> <state_dir>}"
SD="${2:?usage: commit-milestone.sh <project_dir> <state_dir>}"
PY="${PYTHON:-python3}"

command -v git >/dev/null 2>&1 || exit 0
( cd "$PROJ" && git rev-parse --git-dir >/dev/null 2>&1 ) || exit 0   # 非 git 仓：跳过

# 列出 DONE 但未记账提交的 milestone（id<TAB>标题）
"$PY" - "$SD" <<'PYIN' | while IFS=$'\t' read -r MID TITLE; do
import json, os, sys
sd = sys.argv[1]
try:
    ms = json.load(open(os.path.join(sd, "milestones.json"), encoding="utf-8"))["milestones"]
except Exception:
    sys.exit(0)
cf = os.path.join(sd, "committed.json")
done = set(json.load(open(cf, encoding="utf-8"))) if os.path.exists(cf) else set()
for m in ms:
    if m.get("status") == "DONE" and m.get("id") not in done:
        t = (m.get("goal", "") or "").replace("\n", " ")
        for sep in ("：", ":"):
            if sep in t:
                t = t.split(sep)[0]
                break
        print("%s\t%s" % (m["id"], t.strip()[:50]))
PYIN
  # 暂存全部代码改动（排除 .longhaul 构建状态），有改动才提交
  git -C "$PROJ" add -A >/dev/null 2>&1 || true
  git -C "$PROJ" reset -q -- "$SD" >/dev/null 2>&1 || true
  git -C "$PROJ" reset -q -- .longhaul >/dev/null 2>&1 || true
  if ! git -C "$PROJ" diff --cached --quiet 2>/dev/null; then
    git -C "$PROJ" commit -q -m "milestone ${MID}: ${TITLE}

由 longhaul 在该 milestone 完成时自动提交。
Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>" >/dev/null 2>&1 \
      && echo "[commit-milestone] committed ${MID}: ${TITLE}" >&2 || true
  fi
  # 记账：无论是否有改动都标记，避免每拍重试同一个已完成 milestone
  "$PY" - "$SD" "$MID" <<'PYIN2' || true
import json, os, sys
sd, mid = sys.argv[1], sys.argv[2]
cf = os.path.join(sd, "committed.json")
lst = json.load(open(cf, encoding="utf-8")) if os.path.exists(cf) else []
if mid not in lst:
    lst.append(mid)
json.dump(lst, open(cf, "w", encoding="utf-8"), ensure_ascii=False)
PYIN2
done
exit 0
