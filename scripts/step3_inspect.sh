#!/usr/bin/env bash
# Dry-run step 3: decode the minted JWTs and inspect the APOA definition.
# Proves the constraints are embedded in the signed token, not just a wrapper arg.
set -euo pipefail

MINT_FILE=/tmp/claw-mint.json
test -f "$MINT_FILE" || { echo "No $MINT_FILE. Run step2_mint.sh first."; exit 1; }

python3 - <<'PY'
import base64
import json

def decode_jwt(jwt: str):
    def b64url(s: str) -> bytes:
        return base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))
    h, p, _sig = jwt.split('.')
    return json.loads(b64url(h)), json.loads(b64url(p))

with open('/tmp/claw-mint.json') as f:
    mint = json.load(f)

for role in ('founder', 'investor'):
    path = mint[f'{role}_token_path']
    with open(path) as f:
        jwt = f.read().strip()
    header, payload = decode_jwt(jwt)
    print(f"\n===== {role.upper()} TOKEN =====")
    print(f"Path: {path}")
    print()
    print("Header:")
    print(json.dumps(header, indent=2))
    print()
    print("Payload (APOA definition):")
    print(json.dumps(payload, indent=2))
PY
