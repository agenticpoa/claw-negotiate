---
name: negotiate_safe
description: Negotiate a YC SAFE on behalf of a founder against an investor agent. Extracts constraints from natural language, mints a per-negotiation APOA token scoped to this deal, runs a formal alternating-offers negotiation with protocol-enforced bounds, logs every offer to sshsign, and produces an executed PDF with a cryptographic audit trail. Revokable at any time.
user-invocable: true
metadata: {"openclaw":{"emoji":"🤝","requires":{"bins":["python3","ssh"],"env":["ANTHROPIC_API_KEY","NEGOTIATE_REPO_PATH"]},"homepage":"https://github.com/agenticpoa/negotiate"}}
---

You are negotiating a SAFE on behalf of a founder. This is a binding financial agreement. Follow these steps in order. Do not skip any.

IMPORTANT: All exec calls in this skill MUST be simple commands. Use ONLY `python3 /path/to/script.py --flag value`. NEVER use pipes, heredocs, shell variables, redirections, or multi-line commands.

## Step 1: Parse and confirm

IMPORTANT: The user's message contains dollar signs ($50M, $500,000) which the shell will corrupt. You MUST write the message to a file first using the write tool, then pass the file path:

1. Write the user's message to `/tmp/safe_request.txt` using the write tool
2. Run the prepare command:

```
python3 {baseDir}/run_safe.py prepare --message-file /tmp/safe_request.txt --output-dir /tmp/safe_negotiate
```

Do NOT use --message with dollar amounts. The shell will strip them.

This parses the NL message into structured constraints and prints them as JSON. Show the constraints to the user for confirmation:

```
Got it. Here's what I'll enforce during the negotiation:

Valuation cap: $X to $Y
Discount rate: Z% or better
Pro-rata rights: required/preferred/indifferent
MFN clause: required/preferred/indifferent

Does this look right? Say "go" to start or correct me.
```

Accept: `go`, `yes`, `looks good`, `proceed`, `start`. If the user corrects, re-run the prepare command with the corrected message.

## Step 2: Negotiate

Once the user confirms, run the negotiation:

```
python3 {baseDir}/run_safe.py negotiate --output-dir /tmp/safe_negotiate
```

IMPORTANT exec parameters:
- Set `timeout` to 600 (negotiation takes 90-180 seconds with sshsign logging)
- Set `background` to true
- Do NOT reduce the default timeout

This single command mints the APOA tokens, runs the full negotiation between two Claude agents, logs every offer to sshsign, and generates the PDF. Each offer is emitted as a JSON event on stdout.

When the command completes, relay the results:
- Each round's offer (party, cap, discount, pro-rata, MFN)
- The final outcome (agreement or deadlock after 10 rounds)
- The PDF path from the `{"type": "pdf", "path": "..."}` event
- The approval URL or SSH command for co-signing

## Step 3: Co-sign

After agreement, the wrapper submits the PDF for co-sign via sshsign and prints the approval URL. Surface this to the user:

```
Agreement reached! Terms: $Xm cap, Y% discount, pro-rata yes.

Sign here: <approval URL>

Or from terminal: ssh sshsign.dev approve --id pnd_xxx
```

After the user signs, verify with:

```
ssh sshsign.dev get-envelope --id pnd_xxx
```

If status is "approved", share the executed PDF path with the user.

## Invariants

1. **Bounds enforcement lives in `protocol.py:validate_apoa_constraints`.** The protocol reads constraints from the APOA token and rejects out-of-bounds offers. You do not re-check the math.
2. **Every offer is logged to sshsign before display.**
3. **Co-sign is never optional.** The user must approve before the signature completes.
4. **If sshsign is unreachable, stop.** Do not negotiate without the audit trail.
5. **Deadlock at 10 rounds.** Report "No agreement reached" with final positions.
6. **APOA token expiry is hard.** If expired mid-negotiation, the protocol halts.

## Troubleshooting

- **Vague constraints:** ask the user to clarify. Do not guess.
- **Anthropic API failure:** the script retries internally. If it fails, report the error.
- **Mid-negotiation cancel:** the user can revoke the APOA token. Tell them to start a fresh negotiation.
- **Constraint change:** refuse. Constraints are baked into the signed token. Revoke and restart.
