#!/usr/bin/env bash
# e2e spec reviewer stub：spec 含 E2E_FIXED 才 APPROVE，否则 REVISE（配 proposer 跑出 2 轮收敛）。
set -u; ART="${1:?}"
if grep -q E2E_FIXED "$ART" 2>/dev/null; then echo "VERDICT: APPROVE"; else printf "VERDICT: REVISE\nREASON: 补 E2E_FIXED\n"; fi
