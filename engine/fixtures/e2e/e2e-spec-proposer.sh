#!/usr/bin/env bash
# e2e spec proposer stub：往 spec 追加 E2E_FIXED（模拟"按反馈就地改"）。
set -u; ART="${1:?}"; echo "E2E_FIXED (e2e proposer 改稿)" >> "$ART"
