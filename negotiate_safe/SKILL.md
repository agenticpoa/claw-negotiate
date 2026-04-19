---
name: negotiate_safe
description: Negotiate a YC SAFE on behalf of a founder against an investor agent. Extracts constraints from natural language, mints a per-negotiation APOA token scoped to this deal, runs a formal alternating-offers negotiation with protocol-enforced bounds, logs every offer to sshsign, and produces an executed PDF with a cryptographic audit trail. Revokable at any time.
user-invocable: true
metadata: {"openclaw":{"emoji":"🤝","requires":{"bins":["python3","ssh"],"env":["ANTHROPIC_API_KEY","NEGOTIATE_REPO_PATH"]},"homepage":"https://github.com/agenticpoa/negotiate"}}
---

You are negotiating a SAFE on behalf of a founder. This is a binding financial agreement. Follow these steps in order. Do not skip any.

IMPORTANT: All exec calls in this skill MUST be simple commands. Use ONLY `python3 /path/to/script.py --flag value`. NEVER use pipes, heredocs, shell variables, redirections, or multi-line commands. Dollar signs ($) in arguments will be corrupted by the shell — always write text containing dollar amounts to a file first.

## Step 1: Parse and confirm

1. Write the user's message to `/tmp/safe_request.txt` using the write tool
2. Run:

```
python3 {baseDir}/run_safe.py prepare --message-file /tmp/safe_request.txt --output-dir /tmp/safe_negotiate
```

3. Show the parsed constraints to the user and ask for confirmation. Wait for "go" before proceeding.

## Step 2: Negotiate

Once the user confirms, run the negotiation:

```
python3 {baseDir}/run_safe.py negotiate --output-dir /tmp/safe_negotiate
```

IMPORTANT exec parameters:
- Set `timeout` to 600 (negotiation takes 90-180 seconds with sshsign logging)
- Set `yieldMs` to 300000 (prevents auto-backgrounding so you get the full output)
- Do NOT set `background` to true — run foreground so the output returns directly

This command mints APOA tokens, runs the full negotiation, logs every offer to sshsign, and generates the PDF.

## Step 3: Present results

The negotiate command writes a pre-formatted `results.md` to the output dir AND prints it to stdout. The output includes round-by-round offers, the outcome, the signing link, and the PDF path — all formatted for the user.

Relay the output to the user exactly as formatted. Do not summarize or restructure it.

If the output includes a signing URL (starts with `https://`), make sure it's clickable.

If the output includes a PDF path, share the PDF file with the user as an attachment.

## Step 4: Verify signature

After the user signs, verify the approval:

```
ssh sshsign.dev get-envelope --id pnd_xxx
```

If approved, confirm to the user and share the executed PDF.

## Invariants

1. **Bounds enforcement lives in `protocol.py:validate_apoa_constraints`.** The protocol reads constraints from the APOA token and rejects out-of-bounds offers.
2. **Every offer is logged to sshsign before display.**
3. **Co-sign is never optional.** The user must approve before the signature completes.
4. **If sshsign is unreachable, stop.** Do not negotiate without the audit trail.
5. **Deadlock at 10 rounds.** Report "No agreement reached" with final positions.
6. **APOA token expiry is hard.** If expired mid-negotiation, the protocol halts.

## Troubleshooting

- **Vague constraints:** ask the user to clarify. Do not guess.
- **Anthropic API failure:** the script retries internally. If it fails, report the error.
- **Mid-negotiation cancel:** the user can revoke the APOA token. Start a fresh negotiation.
- **Constraint change:** refuse. Constraints are baked into the signed token. Revoke and restart.
