#!/usr/bin/env bash
# Dry-run step 1: parse natural-language terms into APOA constraints.
# Usage:
#   bash scripts/step1_parse.sh                 # uses the default demo message
#   bash scripts/step1_parse.sh "your message"  # use your own
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"

DEFAULT_MSG='Negotiate my SAFE for Claw Corp with Angel Ventures. Cap between $50M and $100M, discount at least 10%, pro-rata required, MFN preferred. Investment: $500,000.'
MSG="${1:-$DEFAULT_MSG}"

OUT=/tmp/claw-constraints.json

echo "Sending to Claude:"
echo "  $MSG"
echo
echo "$MSG" | python3 "$REPO_ROOT/negotiate-safe/parse_constraints.py" | tee "$OUT"
echo
echo "Saved to $OUT"
