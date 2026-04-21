#!/usr/bin/env bash
# Dry-run step 4: run the actual negotiation (no sshsign, no signing).
# Two Claude agents negotiate within the APOA constraints you set.
# Takes 30-60 seconds. Output is colored terminal text showing each offer.
set -euo pipefail

: "${NEGOTIATE_REPO_PATH:?NEGOTIATE_REPO_PATH not set}"
: "${ANTHROPIC_API_KEY:?ANTHROPIC_API_KEY not set}"

CONSTRAINTS_FILE=/tmp/claw-constraints.json
test -f "$CONSTRAINTS_FILE" || { echo "No $CONSTRAINTS_FILE. Run step1_parse.sh first."; exit 1; }

# Pull founder constraints from the parsed JSON
eval "$(python3 -c "
import json, os
c = json.load(open('$CONSTRAINTS_FILE'))
print(f'export COMPANY_NAME=\"{c[\"company_name\"]}\"')
print(f'export INVESTOR_NAME=\"{c[\"investor_name\"]}\"')
print(f'export INVESTMENT_AMOUNT={c[\"investment_amount\"]}')
print(f'export FOUNDER_CAP_MIN={c[\"valuation_cap_min\"]}')
print(f'export FOUNDER_CAP_MAX={c[\"valuation_cap_max\"]}')
print(f'export FOUNDER_DISCOUNT_MIN={c[\"discount_min\"]}')
print(f'export FOUNDER_DISCOUNT_MAX={max(0.25, c[\"discount_min\"])}')
pro = 'true' if c['pro_rata'] == 'required' else 'false'
mfn = 'true' if c['mfn'] == 'required' else 'false'
print(f'export FOUNDER_PRO_RATA_REQUIRED={pro}')
print(f'export FOUNDER_MFN_REQUIRED={mfn}')
")"

export FOUNDER_NAME="${FOUNDER_NAME:-Juan Figuera}"
export FOUNDER_TITLE="${FOUNDER_TITLE:-CEO}"

# Investor constraints come from env (set before running this script).
# If not set, upstream uses its defaults from .env or hardcoded values.

echo "=== Negotiation parameters ==="
echo "  Company:    $COMPANY_NAME"
echo "  Founder:    $FOUNDER_NAME ($FOUNDER_TITLE)"
echo "  Investor:   $INVESTOR_NAME"
echo "  Investment: \$$INVESTMENT_AMOUNT"
echo
echo "  Founder constraints:  cap \$${FOUNDER_CAP_MIN}-\$${FOUNDER_CAP_MAX}, discount ${FOUNDER_DISCOUNT_MIN}-${FOUNDER_DISCOUNT_MAX}"
echo "  Investor constraints: cap \$${INVESTOR_CAP_MIN:-default}-\$${INVESTOR_CAP_MAX:-default}, discount ${INVESTOR_DISCOUNT_MIN:-default}-${INVESTOR_DISCOUNT_MAX:-default}"
echo
echo "  Mode: --no-sshsign (no audit trail, no signing)"
echo
echo "Starting negotiation..."
echo

cd "$NEGOTIATE_REPO_PATH"
python3 negotiate.py --no-sshsign
