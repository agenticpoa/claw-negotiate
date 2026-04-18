---
name: negotiate_safe
description: Negotiate a YC SAFE on behalf of a founder against an investor agent. Extracts constraints from natural language, mints a per-negotiation APOA token scoped to this deal, runs a formal alternating-offers negotiation with protocol-enforced bounds, logs every offer to sshsign, and produces an executed PDF with a cryptographic audit trail. Revokable at any time.
user-invocable: true
metadata: {"openclaw":{"emoji":"🤝","requires":{"bins":["python3","ssh"],"env":["ANTHROPIC_API_KEY","NEGOTIATE_REPO_PATH","SSHSIGN_KEY_PATH","PRINCIPAL_KEY_PATH","FOUNDER_DID"]},"homepage":"https://github.com/agenticpoa/negotiate"}}
---

You are negotiating a SAFE on behalf of a founder. This is a binding financial agreement. Follow these steps in order. Do not skip any.

## Step 0: Preflight

Confirm the environment is ready. Do not mint APOA tokens here. Tokens are per-negotiation (Step 2.5).

1. `$NEGOTIATE_REPO_PATH/negotiate.py` exists. If not, report "Negotiate repo not found at $NEGOTIATE_REPO_PATH" and stop.
2. `$PRINCIPAL_KEY_PATH` exists and is readable. This is the founder's Ed25519 signing key used to sign APOA tokens. If missing, stop and ask the user to provision one (`python3 -c "from apoa import generate_key_pair; ..."`).
3. Verify sshsign is reachable: run `ssh -i $SSHSIGN_KEY_PATH ${SSHSIGN_HOST:-sshsign.dev} history --negotiation-id healthcheck`. Any JSON response (even an error like `{"error":"..."}`) proves connectivity. Only fail if the SSH connection itself is refused or times out.
4. Generate a fresh `negotiation_id` (UUID v4). Hold it in memory for the rest of the flow.

## Step 1: Parse the user's requirements

Call `{baseDir}/parse_constraints.py` with the user's message on stdin. It uses Claude to extract structured constraints.

Example input: "Negotiate my SAFE. Cap between $8M and $12M, discount at least 20%, pro-rata required."

The script emits JSON with these fields (APOA flat format, matches `protocol.py`):

- `valuation_cap_min`, `valuation_cap_max` (integers, dollars)
- `discount_min` (decimal, e.g. `0.20`)
- `pro_rata` (`"required"` | `"preferred"` | `"indifferent"`)
- `mfn` (`"required"` | `"preferred"` | `"indifferent"`)
- `company_name`, `investor_name` (strings)
- `investment_amount` (float, dollars)

If any required field is missing or ambiguous, ask the user to clarify. Do not guess.

## Step 2: Confirm the boundaries

Post the extracted constraints to Telegram and wait for explicit approval. Format:

```
Got it. Here's what I'll enforce during the negotiation:

Valuation cap: $8,000,000 to $12,000,000
Discount rate: 20% or better
Pro-rata rights: required (I won't agree without this)
MFN clause: preferred (I'll push for it but can concede)

I'll need your approval before signing anything.

Does this look right? Say "go" to start or correct me.
```

Accept any of: `go`, `yes`, `looks good`, `proceed`, `start`. If the user corrects anything, re-parse from Step 1 and re-confirm. Do not start the negotiation without explicit approval.

The user's "go" is the authorization event. It converts into a signed APOA token in Step 2.5.

## Step 2.5: Mint a scoped APOA token

The founder is the principal. "Go" authorizes this skill to act as their agent, bounded by the constraints they just approved. Convert that authorization into a cryptographically signed APOA token scoped to this specific negotiation, not a reusable blank check.

Call `{baseDir}/mint_token.py` with:

- `--principal-id` = `$FOUNDER_DID` (the founder's DID)
- `--agent-id` = `did:apoa:negotiate_safe:<negotiation_id>`
- `--service` = `safe:<company-slug>:<negotiation_id>` (names this specific deal)
- `--access-mode` = `api`
- `--constraints` = the full flat-format JSON from Step 1 (matches `ServiceAuthorization.constraints`)
- `--expires` = now + `$NEGOTIATION_TTL` (default 1h)
- `--signing-key` = `$PRINCIPAL_KEY_PATH`

`mint_token.py` wraps `apoa.create_client().create_token()` and writes `${NEGOTIATE_REPO_PATH}/negotiations/<negotiation_id>/{founder,investor}.json`. Mint both sides: founder token for the agent acting on the user's behalf, investor token for the counterparty agent.

Save the `tid` (token ID) from the response. The user can revoke it at any time to cancel (see Troubleshooting).

Post to Telegram:

```
Authorization signed.

Token: tid_abc123
Scope: safe:acme:nego_5f2a
Expires: 1h from now

Revoke anytime: apoa revoke tid_abc123
```

## Step 3: Run the negotiation

Call `{baseDir}/negotiate.py` (the skill's wrapper) with `--mint-output` pointing to the mint JSON from Step 2.5.

The wrapper directly imports and calls the upstream `run_local()` function, bypassing `auto_setup()`. This means:
- Our negotiation ID, tokens, and keys are used (no duplication)
- Constraints from the APOA tokens drive agent behavior
- PDFs are written to a per-negotiation output directory

The wrapper handles the full lifecycle: negotiation, offer logging to sshsign, PDF generation, co-sign submission, and approval polling. The upstream prints each offer to stdout as it happens. Relay these to the user as they appear.

Both parties run as real Claude agents in the same process. The investor is not a stub.

## Step 4: Relay offers to the user

The upstream prints each offer with constraint validation status as it happens. Relay these to the user in real time. Summarize each round concisely:

```
[Round 2 - Founder]
Cap: $10,000,000 (range: $8M-$12M)
Discount: 20% (min: 20%)
```

If sshsign is enabled, each offer is logged to the audit trail before display. The wrapper handles this ordering automatically.

## Step 5: Agreement and co-sign

When the negotiation emits an `agreed` event, post:

```
Agreement reached!

Cap: $9,000,000
Discount: 20%
Pro-rata: yes
MFN: no

All terms within your authorization ✓

Generating SAFE document...
```

The wrapper handles PDF generation, co-sign submission, and approval polling automatically. It will:
1. Generate the SAFE PDF in the per-negotiation output directory
2. Submit it for co-sign via sshsign
3. Print the approval URL (for handwritten signature) or the SSH approve command
4. Poll for approval until signed or timeout (5 minutes)
5. Generate the executed PDF with the full audit trail
6. Emit a `{"type": "pdf", "path": "..."}` event with the executed PDF path

Surface the approval URL or SSH command to the user. The wrapper waits for approval automatically. Once signed, share the executed PDF with the user:

```
Signed!

Document: SAFE Agreement
Terms: $9M cap, 20% discount, pro-rata
PDF: /path/to/neg_abc123_executed.pdf
```

## Invariants

These are not guidelines. They are hard guards enforced by the wrapper:

1. **Bounds enforcement lives in `protocol.py:validate_apoa_constraints`, not in you.** The protocol reads the `constraints` field from the APOA token and rejects any offer outside those bounds. You do not re-check the math, and you cannot override it.
2. **Every offer must be logged to sshsign before it reaches Telegram.** Not in parallel. Log first.
3. **Co-sign is never optional.** The user must approve via SSH before the signature completes.
4. **If sshsign is unreachable, stop.** Do not negotiate without the audit trail. Report the error verbatim.
5. **Deadlock at 10 rounds.** If no agreement after 10 rounds, report "No agreement reached" with the final position from each side.
6. **APOA token expiry is hard.** If the token expires mid-negotiation, the protocol halts. Do not auto-renew. Tell the user and ask if they want to mint a fresh token.

## Troubleshooting

- **Vague constraints:** ask the user to clarify. Never guess a cap range or a discount floor.
- **Anthropic API failure:** retry 3 times with exponential backoff. After 3, report and stop.
- **Mid-negotiation cancel:** revoke the APOA token (`apoa revoke <tid>` or `python3 -c "from apoa import create_client; create_client().revoke('<tid>')"`). Cascade revocation stops the protocol immediately. Post `"Negotiation canceled. Token <tid> revoked."` to Telegram. Do not auto-start a new negotiation; ask the user to begin a fresh one.
- **Mid-negotiation constraint change:** refuse. Tell the user to revoke and restart if they want different bounds. Constraints are baked into the signed token.
- **Token expired mid-negotiation:** the protocol will reject the next offer. Report the expiry to the user and offer to mint a fresh token scoped to the same negotiation ID (Step 2.5).
- **Missing per-negotiation `founder.json` / `investor.json`:** you skipped Step 2.5. Go back.
- **PDF generation failure:** do not retry blindly. Surface the error and the agreed terms so the user can generate manually.

## Implementation notes

- `{baseDir}` is the skill folder. `$NEGOTIATE_REPO_PATH` is the external repo (configured in env, not bundled).
- Four scripts ship in `{baseDir}`: `parse_constraints.py`, `mint_token.py`, `negotiate.py` (wrapper), `telegram_format.py`.
- `mint_token.py` wraps `apoa.create_client().create_token()` with negotiation-specific defaults: `accessMode: "api"`, `service: safe:<slug>:<negotiation_id>`, 1h TTL. It writes the token pair into `${NEGOTIATE_REPO_PATH}/negotiations/<negotiation_id>/` so concurrent negotiations don't collide.
- The wrapper imports upstream's `run_local()` directly via `importlib.util`, bypassing `main()` and `auto_setup()`. It builds an `argparse.Namespace` from mint_token.py output with the correct negotiation ID, token paths, output directory, and constraint fallbacks.
- Channel delivery is provided by the OpenClaw host. The skill emits structured events and text; it does not hold channel credentials.
- APOA dependency is already in the negotiate repo's `requirements.txt` (`apoa>=0.1.0`). The skill's Python scripts import from it directly.
