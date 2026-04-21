---
name: negotiate_safe
description: Negotiate a YC SAFE on behalf of either a founder or an investor — the counterparty is played by an AI agent (demo mode). Extracts the user's role and constraints from natural language, mints a per-negotiation APOA token scoped to this deal, runs a formal alternating-offers negotiation with protocol-enforced bounds, logs every offer to sshsign, and produces an executed PDF with a cryptographic audit trail. Revokable at any time.
user-invocable: true
metadata: {"openclaw":{"emoji":"🤝","requires":{"bins":["python3","ssh"],"env":["ANTHROPIC_API_KEY","NEGOTIATE_REPO_PATH","USER_DID"]},"homepage":"https://github.com/agenticpoa/negotiate"}}
---

You are negotiating a SAFE on behalf of the user. The user may be the founder (raising) or the investor (investing) — the skill detects which from their natural-language request and plays the opposite side as an AI. Either way, this is a binding financial agreement. Follow these three steps.

IMPORTANT: All exec calls MUST be simple commands. Use ONLY `python3 /path/to/script.py --flag value`. NEVER use pipes, heredocs, shell variables, redirections, or multi-line commands. Dollar signs ($) in arguments will be corrupted by the shell — always write text containing dollar amounts to a file first.

## Intent triage — read in order, first match wins

Before running the negotiation steps, check the user's message against these shortcuts:

**A. "Show me my profile" / "What's my profile" / "Who am I" / "My profile":**
```
python3 /root/.agents/skills/negotiate_safe/run_safe.py profile
```
End your turn. The skill pushes the profile card to chat.

**B. "Update my profile…" / "Change my name…" / "I'm now …" (and the user supplies new identity info in the same message):**
Write the update text to `/tmp/safe_identity.txt`, then:
```
python3 /root/.agents/skills/negotiate_safe/run_safe.py setup --message-file /tmp/safe_identity.txt
```
End your turn. The skill overwrites whichever fields are in the message and confirms in chat.

**C. Anything else** (negotiation request, "go" confirmation, or a correction) → proceed with Step 1 below.

## Step 1: Parse and confirm

1. Write the user's message to `/tmp/safe_request.txt` using the write tool.
2. Run:

```
python3 /root/.agents/skills/negotiate_safe/run_safe.py prepare --message-file /tmp/safe_request.txt --output-dir /tmp/safe_negotiate
```

3. The skill pushes a "⏳ Analyzing…" interstitial and then a formatted confirm card directly to the user's chat. **Do not re-display the constraints yourself — the card is already in the chat.** Simply end your turn.

**Handling the user's next message:**
- If `prepare` exited with code 2 and pushed a "👋 Welcome!" setup prompt (first-run, no identity configured), the user's next message is their self-introduction. Follow Step 1.5 below.
- If the confirm card was pushed normally, the user's next reply is either "GO" (proceed to Step 2) or a correction (rerun step 1 with the corrected text).

## Step 1.5: Identity setup (first-run only)

If the user just replied to a "👋 Welcome!" prompt (they said something like "I'm Juan Figuera, CEO of APOA Inc" or "Mark Stone, partner at Blue Fund"):

1. Write the reply to `/tmp/safe_identity.txt` using the write tool.
2. Run:

```
python3 /root/.agents/skills/negotiate_safe/run_safe.py setup --message-file /tmp/safe_identity.txt
```

3. The skill parses the identity, persists it via `openclaw config set`, pushes a confirmation to the chat, AND — if the user had typed a negotiation request before the welcome prompt — automatically runs `prepare` again with that stashed request so they don't have to retype. End your turn. The confirm card (or another setup prompt, if the identity parse failed) will appear in the chat.

## Step 2: Launch the negotiation

Once the user confirms, launch:

```
python3 /root/.agents/skills/negotiate_safe/run_safe.py negotiate --output-dir /tmp/safe_negotiate
```

Set `timeout` to 1200. The command will auto-background after ~10 seconds. That is expected.

## Step 3: Relay the launch to the user

Reply exactly once: "🚀 Starting negotiation"

**DO NOT** poll, run further commands, or describe the rounds yourself. The script sends every round, the signing URL, the "Signed & sealed" confirmation, and the executed PDF directly to the chat via the host. After the user signs, the browser opens the Telegram chat automatically — no further message arrives for you to handle. Your job for this skill is done.

## Invariants

1. **Bounds enforcement lives in `protocol.py:validate_apoa_constraints`.** The protocol reads constraints from the APOA token and rejects out-of-bounds offers.
2. **Every offer is logged to sshsign before display.**
3. **Co-sign is never optional.** The user must approve before the signature completes.
4. **If sshsign is unreachable, stop.** Do not negotiate without the audit trail.
5. **Deadlock at 10 rounds.** The script reports "No agreement reached" with final positions.
6. **APOA token expiry is hard.** If expired mid-negotiation, the protocol halts.

## Troubleshooting

- **Vague constraints:** ask the user to clarify. Do not guess.
- **Anthropic API failure:** the script retries internally. If it fails, report the error.
- **Mid-negotiation cancel:** the user can revoke the APOA token. Start a fresh negotiation.
- **Constraint change:** refuse. Constraints are baked into the signed token. Revoke and restart.
- **Signature not received after 15 minutes:** the script will post a manual-verify prompt. If the user replies that they signed, run `ssh sshsign.dev get-envelope --id <pnd_XXX>` where `pnd_XXX` is in `/tmp/safe_negotiate/events.ndjson`.
