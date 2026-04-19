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
python3 /root/.agents/skills/negotiate_safe/run_safe.py prepare --message-file /tmp/safe_request.txt --output-dir /tmp/safe_negotiate
```

3. Show the parsed constraints to the user and ask for confirmation. Wait for "go" before proceeding.

## Step 2: Negotiate

Once the user confirms, run the negotiation:

```
python3 /root/.agents/skills/negotiate_safe/run_safe.py negotiate --output-dir /tmp/safe_negotiate
```

Set `timeout` to 600. The command will auto-background after ~10 seconds. That is expected.

Then IMMEDIATELY run the results waiter (do not respond to the user between these two commands):

```
python3 /root/.agents/skills/negotiate_safe/wait_results.py --output-dir /tmp/safe_negotiate
```

Set `timeout` to 600. This command blocks until the negotiation finishes and results are ready (typically 2 minutes), then prints the full results. You will get the output directly when it returns.

## Step 3: Present results

The wait_results.py output contains the complete negotiation results. Send it to the user EXACTLY as written. Do not summarize, restructure, or shorten it. It includes:
- Every negotiation round with terms and reasoning
- The outcome (agreement or deadlock)
- A signing URL (send as a clickable link)
- The PDF path (share the PDF file with the user)

## Step 4: Verify signature

After presenting the results, tell the user:

"After you've signed, reply 'signed' and I'll verify and share the final document."

When the user replies (e.g. "signed", "done", "approved"), extract the pending ID from the results (it's in the signing URL after `pnd_`) and verify:

```
python3 /root/.agents/skills/negotiate_safe/wait_results.py --output-dir /tmp/safe_negotiate
```

Read the results.md to find the pending ID, then run:

```
ssh sshsign.dev get-envelope --id pnd_XXXXX
```

If the response contains `"status": "approved"`, tell the user:

"Signature verified! Your executed SAFE is ready."

Then share the PDF file from the output directory.

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
