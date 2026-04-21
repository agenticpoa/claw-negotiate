#!/usr/bin/env bash
# Dry-run step 2: mint an APOA token pair from parsed constraints.
# Expects /tmp/claw-constraints.json to exist (step 1).
# Usage:
#   bash scripts/step2_mint.sh
#   FOUNDER_NAME="Jane Doe" bash scripts/step2_mint.sh   # override defaults
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"

CONSTRAINTS_FILE=/tmp/claw-constraints.json
OUT=/tmp/claw-mint.json

test -f "$CONSTRAINTS_FILE" || { echo "No $CONSTRAINTS_FILE. Run step1_parse.sh first."; exit 1; }
: "${NEGOTIATE_REPO_PATH:?NEGOTIATE_REPO_PATH not set}"

FOUNDER_NAME="${FOUNDER_NAME:-Juan Figuera}"
FOUNDER_TITLE="${FOUNDER_TITLE:-CEO}"

# Extract fields from the parsed constraints without a jq dependency
COMPANY=$(python3 -c "import json; print(json.load(open('$CONSTRAINTS_FILE'))['company_name'])")
INVESTOR=$(python3 -c "import json; print(json.load(open('$CONSTRAINTS_FILE'))['investor_name'])")
AMOUNT=$(python3 -c "import json; print(json.load(open('$CONSTRAINTS_FILE'))['investment_amount'])")

echo "Minting APOA token pair:"
echo "  Principal DID: ${FOUNDER_DID:-did:apoa:principal}"
echo "  Founder:       $FOUNDER_NAME ($FOUNDER_TITLE)"
echo "  Company:       $COMPANY"
echo "  Investor:      $INVESTOR"
echo "  Investment:    \$$AMOUNT"
echo "  TTL:           ${NEGOTIATION_TTL:-3600}s (1 hour)"
echo "  sshsign keys:  skipped (dry run)"
echo

python3 "$REPO_ROOT/negotiate-safe/mint_token.py" \
    --constraints-json "$(cat "$CONSTRAINTS_FILE")" \
    --company-name "$COMPANY" \
    --founder-name "$FOUNDER_NAME" \
    --founder-title "$FOUNDER_TITLE" \
    --investor-name "$INVESTOR" \
    --investment-amount "$AMOUNT" \
    --skip-sshsign-keys \
    | tee "$OUT"

echo
echo "Saved to $OUT"
