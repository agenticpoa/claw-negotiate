---
name: negotiate_safe
description: Negotiate a SAFE on behalf of either a founder or an investor — the counterparty is played by another OpenClaw (demo mode) or a real human on another OpenClaw instance (two-party mode). Extracts the user's role and constraints from natural language, mints a per-negotiation APOA token scoped to this deal, runs a formal alternating-offers negotiation with protocol-enforced bounds, logs every offer to sshsign, and produces an executed PDF with a cryptographic audit trail. Revokable at any time. ALSO handles exact `GO`/`go` confirmations after a SAFE authorization review card, plus the Phase 8 `/bind` slash command (pattern `/bind` with optional `@BotName` suffix and `INV-XXXXX` code) used in Telegram group chats to bind an existing two-party negotiation to the current group as its live-observation venue — route ANY exact GO confirmation or message starting with `/bind` to this skill regardless of surrounding context.
user-invocable: true
metadata: {"openclaw":{"emoji":"🤝","requires":{"bins":["python3","ssh"],"env":["NEGOTIATE_REPO_PATH","USER_DID"]},"homepage":"https://github.com/agenticpoa/negotiate"}}
---

You are negotiating a SAFE on behalf of the user. The user may be the founder (raising) or the investor (investing) — the skill detects which from their natural-language request and plays the opposite side as an AI. Either way, this is a binding financial agreement. Follow these three steps.

IMPORTANT: All exec calls MUST be simple commands. Use ONLY `python3 /path/to/script.py --flag value`. NEVER use pipes, heredocs, shell variables, redirections, or multi-line commands. Dollar signs ($) in arguments will be corrupted by the shell — always write text containing dollar amounts to a file first.

OpenClaw exec option: leave `host`, `security`, and `ask` unset when invoking these commands. Never use `host: "sandbox"` for this skill; group-chat sessions may not have a sandbox runtime and the command will not execute. Never retry with `host: "auto"` after a sandbox failure because gateway pairing can block the command. If the exec tool requires an explicit host, use `host: "node"`.

## Intent triage — read in order, first match wins

Before running the negotiation steps, check the user's message against these shortcuts:

**A. Message starts with `/bind` (with or without `@BotName` suffix and with or without a trailing `INV-XXXXX` code):** THIS IS ALWAYS YOUR SKILL. Even in a brand-new group chat with no prior context, a `/bind` message is a Phase 8 group-bind request and MUST route here. Run:
```
python3 /root/.agents/skills/negotiate_safe/run_safe.py bind --message "<the full /bind message text exactly as received>" --chat-id <chat.id from the inbound envelope> --from-id <from.id from the inbound envelope>
```
End your turn. Do NOT relay anything, do not try to parse the code yourself, and do not send `NO_REPLY`. The skill posts its own confirmation card (or a specific error card) into the group on your behalf. Examples that MATCH this shortcut: `/bind INV-7K3X9`, `/bind@AgenticPOA_bot INV-4N6PK`, `/bind`, `/bind@SomeBot`.

**A.5. Inbound is a system event whose body starts with `negotiate_safe_scan`:** THIS IS ALWAYS YOUR SKILL. This fires from the droplet's own OpenClaw cron job (installed at mint time for P7-5 durable founder-wait). It tells the skill to check every waiting two-party negotiation and resume any whose investor has just joined. Run:
```
python3 /root/.agents/skills/negotiate_safe/run_safe.py scan
```
End your turn. `scan` is idempotent, handles its own Telegram output (orienting card in the bound group, streaming, status updates), and exits 0 even if there's nothing to resume. Do not re-check, do not relay, do not emit any message yourself — especially nothing that starts with `/`, which would bounce back through this very dispatcher.

**A.7. Message starts with `/cancel` (with or without `@BotName` suffix), OR is exactly the word `cancel` / `Cancel` / `stop` / `abort` (case-insensitive, single-word):** THIS IS ALWAYS YOUR SKILL. Same emphatic dispatch pattern as `/bind` above. Run:
```
python3 /root/.agents/skills/negotiate_safe/run_safe.py cancel --output-dir /tmp/safe_negotiate --chat-id <chat.id from the inbound envelope>
```
End your turn. Do NOT relay anything, do not acknowledge in your own words, and do not send `NO_REPLY` (the skill posts its own confirmation card on your behalf — if you also reply the user sees a duplicate). Examples that MATCH this shortcut: `/cancel`, `/cancel@AgenticPOA_bot`, `cancel`, `Cancel`, `stop`, `abort`. The model has historically tried to substitute prose like "Understood, canceled." or "Got it, canceled." in place of dispatching — DO NOT do that. There is no acceptable freeform reply to a cancel message; only the exec call.

**A.9. Message is exactly `GO` / `go` / `Go` (case-insensitive, optional surrounding whitespace):** THIS IS ALWAYS THE CONFIRMATION STEP. Do not run `prepare` again. Do not ask for profile again. Run:
```
python3 /root/.agents/skills/negotiate_safe/run_safe.py negotiate --output-dir /tmp/safe_negotiate --chat-id <chat.id from the inbound envelope>
```
Set `timeout` to 1200. End your turn by returning the literal string `NO_REPLY` and nothing else. The skill posts the authorization, invitation/join confirmation, signing links, and executed PDF itself. This shortcut exists because treating `GO` as a fresh negotiation request causes stalls, duplicate cards, and missing counterparty signature requests.

**B. "Show me my profile" / "What's my profile" / "Who am I" / "My profile":**
```
python3 /root/.agents/skills/negotiate_safe/run_safe.py profile
```
End your turn. The skill pushes the profile card to chat.

**C. "Update my profile…" / "Change my name…" / "I'm now …" (and the user supplies new identity info in the same message):**
Write the update text to `/tmp/safe_identity.txt`, then:
```
python3 /root/.agents/skills/negotiate_safe/run_safe.py setup --message-file /tmp/safe_identity.txt
```
End your turn. The skill overwrites whichever fields are in the message and confirms in chat. A founder profile should include the founder's name and company, e.g. "I'm Juan Figuera, CEO of APOA Inc". An investor profile should include the investor's name and firm/fund, e.g. "I'm Nora Vassileva, partner at SD Fund".

**D. Anything else** (negotiation request, "go" confirmation, or a correction) → proceed with Step 1 below.

## Step 1: Parse and confirm

1. Write the user's message to `/tmp/safe_request.txt` using the write tool.
2. Run:

```
python3 /root/.agents/skills/negotiate_safe/run_safe.py prepare --message-file /tmp/safe_request.txt --output-dir /tmp/safe_negotiate
```

3. The skill pushes a "⏳ Reading your negotiation terms…" interstitial and then a formatted confirm card directly to the user's chat. **Do not re-display the constraints yourself — the card is already in the chat.** Simply end your turn.

**Handling the user's next message:**
- If `prepare` exited with code 2 and pushed a "👋 Welcome!" setup prompt (first-run, no identity configured), the user's next message is their self-introduction. Follow Step 1.5 below.
- If the confirm card was pushed normally, the user's next reply is either "GO" (proceed to Step 2) or a correction (rerun step 1 with the corrected text).
- If the inbound turn contains both a just-finished `prepare` result/confirm-card delivery and the user's "GO" reply, do **not** launch `negotiate` in that same turn. The card may only now be reaching Telegram, so end the turn and wait for a fresh "GO" after the user has seen it. This avoids duplicate/out-of-order authorization cards.

## Step 1.5: Identity setup (first-run only)

If the user just replied to a "👋 Welcome!" prompt (they said something like "I'm Juan Figuera, CEO of APOA Inc" or "Nora Vassileva, partner at SD Fund"):

1. Write the reply to `/tmp/safe_identity.txt` using the write tool.
2. Run:

```
python3 /root/.agents/skills/negotiate_safe/run_safe.py setup --message-file /tmp/safe_identity.txt
```

3. The skill parses the identity, persists it via `openclaw config set`, pushes a confirmation to the chat, AND — if the user had typed a negotiation request before the welcome prompt — automatically runs `prepare` again with that stashed request so they don't have to retype. End your turn. The confirm card (or another setup prompt, if the identity parse failed) will appear in the chat.

Saved profile describes the local user and should not be repeated every negotiation. Counterparty identity belongs in the negotiation request. For example, a founder can say: "Live negotiation with Nora Vassileva at SD Fund. Cap $30M to $40M, 10% discount, pro-rata required." The skill will combine the saved founder profile with Nora's counterparty identity for the authorization, signing view, and final SAFE.

## Step 2: Launch the negotiation

Once the user confirms, launch:

```
python3 /root/.agents/skills/negotiate_safe/run_safe.py negotiate --output-dir /tmp/safe_negotiate
```

Set `timeout` to 1200. The command will auto-background after ~10 seconds. That is expected. If the investor joins after the foreground turn is reaped, a droplet-installed cron job fires the `negotiate_safe_scan` system event about every 5 seconds and re-enters via rule A.5 above — no action from you.

## Step 3: Relay the launch to the user

Your output at this step MUST be the literal string `NO_REPLY` and nothing else. Do not add a preamble, an emoji, a "starting" line, a confirmation, or a sign-off. The skill pushes every user-visible card itself (setup, authorization, invitation, joined, round-by-round, signing URL, signed confirmation, executed PDF). If you emit your own text here, the user sees it in addition to the skill's cards — duplication and ordering bugs follow.

**DO NOT** poll, run further commands, or describe the rounds yourself. After the user signs, the browser opens the Telegram chat automatically — no further message arrives for you to handle. Your job for this skill is done.

## Invariants

1. **Bounds enforcement lives in `protocol.py:validate_apoa_constraints`.** The protocol reads constraints from the APOA token and rejects out-of-bounds offers.
2. **Every offer is logged to sshsign before display.**
3. **Co-sign is never optional.** The user must approve before the signature completes.
4. **If sshsign is unreachable, stop.** Do not negotiate without the audit trail.
5. **Deadlock at 10 rounds.** The script reports "No agreement reached" with final positions.
6. **APOA token expiry is hard.** If expired mid-negotiation, the protocol halts.

## Troubleshooting

- **Operator install check:** run `python3 /root/.agents/skills/negotiate_safe/run_safe.py doctor` on the OpenClaw host. It validates required env vars, upstream negotiate compatibility, sshsign reachability, and OpenClaw message/cron primitives before a live negotiation.
- **Operator setup:** run `python3 /root/.agents/skills/negotiate_safe/run_safe.py operator-setup --role founder --bot-username @YourBot --sshsign-host sshsign.dev --negotiate-repo-path /path/to/negotiate` (or `--role investor`) to persist deployment-level skill config via `openclaw config set`.
- **Vague constraints:** ask the user to clarify. Do not guess.
- **Anthropic API failure:** the script retries internally. If it fails, report the error.
- **Mid-negotiation cancel:** the user can revoke the APOA token. Start a fresh negotiation.
- **Constraint change:** refuse. Constraints are baked into the signed token. Revoke and restart.
- **Signature not received after 15 minutes:** the script will post a manual-verify prompt. If the user replies that they signed, run `ssh sshsign.dev get-envelope --id <pnd_XXX>` where `pnd_XXX` is in `/tmp/safe_negotiate/events.ndjson`.
