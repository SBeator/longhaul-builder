#!/usr/bin/env bash
# bindings/e2e-playwright.sh —— web-e2e milestone 的「真浏览器探针」模板（B 簇）。
#
# 为什么有它：UI milestone 不能用"静态断言 HTML/JS 文本 + 打 API"冒充验收——那样浏览器里到底渲染对没、
# 按钮点了动不动根本没验（ai-cockpit 踩过）。web-e2e 的 acceptance.probe_cmd 应指向一个**真开浏览器**的
# 探针：导航到页面 → 等关键元素真出现 → 截图存证 → 按真实退出码裁定。verify.py 只看退出码（0=PASS）。
#
# 用法（写进某个 web-e2e milestone 的 acceptance.probe_cmd，按需改 URL/选择器/截图路径）：
#   LONGHAUL_E2E_URL=http://127.0.0.1:8848 \
#   LONGHAUL_E2E_EXPECT='#board-tab' \
#   LONGHAUL_E2E_SHOT=<state_dir>/evidence/<M>/board.png \
#   bash <root>/bindings/e2e-playwright.sh
# 截图落进 evidence/<M>/ 后，会被 `lhb report --images <M>` 自动列出、随进度报告附给人（A 簇）。
#
# ⚠️ 依赖 playwright，是**按项目接的可选绑定**：只有 web 项目才装它（pip install playwright &&
#    python -m playwright install chromium）——核心框架仍保持零三方依赖、可移植性不破。
set -u
URL="${LONGHAUL_E2E_URL:?需要 LONGHAUL_E2E_URL（被测页面地址）}"
EXPECT="${LONGHAUL_E2E_EXPECT:-body}"          # 等到这个 CSS 选择器在浏览器里真出现才算渲染成功
SHOT="${LONGHAUL_E2E_SHOT:-e2e-screenshot.png}"
TIMEOUT_MS="${LONGHAUL_E2E_TIMEOUT_MS:-15000}"
# 🔒 防假绿（2026-06-24）：不止"选择器存在"——空壳/占位/加载态也会满足 wait_for_selector ＝ 假绿。
# 下面要求匹配元素**真有内容**（非空文本 或 有子元素），且不命中占位/加载态标记。
MIN_COUNT="${LONGHAUL_E2E_MIN_COUNT:-1}"        # 至少 N 个匹配元素有真内容（列表类设大点，逼真渲染条目）
FORBID="${LONGHAUL_E2E_FORBID:-加载中|loading|placeholder|占位|暂无数据|skeleton}"  # 命中文本=占位/加载=假绿
ALLOW_EMPTY="${LONGHAUL_E2E_ALLOW_EMPTY:-}"     # =1 放宽到"只要选择器存在"（极少用，明确知道在干嘛）
mkdir -p "$(dirname "$SHOT")" 2>/dev/null || true

python3 - "$URL" "$EXPECT" "$SHOT" "$TIMEOUT_MS" "$MIN_COUNT" "$FORBID" "$ALLOW_EMPTY" <<'PY'
import sys, re
url, expect, shot, timeout = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
min_count, forbid, allow_empty = int(sys.argv[5]), sys.argv[6], sys.argv[7] == "1"
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("E2E FAIL: playwright 未安装（pip install playwright && python -m playwright install chromium）",
          file=sys.stderr)
    sys.exit(2)
try:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(url, timeout=timeout)
        page.wait_for_selector(expect, timeout=timeout)   # ① 元素真在浏览器里出现
        # ② 🔒 防假绿：匹配元素要有"真内容"（非空文本 或 有子元素），不是空壳/占位/加载态
        els = page.query_selector_all(expect)
        real, placeholder_hit = 0, None
        for el in els:
            txt = (el.inner_text() or "").strip()
            nchild = el.evaluate("e => e.children.length")
            if forbid and txt and re.search(forbid, txt, re.I):
                placeholder_hit = txt[:60]
            if txt or nchild > 0:
                real += 1
        page.screenshot(path=shot, full_page=True)         # 真截图存证（过没过都留证，给 report 附图）
        browser.close()
    if not allow_empty and placeholder_hit:
        print("E2E FAIL（假绿防护）：%s 命中占位/加载态标记 '%s'，疑似没真渲染" % (expect, placeholder_hit),
              file=sys.stderr)
        sys.exit(1)
    if not allow_empty and real < min_count:
        print("E2E FAIL（假绿防护）：%s 匹配 %d 个但只有 %d 个有真内容（非空），< 期望 %d——空壳/占位不算渲染"
              % (expect, len(els), real, min_count), file=sys.stderr)
        sys.exit(1)
    print("E2E PASS：%s 渲染了真内容（%d/%d 非空，min=%d）；截图 -> %s"
          % (expect, real, len(els), min_count, shot))
    sys.exit(0)
except Exception as e:                                      # noqa: BLE001
    print("E2E FAIL: %s" % e, file=sys.stderr)
    sys.exit(1)
PY
