#!/usr/bin/env bash
# test_portability.sh —— macOS / bash 3.2 可移植性回归测试。
# 守住已修的坑、防新代码再引入：①所有 .sh 语法可解析 ②不用 macOS 缺失/bash4+ 的特性
# ③$变量不紧贴多字节(中文/全角) ④空数组安全展开 ⑤lhb_timeout 真能超时。
# 用法：bash engine/test_portability.sh   退 0=全过。可进 CI（Linux/macOS 都跑）。
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
. "$ROOT/bindings/compat.sh"
PASS=0; FAIL=0
ok(){ echo "  ✓ $1"; PASS=$((PASS+1)); }
no(){ echo "  ✗ $1"; FAIL=$((FAIL+1)); }

# 收集所有 .sh（排除 .git）
SHFILES=$(find "$ROOT" -name '*.sh' -not -path '*/.git/*' | sort)
# 反模式扫描排除本测试文件自身（它含检测用的模式字符串，否则自我误报）；语法检查仍含它
SCANFILES=$(echo "$SHFILES" | grep -v '/test_portability\.sh$')

echo "[1] bash -n 语法解析所有 .sh"
syn=0
for f in $SHFILES; do bash -n "$f" 2>/dev/null || { echo "    语法错: $f"; syn=1; }; done
[ "$syn" = 0 ] && ok "全部 .sh 语法 OK" || no "有 .sh 语法错"

echo "[2] 无裸 timeout（macOS 无；应用 lhb_timeout）"
# 排除：compat.sh(实现处)、注释、lhb_timeout/gtimeout/_TIMEOUT、python(sys.argv)
hits=$(grep -rnE '(^|[^_[:alnum:]])timeout ' $SCANFILES 2>/dev/null \
  | grep -vE 'lhb_timeout|gtimeout|_TIMEOUT|sys\.argv|/compat\.sh:' \
  | grep -vE ':[0-9]+:[[:space:]]*#')
[ -z "$hits" ] && ok "无裸 timeout" || { no "发现裸 timeout:"; echo "$hits" | sed 's/^/      /'; }

echo "[3] 无 bash4+ 专有语法（macOS 默认 bash 3.2 无）"
hits=$(grep -rnE 'declare -A|[^_]mapfile |[^_]readarray |\$\{[A-Za-z_][A-Za-z_0-9]*(\^\^|,,)' $SCANFILES 2>/dev/null \
  | grep -vE ':[0-9]+:[[:space:]]*#')
[ -z "$hits" ] && ok "无 declare -A / mapfile / readarray / 大小写转换" || { no "发现 bash4+ 语法:"; echo "$hits" | sed 's/^/      /'; }

echo "[4] \$变量不紧贴多字节(中文/全角)（非注释行）"
# bash 3.2 在非 UTF-8 locale 会把紧贴变量名的多字节字节并入变量名 → unbound。要 \${VAR} 定界。
hits=$(grep -rnP '\$[A-Za-z_][A-Za-z_0-9]*[\x{4e00}-\x{9fff}\x{ff00}-\x{ffef}]' $SCANFILES 2>/dev/null \
  | grep -vE ':[0-9]+:[[:space:]]*#')
[ -z "$hits" ] && ok "无 \$VAR 紧贴多字节(执行行)" || { no "发现 \$VAR 紧贴多字节:"; echo "$hits" | sed 's/^/      /'; }

echo "[5] 数组展开对空+set -u 安全"
# "\${arr[@]}" 在 bash3.2 + set -u + 空数组会 unbound；应写 \${arr[@]+\"\${arr[@]}\"}
hits=$(grep -rnE '"\$\{[A-Za-z_][A-Za-z_0-9]*\[@\]\}"' $SCANFILES 2>/dev/null \
  | grep -vE '\$\{[A-Za-z_0-9]*\[@\]\+' | grep -vE ':[0-9]+:[[:space:]]*#')
[ -z "$hits" ] && ok "无不安全空数组展开" || { no "发现不安全 \"\${arr[@]}\":"; echo "$hits" | sed 's/^/      /'; }

echo "[6] lhb_timeout 功能：超时返回 124、正常返回 0"
rc=0; lhb_timeout 1 sleep 5 >/dev/null 2>&1 || rc=$?
[ "$rc" = 124 ] && ok "lhb_timeout 超时返回 124" || no "lhb_timeout 超时码=$rc(应124)"
rc=0; lhb_timeout 5 true >/dev/null 2>&1 || rc=$?
[ "$rc" = 0 ] && ok "lhb_timeout 正常返回 0" || no "lhb_timeout 正常码=$rc(应0)"

echo ""
echo "== 可移植性测试: PASS=$PASS FAIL=$FAIL =="
[ "$FAIL" = 0 ] || exit 1
