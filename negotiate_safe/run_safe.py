#!/usr/bin/env python3
"""Single entry point for the negotiate_safe skill.

Two subcommands:
  prepare   — parse NL message into constraints, write config (fast, <10s)
  negotiate — mint tokens + run negotiation + emit results (long, 90-180s)

The output-dir is the shared state between the two calls. The model never
needs to pass files between scripts or construct shell pipes.

Usage:
  python3 run_safe.py prepare --message "Negotiate my SAFE..." --output-dir /tmp/safe_123
  python3 run_safe.py negotiate --output-dir /tmp/safe_123
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from parse_constraints import extract_constraints
from parse_identity import extract_identity
from format_event import format_event
from telegram_push import (
    resolve_chat_id,
    send_telegram,
    send_signing_url_to_dm,
    SigningUrlTargetError,
)
from typing_loop import TypingLoop, get_bot_token
from sshsign_session import (
    SshsignSession,
    SshsignSessionError,
    SessionTerminalError,
    SessionExpiredError,
    SessionNotFoundError,
    SessionRoleConflictError,
    SessionNotMemberError,
    GroupAlreadyBoundError,
)
import state_store


IDENTITY_SENTINEL_PATH = Path("/tmp/safe_negotiate/pending_negotiation.txt")


def _identity_configured() -> bool:
    """Return True if the installed user's identity is already set up.

    Either FOUNDER_NAME or INVESTOR_NAME counts — the user might only
    negotiate from one side of the deal. A founder-only install sets
    FOUNDER_NAME; an investor-only install sets INVESTOR_NAME (e.g. an
    investor joining a two-party code via their own OC). Either way the
    wizard has already run to completion and we skip the prompt.
    """
    return bool(
        (os.environ.get("FOUNDER_NAME") or "").strip()
        or (os.environ.get("INVESTOR_NAME") or "").strip()
    )


SKILL_DIR = Path(__file__).resolve().parent
STREAM_HELPER = SKILL_DIR / "_stream_negotiate.py"


def _sshsign_session_id(negotiation_id: str) -> str:
    """Return the session_id we use when talking to sshsign session APIs.

    Upstream agenticpoa/negotiate derives its session_id as
    ``f"session_{negotiation_id}"`` inside auto_setup, and passes THAT
    value as --session-id on sign calls. For sshsign's cross-party
    get-envelope ACL (which matches pending_signatures.signing_session_id
    against the signing_sessions row's session_id) to let members read
    each other's pendings, we have to use the SAME prefixed form when
    create-session / join-session / get-session etc.
    """
    if not negotiation_id:
        return ""
    if negotiation_id.startswith("session_"):
        return negotiation_id
    return f"session_{negotiation_id}"


def _negotiation_id_from_sshsign_session_id(sshsign_session_id: str) -> str:
    """Inverse of _sshsign_session_id. sshsign's get-session returns the
    prefixed form (e.g. 'session_neg_X'); upstream agenticpoa/negotiate
    and our on-disk layout both key off the RAW negotiation_id
    ('neg_X'). Called by the joiner to recover the raw id after fetching
    a session by code. Tolerates an already-unprefixed input so it's
    safe to apply twice.
    """
    if not sshsign_session_id:
        return ""
    if sshsign_session_id.startswith("session_"):
        return sshsign_session_id[len("session_"):]
    return sshsign_session_id


def _resolve_group_chat_id(
    session_id: str,
    session_client=None,
) -> str | None:
    """Read the session's bound Telegram group chat_id, or None if unbound.

    Phase 8 uses sshsign's `group_chat_id` column (set via the K0
    `bind-group` RPC) to pin the "live negotiation" venue. If the session
    has been bound to a group, returns the chat_id as a string (negative
    int); otherwise returns None and the caller keeps streaming to the
    user's DM.

    Defensive: any error talking to sshsign (missing session, network
    problem, malformed response) returns None rather than raising, so a
    momentary sshsign hiccup never prevents the user from seeing their
    own DM stream.
    """
    if not session_id:
        return None
    client = session_client or SshsignSession(
        host=os.environ.get("SSHSIGN_HOST", "sshsign.dev"),
    )
    try:
        sess = client.get_session(session_id=session_id)
    except (SshsignSessionError, Exception):  # noqa: BLE001 — defensive
        return None
    raw = sess.get("group_chat_id")
    if raw in (None, 0, "0", ""):
        return None
    try:
        as_int = int(raw)
    except (TypeError, ValueError):
        return None
    if as_int == 0:
        return None
    return str(as_int)


# Lightweight regex used for fast role pre-classification before the
# slow Claude call. Mirrors _BIND_CODE_RE; deliberately conservative
# (requires the INV- prefix). Used only to reject obvious wrong-bot
# requests early; the parsed `role` field from extract_constraints is
# still the authoritative post-parse signal.
_INVESTOR_HINT_RE = re.compile(
    r"\b(?:INV-[A-Z0-9]+|join(?:ing)?\s+(?:as|the)?\s*invest|as\s+investor)\b",
    re.IGNORECASE,
)


def _classify_bot_role() -> str | None:
    """Return the bot's configured role per env, or None if unset.

    NEGOTIATE_SAFE_BOT_ROLE is the explicit operator-set knob:
      "founder"  → only founder-shaped requests accepted on this bot
      "investor" → only investor-shaped requests accepted
      unset / "either" → no enforcement (test default)

    Falls back to inference from FOUNDER_NAME / INVESTOR_NAME for
    droplets that haven't set the explicit knob yet:
      FOUNDER_NAME set, INVESTOR_NAME unset → founder
      INVESTOR_NAME set, FOUNDER_NAME unset → investor
      both set or both unset → None (no enforcement)
    """
    explicit = (os.environ.get("NEGOTIATE_SAFE_BOT_ROLE") or "").strip().lower()
    if explicit in ("founder", "investor"):
        return explicit
    if explicit:
        # Anything else (including the explicit "either") means
        # operator chose to disable enforcement; do NOT fall through
        # to env-name inference. Falling through caused tests to
        # accidentally inherit "founder" from conftest's FOUNDER_NAME
        # default.
        return None

    # No explicit knob — fall back to inferring from identity env.
    has_f = bool((os.environ.get("FOUNDER_NAME") or "").strip())
    has_i = bool((os.environ.get("INVESTOR_NAME") or "").strip())
    if has_f and not has_i:
        return "founder"
    if has_i and not has_f:
        return "investor"
    return None


def _enforce_bot_role_pre_parse(message: str) -> str | None:
    """Fast regex-only role check that runs BEFORE parse_constraints
    so an obvious mismatch (investor-shaped message to founder bot or
    vice versa) gets rejected instantly without burning a Claude
    round-trip.

    Returns:
      None if no enforcement needed (mode unset, or message matches bot's role)
      A short human-friendly error string explaining the rejection
    """
    bot_role = _classify_bot_role()
    if bot_role is None:
        return None  # no enforcement configured

    looks_investor = bool(_INVESTOR_HINT_RE.search(message))

    if looks_investor and bot_role == "founder":
        return (
            "⛔ This bot represents the FOUNDER side.\n\n"  # ⛔
            "Joining a negotiation as the investor goes through the "
            "investor bot. DM @AgenticPOAInvestor_bot with your join "
            "message instead."
        )
    if not looks_investor and bot_role == "investor":
        return (
            "⛔ This bot represents the INVESTOR side.\n\n"  # ⛔
            "To start a new negotiation as the founder, DM "
            "@AgenticPOA_bot. To join an existing one as the investor, "
            "include the INV-XXXXX code in your message here."
        )
    return None


def _enforce_bot_role_post_parse(parsed_role: str) -> str | None:
    """After-parse double-check: parse_constraints might infer a role
    that's contradictory to the bot's configured role even when the
    pre-parse regex didn't catch it. Returns error string or None.
    """
    bot_role = _classify_bot_role()
    if bot_role is None:
        return None
    parsed = (parsed_role or "").strip().lower()
    if parsed and parsed != bot_role:
        if bot_role == "founder":
            return (
                "⛔ This bot represents the FOUNDER side, but your "  # ⛔
                "request reads as an INVESTOR action. DM "
                "@AgenticPOAInvestor_bot instead."
            )
        return (
            "⛔ This bot represents the INVESTOR side, but your "  # ⛔
            "request reads as a FOUNDER action. DM @AgenticPOA_bot instead."
        )
    return None


def _has_active_negotiation() -> tuple[bool, str | None]:
    """Detect whether this droplet already has an in-flight negotiation
    that should block a new one. Two signals:

    1. State pointers: any pointer that maps to a non-terminal sshsign
       session (status in {open, joined}) is an active two-party
       negotiation. Stale pointers (terminal session) are quietly
       cleaned by the next scan tick; we tolerate them here.
    2. ``.session.pid`` in /tmp/safe_negotiate: a still-running
       background process from a prior demo or two-party stream that
       hasn't exited yet.

    Returns ``(True, descriptor)`` to block a new mint, or
    ``(False, None)`` to allow it. The descriptor is human-friendly
    and suitable for inclusion in the rejection card.

    Network/sshsign errors fall through as "no block" — we'd rather
    let a fresh mint proceed than wedge the user behind a transient
    transport failure.
    """
    # Check two-party state pointers first.
    try:
        pointers = state_store.list_active()
    except Exception:  # noqa: BLE001
        pointers = []
    if pointers:
        try:
            client = SshsignSession(host=os.environ.get("SSHSIGN_HOST", "sshsign.dev"))
        except Exception:  # noqa: BLE001
            client = None
        for state in pointers:
            try:
                if client is None:
                    raise SshsignSessionError("no client")
                sess = client.get_session(
                    session_id=_sshsign_session_id(state["negotiation_id"]),
                )
                status = (sess.get("status") or "").lower()
                if status in ("open", "joined"):
                    return (True, state.get("session_code") or state["negotiation_id"])
            except SshsignSessionError:
                # Couldn't reach sshsign — don't block on uncertain state;
                # a stale pointer for a terminal session shouldn't trap
                # the user out of new mints.
                continue

    # Check demo-mode PID file.
    pid_path = Path("/tmp/safe_negotiate/.session.pid")
    try:
        pid_text = pid_path.read_text().strip()
        pid = int(pid_text)
        os.kill(pid, 0)  # raises if process is gone
        return (True, "a running negotiation")
    except (OSError, ValueError, FileNotFoundError):
        pass
    return (False, None)


def run_prepare(
    message: str,
    output_dir: str,
    founder_name: str = "",
    founder_title: str = "CEO",
    chat_id_flag: str | None = None,
    sender=send_telegram,
) -> int:
    """Parse NL constraints, write config.json, and push the confirm card to chat.

    The confirm card uses format_event.py so the copy lives in one place and
    the model never generates UI itself.
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # Resolve chat_id first so we can push an interstitial before the slow parse.
    chat_id = resolve_chat_id(chat_id_flag)

    # Gate 1: bot-role pre-check. Reject obviously wrong-bot requests
    # (investor-shaped to founder bot or vice versa) BEFORE we burn a
    # Claude round-trip on parse_constraints. The regex is conservative;
    # the post-parse check below is the authoritative backstop.
    role_err = _enforce_bot_role_pre_parse(message)
    if role_err:
        if chat_id:
            sender(chat_id, message=role_err)
        return 1

    # Gate 2: single-active-negotiation. Each bot+droplet handles one
    # negotiation at a time. If there's already an in-flight session
    # (two-party state pointer + non-terminal sshsign status, OR a live
    # .session.pid from demo mode), refuse the new mint cleanly and
    # tell the user how to free the slot.
    has_active, descriptor = _has_active_negotiation()
    if has_active:
        body = format_event({
            "type": "active_negotiation_block",
            "descriptor": descriptor,
        })
        if chat_id and body:
            sender(chat_id, message=body)
        return 1

    # First-run guard: if the user hasn't configured their identity yet,
    # stash their negotiation message and ask for identity before parsing.
    # They'll reply with "I'm Name, Title at Company" → run_setup picks up
    # the stashed message and continues the flow automatically.
    if not _identity_configured():
        try:
            IDENTITY_SENTINEL_PATH.parent.mkdir(parents=True, exist_ok=True)
            IDENTITY_SENTINEL_PATH.write_text(message)
        except OSError as e:
            sys.stderr.write(f"Could not stash pending message: {e}\n")
        if chat_id:
            sender(chat_id, message=(
                "\U0001f44b Welcome! Before we negotiate, tell me who you are.\n\n"  # 👋
                "Reply with a short self-intro, for example:\n"
                "• \"I'm Juan Figuera, CEO of APOA Inc\" (founder)\n"
                "• \"Mark Stone, partner at Blue Fund\" (investor)\n\n"
                "I'll remember it for next time."
            ))
        return 2

    if chat_id:
        sender(chat_id, message="\u23f3 Analyzing\u2026")  # ⏳

    # Keep the typing indicator alive during the Anthropic call and the rest
    # of prepare. Stopped in `finally` so the confirm card isn't competing
    # with a lingering "typing" status.
    typing = TypingLoop(chat_id=chat_id or "", bot_token=get_bot_token())
    typing.start()

    try:
        try:
            constraints = extract_constraints(message)
        except (ValueError, RuntimeError) as e:
            sys.stderr.write(f"Parse error: {e}\n")
            if chat_id:
                sender(chat_id, message=(
                    "\u26a0\ufe0f Couldn't parse your request. "  # ⚠️
                    "Please rephrase your terms and try again."
                ))
            return 1

        required_fields = ("valuation_cap_min", "valuation_cap_max", "discount_min", "pro_rata", "mfn")
        missing = [f for f in required_fields if constraints.get(f) is None]
        if missing:
            sys.stderr.write(f"Ambiguous constraints (null values): {missing}. Ask the user to clarify.\n")
            return 1

        # Gate 3: bot-role post-parse backstop. The regex pre-check
        # catches the obvious cases; this catches subtler classifier
        # outputs (e.g., parse_constraints inferring role=investor from
        # phrasing the regex missed). Critical privacy gate — without
        # it, a wrong-role message produces a confirm card revealing
        # the OTHER party's constraints in this party's chat.
        role_err = _enforce_bot_role_post_parse(constraints.get("role") or "")
        if role_err:
            if chat_id:
                sender(chat_id, message=role_err)
            return 1

        # Two-party join path: the investor pasted a session_code. Validate
        # the session via sshsign, merge founder-side metadata into
        # constraints, and stash the shared session_id in config so mint
        # can reuse it instead of generating a fresh one.
        joined_session: dict | None = None
        if constraints.get("session_code"):
            sess_payload, err = _fetch_session_for_join(constraints["session_code"])
            if err or not sess_payload:
                if chat_id:
                    sender(chat_id, message=(
                        "\u26a0\ufe0f " + (err or "Couldn't find that negotiation.")  # ⚠️
                        + "\n\nDouble-check the code with your counterparty."
                    ))
                return 1
            constraints = _enrich_constraints_from_session(constraints, sess_payload)
            joined_session = sess_payload

        config = {
            "constraints": constraints,
            # Legacy field; run_mint reads identity from constraints +
            # FOUNDER_*/INVESTOR_*/COMPANY_NAME env per upstream convention.
            "founder_name": founder_name or os.environ.get("FOUNDER_NAME") or "Founder",
            "founder_title": founder_title,
            "message": message,
        }
        if joined_session:
            config["session"] = {
                "session_id": joined_session.get("session_id"),
                "session_code": joined_session.get("session_code"),
                "status": joined_session.get("status"),
                # Save the founder's APOA pubkey (if we got it in members)
                # so mint can write it to disk without a second sshsign call.
                "counterparty_apoa_pubkey_pem": _extract_counterparty_pubkey(joined_session),
            }

        if chat_id:
            body = format_event({"type": "confirm", "constraints": constraints})
            if body:
                sender(chat_id, message=body)
        else:
            sys.stderr.write(
                "Warning: no chat_id resolvable; skipping confirm push. "
                "The model will need to relay constraints manually.\n"
            )
        (out / "config.json").write_text(json.dumps(config, indent=2))

        sys.stdout.write(json.dumps(constraints, indent=2) + "\n")
        return 0
    finally:
        typing.stop()


def _extract_counterparty_pubkey(session_payload: dict) -> str:
    """Return the first member's APOA pubkey PEM that isn't the caller's own.

    get-session returns the members list only when the caller is a member;
    for a prospective joiner (not yet a member) the list is empty and we
    rely on join-session + a follow-up get-session to retrieve it. In that
    case this returns '' and mint writes a placeholder that _stream_negotiate
    can refresh later.
    """
    members = session_payload.get("members") or []
    for m in members:
        pem = m.get("apoa_pubkey_pem") or ""
        if pem:
            return pem
    return ""


def run_mint(output_dir: str, config: dict, telegram_user_id: int | None = None) -> int:
    """Mint APOA tokens using the constraints from config.json.

    telegram_user_id, when provided (founder two-party path), is stashed on
    the sshsign session's metadata_member so the Phase 8 /bind handler can
    verify that the caller of /bind matches the session creator.
    """
    repo = os.environ.get("NEGOTIATE_REPO_PATH", "")
    if not repo:
        sys.stderr.write("NEGOTIATE_REPO_PATH not set.\n")
        return 2

    repo = Path(repo).resolve()
    if not (repo / "create_tokens.py").exists():
        sys.stderr.write(f"create_tokens.py not found under {repo}\n")
        return 2

    constraints = config["constraints"]
    out = Path(output_dir)

    # Join mode: the investor is entering a session the founder already
    # created. Reuse the shared session_id as the negotiation_id so both
    # sides' APOA tokens, pending_signatures, and offer logs correlate.
    shared_session = config.get("session")
    if shared_session and shared_session.get("session_id"):
        # Sshsign's get-session returns the prefixed form; strip it so
        # upstream, create_tokens.py, and our on-disk layout all use
        # the same raw negotiation_id as the founder's side.
        negotiation_id = _negotiation_id_from_sshsign_session_id(
            shared_session["session_id"]
        )
    else:
        negotiation_id = f"neg_{uuid.uuid4().hex[:12]}"
    neg_dir = repo / "negotiations" / negotiation_id
    neg_dir.mkdir(parents=True, exist_ok=True)

    expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=int(os.environ.get("NEGOTIATION_TTL", "3600"))
    )
    expires_str = expires_at.strftime("%Y-%m-%dT%H:%M:%SZ")

    pro_rata_required = constraints.get("pro_rata") == "required"
    mfn_required = constraints.get("mfn") == "required"
    discount_min = float(constraints.get("discount_min", 0.20))
    discount_max = discount_min + 0.10

    # `get(k, default)` returns default only when the key is MISSING; the
    # parser emits null for unknown fields (key-present, value-None), which
    # would leak through and break subprocess.run. Use `or default` to catch both.
    amount = constraints.get("investment_amount") or 500_000.0

    # Dual-role: user's constraints bind whichever side the user is playing.
    # The OTHER side's constraints come from env-var defaults so the AI
    # opposing agent has sensible negotiating bounds.
    user_role = (constraints.get("role") or "founder").lower()
    if user_role not in ("founder", "investor"):
        user_role = "founder"
    user_flag_prefix = "--founder-" if user_role == "founder" else "--investor-"
    ai_flag_prefix = "--investor-" if user_role == "founder" else "--founder-"
    ai_env_prefix = "INVESTOR_" if user_role == "founder" else "FOUNDER_"

    # Identity resolution — aligned with upstream agenticpoa/negotiate
    # `.env.example`. Precedence per field: NL > env > demo-preset > literal
    # fallback. Upstream convention: FOUNDER_* describes the founder side
    # (regardless of who the user is), INVESTOR_* describes the investor
    # side. In demo mode the user's own identity ends up in whichever set
    # matches their role; the OTHER side gets demo-flavored presets (e.g.
    # "Demo Investor, Demo Fund") so the AI counterparty has a name that
    # reads as intentional rather than placeholder-y in the HN screenshot.
    mode = (constraints.get("mode") or "demo").lower()
    is_demo = mode == "demo"

    # Demo presets apply only to the side the user ISN'T playing.
    demo_founder_name = "Demo Founder" if is_demo and user_role == "investor" else ""
    demo_founder_title = "CEO" if is_demo and user_role == "investor" else ""
    demo_investor_name = "Demo Investor" if is_demo and user_role == "founder" else ""
    demo_investor_firm = "Demo Capital" if is_demo and user_role == "founder" else ""

    founder_name = (
        constraints.get("founder_name")
        or os.environ.get("FOUNDER_NAME")
        or demo_founder_name
        or "Founder"
    )
    founder_title = (
        constraints.get("founder_title")
        or os.environ.get("FOUNDER_TITLE")
        or demo_founder_title
        or "CEO"
    )
    investor_name = (
        constraints.get("investor_name")
        or os.environ.get("INVESTOR_NAME")
        or demo_investor_name
        or "Investor"
    )
    investor_firm = (
        constraints.get("investor_firm")
        or os.environ.get("INVESTOR_FIRM")
        or demo_investor_firm
        or "Investor Firm"
    )
    company = (
        constraints.get("company_name")
        or os.environ.get("COMPANY_NAME")
        or "Company"
    )

    slug = "".join(c.lower() if c.isalnum() else "-" for c in company).strip("-")
    service = f"safe:{slug}:{negotiation_id}"

    cmd = [
        sys.executable, str(repo / "create_tokens.py"),
        "--negotiation-id", negotiation_id,
        "--principal-id", os.environ.get("USER_DID") or "did:apoa:default",
        "--expires", expires_str,
        "--service", service,
        "--company-name", company,
        "--founder-name", founder_name,
        "--founder-title", founder_title,
        "--investor-name", investor_name,
        "--investor-firm", investor_firm,
        "--investment-amount", str(amount),
        f"{user_flag_prefix}cap-min", str(constraints["valuation_cap_min"]),
        f"{user_flag_prefix}cap-max", str(constraints["valuation_cap_max"]),
        f"{user_flag_prefix}discount-min", str(constraints["discount_min"]),
        f"{user_flag_prefix}discount-max", str(discount_max),
        f"{user_flag_prefix}pro-rata-required", "true" if pro_rata_required else "false",
        f"{user_flag_prefix}mfn-required", "true" if mfn_required else "false",
        "--keys-dir", str(neg_dir / "keys"),
        "--tokens-dir", str(neg_dir / "tokens"),
        "--config-dir", str(neg_dir),
        "--create-keys",
    ]

    ai_flag_suffixes = {
        "CAP_MIN": "cap-min",
        "CAP_MAX": "cap-max",
        "DISCOUNT_MIN": "discount-min",
        "DISCOUNT_MAX": "discount-max",
        "PRO_RATA_REQUIRED": "pro-rata-required",
        "MFN_REQUIRED": "mfn-required",
    }

    # In join mode (shared_session set), the investor only mints their OWN
    # token — the founder already minted theirs on their OC. Pass --role to
    # create_tokens.py to skip the counterparty-token generation.
    # Skip the AI-side env overrides too — there is no AI side in two-party
    # mode; the counterparty's constraints live in their own APOA token.
    if shared_session:
        cmd.extend(["--role", user_role])
    else:
        for env_key_suffix, flag_suffix in ai_flag_suffixes.items():
            val = os.environ.get(f"{ai_env_prefix}{env_key_suffix}")
            if val:
                cmd.extend([f"{ai_flag_prefix}{flag_suffix}", val])

    result = subprocess.run(cmd, cwd=repo, capture_output=True, text=True)
    if result.returncode != 0:
        sys.stderr.write(f"Mint failed:\n{result.stdout}\n{result.stderr}\n")
        return result.returncode

    mint_output = {
        "negotiation_id": negotiation_id,
        "founder_config_path": str(neg_dir / "founder.json"),
        "investor_config_path": str(neg_dir / "investor.json"),
        "founder_token_path": str(neg_dir / "tokens" / "founder.jwt"),
        "investor_token_path": str(neg_dir / "tokens" / "investor.jwt"),
        "expires_at": expires_str,
        "service": service,
        "user_role": user_role,
        "mode": (constraints.get("mode") or "demo").lower(),
    }

    # Two-party mode: register (creator) or join (joiner) the signing
    # session with sshsign. Creator flow (no shared_session in config)
    # creates a fresh session and gets a shareable code. Joiner flow
    # publishes the joiner's APOA pubkey to the existing session.
    if mint_output["mode"] == "two_party":
        if shared_session:
            joined = _join_signing_session(
                mint_output=mint_output,
                shared_session=shared_session,
                user_role=user_role,
                neg_dir=neg_dir,
                repo=repo,
            )
            if joined is None:
                return 3
            mint_output.update(joined)
        else:
            session_registered = _register_signing_session(
                mint_output, constraints, user_role, neg_dir,
                telegram_user_id=telegram_user_id,
            )
            if session_registered is None:
                return 3
            mint_output.update(session_registered)

    (out / "mint.json").write_text(json.dumps(mint_output, indent=2))

    # P7-5: leave a pointer so a later cron-scanned `scan` turn can
    # resume this negotiation if OC reaps the founder's foreground
    # process before the investor joins. Founder-only (investor has
    # no resume flow) and two-party-only (demo mode runs inline).
    if mint_output["mode"] == "two_party" and user_role == "founder":
        session_code = mint_output.get("session_code")
        if session_code:
            try:
                state_store.write_state({
                    "negotiation_id": negotiation_id,
                    "output_dir": str(out),
                    "session_code": session_code,
                })
            except state_store.StateCorruptError as e:
                # Non-fatal: the founder can still wait inline this
                # turn; they just won't survive a reap. Surface the
                # reason so ops can fix the state dir.
                sys.stderr.write(f"state_store.write_state failed: {e}\n")
        # Install the global cron scan job (idempotent). Logged but
        # not fatal — if OC rejects --every 30s or the pairing wall
        # blocks us, the mint still succeeds and ops can install the
        # job manually.
        interval = os.environ.get("CLAW_NEGOTIATE_SCAN_INTERVAL", "30s")
        ok, err = ensure_cron(interval=interval)
        if not ok and err:
            sys.stderr.write(f"ensure_cron: {err}\n")

    sys.stdout.write(json.dumps({
        "type": "authorized",
        "negotiation_id": negotiation_id,
        "service": service,
        "expires_at": expires_str,
        "mode": mint_output["mode"],
        "session_code": mint_output.get("session_code"),
    }) + "\n")
    sys.stdout.flush()

    return 0


def _register_signing_session(
    mint_output: dict,
    constraints: dict,
    user_role: str,
    neg_dir: Path,
    session_client=None,
    telegram_user_id: int | None = None,
) -> dict | None:
    """Call sshsign.dev create-session for a two-party negotiation.

    Reads the user's APOA pubkey PEM from the minted keys on disk and
    publishes it + a minimal metadata envelope so the counterparty can
    retrieve it on join. Returns a dict of session fields to merge into
    mint.json, or None on error.
    """
    role_to_pubkey_path = {
        "founder": neg_dir / "keys" / "founder_public.pem",
        "investor": neg_dir / "keys" / "investor_public.pem",
    }
    pubkey_path = role_to_pubkey_path.get(user_role)
    if pubkey_path is None or not pubkey_path.exists():
        sys.stderr.write(
            f"APOA pubkey for role={user_role} not found at {pubkey_path}\n"
        )
        return None

    try:
        apoa_pubkey_pem = pubkey_path.read_text()
    except OSError as e:
        sys.stderr.write(f"reading {pubkey_path}: {e}\n")
        return None

    # company_name lives in metadata_public so a prospective investor can see
    # "you're joining a negotiation for Acme Corp" BEFORE committing to join
    # (join-session requires a PUT; pre-join the investor only has get-session
    # which hides metadata_member). Founder chose to share the code with this
    # investor, so the company identity is already public-between-them.
    metadata_public = {"use_case": "safe", "version": 1}
    if constraints.get("company_name"):
        metadata_public["company_name"] = constraints["company_name"]
    # Member-only metadata carries the display fields the joiner needs for
    # their confirm card + signing view (title, both sides' names/firms)
    # and the /bind ACL field (founder's Telegram user_id). Now that the
    # client sends base64-encoded JSON via --metadata-member-b64 (P8-2),
    # string values with spaces or inner quotes survive SSH argv — no
    # need to restrict to integer-only fields.
    metadata_member: dict = {}
    for field in (
        "founder_name", "founder_title",
        "investor_name", "investor_firm",
        "investment_amount",
    ):
        val = constraints.get(field)
        if val not in (None, ""):
            metadata_member[field] = val
    if telegram_user_id and user_role == "founder":
        metadata_member["telegram"] = {"founder_user_id": int(telegram_user_id)}

    client = session_client or SshsignSession(
        host=os.environ.get("SSHSIGN_HOST", "sshsign.dev"),
    )
    try:
        sess = client.create_session(
            session_id=_sshsign_session_id(mint_output["negotiation_id"]),
            role=user_role,
            apoa_pubkey_pem=apoa_pubkey_pem,
            party_did=os.environ.get("USER_DID") or None,
            metadata_public=metadata_public,
            metadata_member=metadata_member,
        )
    except SshsignSessionError as e:
        sys.stderr.write(f"create-session failed: {e}\n")
        return None

    return {
        "session_code": sess.get("session_code"),
        "session_created_at": sess.get("created_at"),
        "session_expires_at": sess.get("expires_at"),
        "session_status": sess.get("status"),
    }


def _wait_for_counterparty(
    session_id: str,
    session_code: str,
    chat_id: str,
    counterparty_label: str,
    sender=send_telegram,
    session_client=None,
    poll_interval: int = 10,
    max_wait_seconds: int = 24 * 60 * 60,  # matches session TTL; sshsign expires first
    typing_factory=None,
    sleep_fn=None,
    now_fn=None,
) -> str:
    """Poll sshsign for the counterparty's join. Returns the final status:
      'joined' — success; caller proceeds to run_distributed
      'expired' — invitation TTL hit; session unusable
      'canceled' — founder canceled; exit cleanly
      'error' — persistent ssh errors; caller surfaces to user

    Periodic waiting cards are pushed at elapsed milestones (5, 15, 60, 180,
    600 minutes) so the user knows we're still alive without being spammed.
    Typing loop runs continuously to keep the UI feeling active.
    """
    if sleep_fn is None:
        import time as _time
        sleep_fn = _time.sleep
    if now_fn is None:
        import time as _time
        now_fn = _time.time

    client = session_client or SshsignSession(
        host=os.environ.get("SSHSIGN_HOST", "sshsign.dev"),
    )

    if typing_factory is None:
        typing = TypingLoop(chat_id=chat_id, bot_token=get_bot_token())
    else:
        typing = typing_factory(chat_id)
    typing.start()

    start = now_fn()
    milestones_minutes = [5, 15, 60, 180, 600]
    fired_milestones: set[int] = set()

    try:
        while True:
            elapsed = now_fn() - start
            if elapsed > max_wait_seconds:
                return "expired"

            try:
                sess = client.get_session(session_id=session_id)
            except SshsignSessionError as e:
                # Transient ssh errors happen (network blips); retry.
                sys.stderr.write(f"get-session transient error: {e}\n")
                sleep_fn(poll_interval)
                continue

            status = (sess.get("status") or "").lower()
            if status == "joined":
                body = format_event({
                    "type": "counterparty_joined",
                    "counterparty_label": counterparty_label,
                })
                if body:
                    sender(chat_id, message=body)
                return "joined"
            if status in ("canceled", "rescinded_after_sign"):
                return "canceled"
            if status == "expired":
                return "expired"

            # Still open — push a waiting card at milestones.
            elapsed_min = int(elapsed / 60)
            for milestone in milestones_minutes:
                if elapsed_min >= milestone and milestone not in fired_milestones:
                    fired_milestones.add(milestone)
                    remaining_hours = (max_wait_seconds - elapsed) / 3600
                    body = format_event({
                        "type": "waiting",
                        "elapsed_minutes": elapsed_min,
                        "remaining_hours": remaining_hours,
                    })
                    if body:
                        sender(chat_id, message=body)
                    break

            sleep_fn(poll_interval)
    finally:
        typing.stop()


def _ssh_history(
    negotiation_id: str,
    sshsign_host: str = "sshsign.dev",
    runner=subprocess.run,
) -> list[dict] | None:
    """Return sshsign's `history --negotiation-id` rows, or None on error.

    Shape (per commands.go:1128): a JSON array of
        {round, from, type, metadata, previous_tx, audit_tx_id, created_at}
    Rows with a non-list response or a non-zero exit are treated as "no data
    yet" and surface as None — the poller re-tries on the next interval.
    """
    try:
        result = runner(
            ["ssh", sshsign_host, "history", "--negotiation-id", negotiation_id],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if result.returncode != 0 or not (result.stdout or "").strip():
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, list) else None


def _synthesize_offer_event(entry: dict) -> dict | None:
    """Translate a sshsign history row into an upstream-compatible event.

    Upstream's `run_local` emits `offer` / `counter` / `accept` NDJSON
    events on stdout via emit_json_event. `run_distributed` currently
    does not (only the final `signing` event leaks through). P8-1:
    reconstruct those events from sshsign's authoritative
    `negotiation_offers` log so the group sees rounds live.

    Upstream writes {**offer.terms, "_message": offer.message} into the
    metadata column (negotiate.py:936), so we pop `_message` out and
    keep the rest as `terms`.

    Returns None for rows we can't render (unknown type, missing round).
    """
    if not isinstance(entry, dict):
        return None
    etype = entry.get("type")
    if etype not in ("offer", "counter", "accept"):
        return None
    try:
        round_num = int(entry.get("round", 0))
    except (TypeError, ValueError):
        return None
    if round_num <= 0:
        return None
    party = entry.get("from") or ""
    if party not in ("founder", "investor"):
        return None

    raw_meta = entry.get("metadata")
    terms: dict = {}
    message = ""
    if isinstance(raw_meta, str) and raw_meta.strip():
        try:
            parsed = json.loads(raw_meta)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            message = str(parsed.pop("_message", "") or "")
            terms = parsed
    elif isinstance(raw_meta, dict):
        meta_copy = dict(raw_meta)
        message = str(meta_copy.pop("_message", "") or "")
        terms = meta_copy

    return {
        "type": etype,
        "party": party,
        "round": round_num,
        "terms": terms,
        "message": message,
    }


def _augment_signing_url(event: dict, bot_username: str) -> dict:
    """Append a bare Telegram deep-link callback to the signing event's approval_url.

    After signing, the browser redirects to `https://t.me/<bot>` which opens the
    Telegram chat without sending any message (for returning users — which the
    user always is at this point, since they just invoked the skill). The script
    is already polling get-envelope and will push the signed confirmation and PDF
    autonomously; this callback just brings the user back to the chat window.
    """
    url = (event.get("approval_url") or "").strip()
    if not url or not bot_username:
        return event
    callback = urllib.parse.quote(f"https://t.me/{bot_username}")
    sep = "&" if "?" in url else "?"
    return {**event, "approval_url": f"{url}{sep}callback={callback}"}


def _stream_to_telegram(
    output_dir: Path,
    chat_id: str,
    constraints: dict | None,
    bot_username: str,
    stream_helper: Path = STREAM_HELPER,
    sender=send_telegram,
    popen=subprocess.Popen,
    typing_factory=None,
    group_chat_id: str | None = None,
    dm_sender=send_signing_url_to_dm,
    negotiation_id: str | None = None,
    sshsign_host: str = "sshsign.dev",
    history_fn=None,
    history_interval: float = 2.0,
) -> tuple[int, dict | None]:
    """Spawn the streaming helper; push each event to Telegram as it fires.

    Target routing (Phase 8 K2):
      - `chat_id` is the user's DM (always required). When there is no
        bound group, everything goes to chat_id (Phase 7 behavior).
      - `group_chat_id`, when set, is the Telegram group the session is
        bound to (via K0/K1 /bind). All stream-of-consciousness events
        (rounds, outcome, propose_new_terms, signed) go to the GROUP so
        both parties watch live.
      - The SIGNING URL is the exception: it always goes to the DM via
        the `send_signing_url_to_dm` primitive. Structural privacy —
        the primitive's type signature refuses a group target. The group
        receives a placeholder ("⏳ [Party] signing — check your DM")
        so both observers see the state without the URL leaking.

    P8-1: when `negotiation_id` is set, a background thread polls
    sshsign's authoritative offer log via `history --negotiation-id`
    every `history_interval` seconds and synthesizes offer/counter/
    accept events to supplement upstream's stdout stream. Necessary
    because `run_distributed` only emits the final `signing` event —
    without this poll the group never sees per-round cards. Events
    are deduped by (type, round, party) so events that DO come through
    stdout (e.g. in demo mode) aren't doubled. `history_fn` is
    injectable for testing.

    Returns 0 on clean exit, the subprocess returncode otherwise.
    `sender`, `popen`, and `dm_sender` are injectable for testing.
    """
    import threading

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    cmd = [sys.executable, "-u", str(stream_helper), "--output-dir", str(output_dir)]

    stream_target = group_chat_id or chat_id

    if typing_factory is None:
        typing = TypingLoop(chat_id=stream_target, bot_token=get_bot_token())
    else:
        typing = typing_factory(stream_target)
    typing.start()

    events: list[dict] = []
    seen: set[tuple[str, int, str]] = set()
    emit_lock = threading.Lock()

    def _emit(event: dict) -> None:
        """Format + route + record. Dedups offer/counter/accept by
        (type, round, party) so the stdout + history poller can run
        concurrently without double-posting."""
        etype = event.get("type")
        if etype in ("offer", "counter", "accept"):
            try:
                key = (etype, int(event.get("round", 0)), event.get("party") or "")
            except (TypeError, ValueError):
                return
            with emit_lock:
                if key in seen:
                    return
                seen.add(key)
                events.append(event)
        else:
            with emit_lock:
                events.append(event)

        if etype == "signing":
            event = _augment_signing_url(event, bot_username)

        message = format_event(event, constraints=constraints)
        if message:
            if etype == "signing":
                try:
                    dm_sender(chat_id, message=message)
                except SigningUrlTargetError:
                    sys.stderr.write(
                        f"stream: refusing to send signing URL to "
                        f"non-DM target chat_id={chat_id!r}\n"
                    )
                if group_chat_id:
                    placeholder = (
                        "⏳ Signing… check your own DM for the "  # ⏳
                        "signing link. Never share this link."
                    )
                    sender(stream_target, message=placeholder)
            else:
                sender(stream_target, message=message)

        if etype == "outcome" and event.get("result") == "max_rounds":
            cp_label = _counterparty_label_from_constraints(constraints or {})
            follow = format_event({
                "type": "propose_new_terms",
                "counterparty_label": cp_label,
            })
            if follow:
                sender(stream_target, message=follow)

    def _drain_history() -> None:
        if history_fn is not None:
            rows = history_fn()
        elif negotiation_id:
            rows = _ssh_history(negotiation_id, sshsign_host=sshsign_host)
        else:
            rows = None
        if not isinstance(rows, list):
            return
        for row in rows:
            synthesized = _synthesize_offer_event(row)
            if synthesized:
                try:
                    _emit(synthesized)
                except Exception as e:  # never let a render error kill the poller
                    sys.stderr.write(f"stream: history emit failed: {e}\n")

    try:
        proc = popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )

        stop_event = threading.Event()
        poller: threading.Thread | None = None
        if negotiation_id or history_fn is not None:
            def _poll() -> None:
                while not stop_event.is_set():
                    try:
                        _drain_history()
                    except Exception as e:
                        sys.stderr.write(f"stream: history poll failed: {e}\n")
                    stop_event.wait(history_interval)

            poller = threading.Thread(target=_poll, daemon=True)
            poller.start()

        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            _emit(event)

        proc.wait()

        if poller is not None:
            # One final drain: upstream emits `signing` on stdout before the
            # last offer has necessarily propagated through sshsign's log.
            # Catch anything that arrived in the window between the last
            # poll and subprocess exit.
            try:
                _drain_history()
            except Exception as e:
                sys.stderr.write(f"stream: final history drain failed: {e}\n")
            stop_event.set()
            poller.join(timeout=history_interval + 1.0)
    finally:
        typing.stop()

    try:
        (output_dir / "events.ndjson").write_text(
            "\n".join(json.dumps(e) for e in events) + "\n"
        )
    except OSError:
        pass

    signing_event = next((e for e in events if e.get("type") == "signing"), None)
    return proc.returncode or 0, signing_event


def _counterparty_label_from_constraints(constraints: dict) -> str:
    """Human label for the counterparty, used in propose-new-terms follow-up.

    From the user's perspective: if they're the founder, label is the
    investor's name/firm; if investor, it's the founder + company.
    Returns empty string when we can't build a useful label.
    """
    role = (constraints.get("role") or "founder").lower()
    if role == "founder":
        parts = [p for p in (
            constraints.get("investor_name"),
            constraints.get("investor_firm"),
        ) if p]
        return ", ".join(parts)
    parts = [p for p in (
        constraints.get("founder_name"),
        constraints.get("company_name"),
    ) if p]
    return ", ".join(parts)


# ─── Post-stream: envelope polling + finalize + PDF push ────────────────────


def _ssh_envelope_status(
    pending_id: str,
    sshsign_host: str = "sshsign.dev",
    runner=subprocess.run,
) -> str | None:
    """Return the envelope status ('approved' / 'pending' / other) or None on error."""
    try:
        result = runner(
            ["ssh", sshsign_host, "get-envelope", "--id", pending_id],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        return str(json.loads(result.stdout).get("status", "")) or None
    except json.JSONDecodeError:
        return None


def _poll_envelope_approval(
    pending_id: str,
    sshsign_host: str = "sshsign.dev",
    timeout: int = 900,
    interval: int = 5,
    status_fn=_ssh_envelope_status,
    sleep_fn=None,
) -> bool:
    """Poll until envelope is approved or timeout elapses. True = approved."""
    if sleep_fn is None:
        import time as _time
        sleep_fn = _time.sleep

    elapsed = 0
    while elapsed < timeout:
        status = status_fn(pending_id, sshsign_host)
        if status == "approved":
            return True
        sleep_fn(interval)
        elapsed += interval
    return False


def _ssh_session_status(
    session_id: str,
    sshsign_host: str = "sshsign.dev",
    runner=subprocess.run,
) -> str | None:
    """Return the aggregate session status from `ssh host session --id <id>`.

    sshsign reports 'complete' when every pending in the session is
    `approved`, 'failed' if any is `denied`, 'pending' otherwise. Used by
    the P8-3 both-signed gate so the creator waits until the counterparty
    has also signed before running finalize.
    """
    try:
        result = runner(
            ["ssh", sshsign_host, "session", "--id", session_id],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if result.returncode != 0 or not (result.stdout or "").strip():
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    status = payload.get("status") if isinstance(payload, dict) else None
    return str(status) if status else None


def _poll_session_complete(
    session_id: str,
    sshsign_host: str = "sshsign.dev",
    timeout: int = 900,
    interval: int = 5,
    status_fn=_ssh_session_status,
    sleep_fn=None,
) -> bool:
    """Poll until the session aggregates to 'complete' (all pendings approved).

    Returns True on complete, False on timeout or 'failed' (a denial is
    treated as a hard stop — no point waiting longer). The default
    interval of 5s matches _poll_envelope_approval so a creator's wait
    for its own sig + the P8-3 wait for the counterparty have the same
    cadence.
    """
    if sleep_fn is None:
        import time as _time
        sleep_fn = _time.sleep

    elapsed = 0
    while elapsed < timeout:
        status = status_fn(session_id, sshsign_host)
        if status == "complete":
            return True
        if status == "failed":
            return False
        sleep_fn(interval)
        elapsed += interval
    return False


def _finalize_executed_pdf(
    output_dir: Path,
    pending_id: str,
    sshsign_host: str,
) -> Path | None:
    """Call upstream's run_finalize via importlib; return executed PDF path if generated.

    Bypasses upstream main()/auto_setup() by invoking run_finalize directly with
    a fully-constructed Namespace built from our mint.json + config files.
    """
    import importlib.util

    repo_str = os.environ.get("NEGOTIATE_REPO_PATH", "")
    if not repo_str:
        return None
    repo = Path(repo_str).resolve()

    mint = json.loads((output_dir / "mint.json").read_text())

    # In two-party join mode only the joiner's config exists locally; the
    # counterparty's was minted on the other OC. Load what exists, fall
    # back to {} so we can stitch kwargs from env / constraints below.
    def _maybe_load_cfg(path_field: str) -> dict:
        path = mint.get(path_field, "")
        if not path:
            return {}
        try:
            return json.loads(Path(path).read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    f_cfg = _maybe_load_cfg("founder_config_path")
    i_cfg = _maybe_load_cfg("investor_config_path")
    neg_id = mint["negotiation_id"]
    config_anchor = mint.get("founder_config_path") or mint.get("investor_config_path") or ""
    neg_dir = Path(config_anchor).parent if config_anchor else output_dir
    neg_output = neg_dir / "output"

    spec = importlib.util.spec_from_file_location("negotiate_upstream_fin", repo / "negotiate.py")
    if spec is None or spec.loader is None:
        return None
    sys.path.insert(0, str(repo))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        return None

    user_role = mint.get("user_role", "founder")

    # Load the user's own prepared constraints so we can fall back to
    # NL-derived values (or env) for any identity field the local cfg
    # doesn't have — the joiner doesn't mint the counterparty's config.
    try:
        constraints = json.loads((output_dir / "config.json").read_text()).get("constraints") or {}
    except (OSError, json.JSONDecodeError):
        constraints = {}

    def _pick(cfg_keys, constraint_key, env_key, default=""):
        for cfg, k in cfg_keys:
            if cfg.get(k):
                return cfg[k]
        if constraints.get(constraint_key):
            return constraints[constraint_key]
        return os.environ.get(env_key) or default

    # Counterparty pubkey path: when joining, _join_signing_session
    # writes the founder's pubkey to neg_dir/keys/founder_public.pem
    # (stashed in mint.counterparty_pubkey_path). Fall back to that.
    counterparty_pubkey = mint.get("counterparty_pubkey_path", "")

    founder_pubkey = f_cfg.get("pubkey") or (
        counterparty_pubkey if user_role == "investor" else ""
    )
    investor_pubkey = i_cfg.get("pubkey") or (
        counterparty_pubkey if user_role == "founder" else ""
    )

    kwargs = dict(
        negotiate_repo=repo,
        negotiation_id=neg_id,
        founder_token_path=mint.get("founder_token_path", ""),
        investor_token_path=mint.get("investor_token_path", ""),
        founder_pubkey_path=founder_pubkey,
        investor_pubkey_path=investor_pubkey,
        company_name=_pick(
            [(f_cfg, "company_name")], "company_name", "COMPANY_NAME", "Company",
        ),
        founder_name=_pick(
            [(f_cfg, "name"), (f_cfg, "founder_name")],
            "founder_name", "FOUNDER_NAME", "Founder",
        ),
        founder_title=_pick(
            [(f_cfg, "title")], "founder_title", "FOUNDER_TITLE", "",
        ),
        investor_name=_pick(
            [(i_cfg, "name"), (i_cfg, "investor_name")],
            "investor_name", "INVESTOR_NAME", "Investor",
        ),
        investor_firm=_pick(
            [(i_cfg, "firm")], "investor_firm", "INVESTOR_FIRM", "",
        ),
        investment_amount=(
            f_cfg.get("investment_amount")
            or i_cfg.get("investment_amount")
            or constraints.get("investment_amount")
            or 500_000.0
        ),
        sshsign_host=sshsign_host,
        output_dir=str(neg_output),
        signing_key_id=(
            f_cfg.get("founder_signing_key_id")
            or i_cfg.get("investor_signing_key_id")
            or f_cfg.get("signing_key_id")
            or i_cfg.get("signing_key_id", "")
        ),
        founder_signing_key_id=(
            f_cfg.get("founder_signing_key_id") or f_cfg.get("signing_key_id", "")
        ),
        investor_signing_key_id=(
            i_cfg.get("investor_signing_key_id") or i_cfg.get("signing_key_id", "")
        ),
        json_events=False,
        poll=False,
    )
    import dataclasses
    if "signer_role" in {f.name for f in dataclasses.fields(module.NegotiationConfig)}:
        kwargs["signer_role"] = user_role
    config = module.NegotiationConfig(**kwargs)

    ns = config.to_namespace()
    ns.finalize = pending_id

    original_cwd = os.getcwd()
    os.chdir(str(repo))
    try:
        module.run_finalize(ns)
    except Exception as e:
        sys.stderr.write(f"Finalize error: {e}\n")
        return None
    finally:
        os.chdir(original_cwd)

    executed = neg_output / f"{neg_id}_executed.pdf"
    return executed if executed.exists() else None


def _is_still_active_session(output_dir: Path) -> bool:
    """Check if this process is still the active negotiation for this output dir.

    When a new `run_safe.py negotiate` starts, it overwrites `.session.pid`
    with its own PID. A previous process whose long poll later times out
    should silently exit rather than push a stale timeout message into the
    chat (that was bug #4 in the PR-A feedback — the 15-min timeout from a
    prior aborted negotiation posted into a new session).
    """
    try:
        pid_file = output_dir / ".session.pid"
        return int(pid_file.read_text().strip()) == os.getpid()
    except (OSError, ValueError):
        # If the file is missing or unreadable, assume we're still active —
        # better to occasionally send a timeout message than suppress a
        # legitimate one.
        return True


def _await_sign_and_push(
    output_dir: Path,
    chat_id: str,
    sshsign_host: str,
    pending_id: str,
    timeout: int = 900,
    poll_interval: int = 5,
    sender=send_telegram,
    poll_fn=_poll_envelope_approval,
    finalize_fn=_finalize_executed_pdf,
    is_active_fn=_is_still_active_session,
    typing_factory=None,
    group_chat_id: str | None = None,
    pre_finalize_wait_fn=None,
) -> int:
    """Wait for signature, finalize PDF, push confirmation + attachment.

    P8-3: when `pre_finalize_wait_fn` is provided, it runs AFTER the
    caller's own sig lands and BEFORE `finalize_fn`. For the creator
    path this gates on "session aggregates to complete (both parties
    signed)", preventing the race where the founder finalized
    immediately after their own sig and produced a partial PDF that
    got posted to the group. The fn must return True when the gate
    passes; returning False causes the flow to post a "counterparty
    not signed yet" note and exit with rc=4.

    Returns 0 if the user received the executed PDF, 1 on timeout, 2 on
    finalize failure, 3 if this process was superseded by a newer session,
    4 if the pre-finalize wait timed out (own sig landed but counterparty
    didn't sign within the window).
    """
    # Typing covers the gap between "Almost done — sign here" and
    # "Signed ✓" or the eventual PDF generation. Follows the visible
    # venue — status cards go there, so the typing indicator belongs there.
    ui_target = group_chat_id or chat_id
    if typing_factory is None:
        typing = TypingLoop(chat_id=ui_target, bot_token=get_bot_token())
    else:
        typing = typing_factory(ui_target)
    typing.start()

    try:
        approved = poll_fn(
            pending_id=pending_id,
            sshsign_host=sshsign_host,
            timeout=timeout,
            interval=poll_interval,
        )
        if not approved:
            if not is_active_fn(output_dir):
                return 3
            sender(chat_id, message=(
                "Signature not received after waiting.\n"
                'When you sign, reply "signed" and I\'ll verify manually.'
            ))
            return 1

        if not is_active_fn(output_dir):
            return 3

        # Confirm signature receipt immediately, then let the user know the
        # PDF is coming. Finalize (upstream pdf generation) takes a few
        # seconds; posting "Generating executed file\u2026" fills that gap.
        sender(ui_target, message="\u2705 Confirmed signature.")  # ✅
        _mark_signed(output_dir)

        # P8-3: both-signed gate. Without it, the founder's OC runs
        # finalize the moment its own sig is approved — producing a
        # partial PDF with only the founder's signature. Later, the
        # investor's own finalize produces a second (both-signed) PDF.
        # Two PDFs land in the group. With this gate, the creator waits
        # for the session to aggregate `complete` (all pendings
        # approved), then produces the executed PDF on the first try.
        if pre_finalize_wait_fn is not None:
            sender(
                ui_target,
                message="\u23f3 Waiting for counterparty to sign\u2026",  # ⏳
            )
            if not pre_finalize_wait_fn():
                if not is_active_fn(output_dir):
                    return 3
                sender(ui_target, message=(
                    "Your signature is on file, but your counterparty "
                    "hasn't signed yet. The executed PDF will land here "
                    "once both signatures are in."
                ))
                return 4
            if not is_active_fn(output_dir):
                return 3

        sender(ui_target, message="\U0001f4c4 Generating executed file\u2026")  # 📄

        pdf_path = finalize_fn(output_dir, pending_id, sshsign_host)
        if not pdf_path:
            if not is_active_fn(output_dir):
                return 3
            sender(ui_target, message=(
                "Signature received but I couldn't generate the executed PDF. "
                "Check the negotiation output directory on the server."
            ))
            return 2

        if not is_active_fn(output_dir):
            return 3

        # PDF is the final artifact; post to the visible venue AND the
        # DM for archival. When no group is bound the two targets collapse
        # to one — guard against double-posting.
        sender(ui_target, media_path=str(pdf_path))
        if group_chat_id and str(group_chat_id) != str(chat_id):
            sender(chat_id, media_path=str(pdf_path))
        return 0
    finally:
        typing.stop()


def _build_artifact_uri(
    session_id: str,
    pdf_path: Path,
    creator_pending_id: str = "",
    creator_role: str = "",
) -> str:
    """URI that marks the session as finalized AND carries the creator's
    pending_id so the joiner can reproduce the PDF locally.

    Content-addressed artifact hosting is deferred to a future sshsign
    change. Until then, both OCs reconstruct the PDF from their own
    envelope + sshsign's get-envelope for the OTHER side. To do that
    the joiner needs the creator's pending_id, which we embed here as
    a query param. Format: sshsign://session/<id>/executed.pdf?
    creator_pending=<pid>&creator_role=<role>.
    """
    base = f"sshsign://session/{session_id}/executed.pdf"
    params = []
    if creator_pending_id:
        params.append(f"creator_pending={urllib.parse.quote(creator_pending_id)}")
    if creator_role:
        params.append(f"creator_role={urllib.parse.quote(creator_role)}")
    if params:
        return base + "?" + "&".join(params)
    return base


def _write_counterparty_pending(
    output_dir: Path,
    session_id: str,  # noqa: ARG001 — kept for signature compat; neg_id comes from mint.json
    role: str,
    pending_id: str,
) -> None:
    """Write the counterparty's pending_id to the local negotiate output
    dir in the format upstream's finalize expects:
      <neg_dir>/output/<neg_id>_<role>_pending.txt

    Without this, the joiner's finalize call only sees its own role's
    pending file and produces a single-signature PDF instead of the
    executed (both-signatures) version.

    Uses the raw negotiation_id from mint.json for the filename (NOT
    the sshsign-prefixed session_id form) so it matches what upstream
    writes on its side.
    """
    try:
        mint = json.loads((output_dir / "mint.json").read_text())
        neg_id = mint.get("negotiation_id") or ""
        founder_cfg = mint.get("founder_config_path") or ""
        investor_cfg = mint.get("investor_config_path") or ""
        anchor = founder_cfg or investor_cfg
        if not anchor or not neg_id:
            return
        neg_dir = Path(anchor).parent
        neg_output = neg_dir / "output"
        neg_output.mkdir(parents=True, exist_ok=True)
        pending_file = neg_output / f"{neg_id}_{role}_pending.txt"
        pending_file.write_text(pending_id.strip() + "\n")
    except (OSError, json.JSONDecodeError, KeyError) as e:
        sys.stderr.write(f"writing counterparty pending: {e}\n")


def _parse_artifact_uri(uri: str) -> tuple[str, str]:
    """Extract (creator_pending_id, creator_role) from a session artifact
    URI. Returns ("", "") for unparseable or missing params."""
    if "?" not in uri:
        return "", ""
    query = uri.split("?", 1)[1]
    params = urllib.parse.parse_qs(query)
    return (
        params.get("creator_pending", [""])[0],
        params.get("creator_role", [""])[0],
    )


def _creator_await_sign_and_finalize(
    output_dir: Path,
    chat_id: str,
    sshsign_host: str,
    pending_id: str,
    session_id: str,
    timeout: int = 900,
    poll_interval: int = 5,
    sender=send_telegram,
    poll_fn=_poll_envelope_approval,
    finalize_fn=_finalize_executed_pdf,
    is_active_fn=_is_still_active_session,
    session_client=None,
    typing_factory=None,
    group_chat_id: str | None = None,
    session_complete_fn=_poll_session_complete,
) -> int:
    """Creator-finalizes flow. Same as the demo path, plus a trailing
    `complete-session` call that unblocks the non-creator's poll loop.

    group_chat_id, when set, forwards to _await_sign_and_push so status
    cards + PDF double-post into the bound Telegram group.

    P8-3: gates finalize on the session aggregating to 'complete' (all
    pendings approved) rather than firing on just the creator's own sig.
    Without the gate, creator-runs-finalize-first produced a partial PDF
    with only the founder's signature; later the joiner's own finalize
    produced a second (both-signed) PDF; both got posted to the group.
    The gate pushes both PDFs into one.

    Returns 0 on success (PDF pushed + session marked complete), non-zero
    for partial failures. A failed complete-session is NOT treated as
    fatal — the PDF already reached the user; J7 adds retry + manual
    fallback.
    """
    # P8-3: build the both-signed gate. The creator is the one who
    # ships the finalized PDF, so they need to wait for the joiner's
    # sig before running upstream's run_finalize. The joiner waits for
    # complete-session (which creator calls below) in its own flow;
    # no gate needed on that side.
    def _wait_for_both_signed() -> bool:
        if not session_id:
            return True
        return session_complete_fn(
            session_id=session_id,
            sshsign_host=sshsign_host,
            timeout=timeout,
            interval=poll_interval,
        )

    rc = _await_sign_and_push(
        output_dir=output_dir,
        chat_id=chat_id,
        sshsign_host=sshsign_host,
        pending_id=pending_id,
        timeout=timeout,
        poll_interval=poll_interval,
        sender=sender,
        poll_fn=poll_fn,
        finalize_fn=finalize_fn,
        is_active_fn=is_active_fn,
        typing_factory=typing_factory,
        group_chat_id=group_chat_id,
        pre_finalize_wait_fn=_wait_for_both_signed,
    )
    if rc != 0 or not session_id:
        return rc

    # Locate the executed PDF so we can cite a stable URI back to the
    # non-creator. We also embed our own pending_id in the URI so the
    # joiner can reconstruct the PDF locally (upstream's finalize needs
    # a pending file for each role, and the joiner has only its own).
    creator_role = ""
    try:
        mint = json.loads((output_dir / "mint.json").read_text())
        creator_role = mint.get("user_role", "")
        neg_id = mint.get("negotiation_id", "")
        neg_dir = Path(mint["founder_config_path"]).parent
        # Upstream writes the executed PDF as "<neg_id>_executed.pdf"
        # (NOT "<session_id>_executed.pdf") — session_id is the prefixed
        # form we use for sshsign session APIs, not for file layout.
        pdf_path = neg_dir / "output" / f"{neg_id}_executed.pdf"
    except (OSError, json.JSONDecodeError, KeyError):
        pdf_path = output_dir / "executed.pdf"

    client = session_client or SshsignSession(host=sshsign_host)
    try:
        client.complete_session(
            session_id=session_id,
            executed_artifact=_build_artifact_uri(
                session_id, pdf_path,
                creator_pending_id=pending_id,
                creator_role=creator_role,
            ),
        )
    except SshsignSessionError as e:
        sys.stderr.write(f"complete-session failed (non-fatal): {e}\n")
    return 0


def _joiner_await_sign_and_finalize(
    output_dir: Path,
    chat_id: str,
    sshsign_host: str,
    pending_id: str,
    session_id: str,
    timeout: int = 900,
    poll_interval: int = 5,
    completion_poll_interval: int = 10,
    completion_timeout: int = 900,
    sender=send_telegram,
    poll_fn=_poll_envelope_approval,
    finalize_fn=_finalize_executed_pdf,
    is_active_fn=_is_still_active_session,
    session_client=None,
    sleep_fn=None,
    now_fn=None,
    typing_factory=None,
    group_chat_id: str | None = None,
) -> int:
    """Joiner-finalizes flow. The investor signs their own envelope, then
    waits for the creator to finalize (status=completed) before running
    their own finalize — that way both sides' signatures are present.

    Why wait: `run_finalize` pulls signatures from sshsign. If the
    investor finalizes before the founder has signed, the output PDF
    only has the investor's signature. Waiting for status=completed
    signals the founder is done and we can safely reproduce the PDF.

    Returns 0 if the investor received the executed PDF, non-zero on
    timeout or finalize failure.
    """
    if sleep_fn is None:
        import time as _time
        sleep_fn = _time.sleep
    if now_fn is None:
        import time as _time
        now_fn = _time.time

    # Typing indicator spans the whole wait — between clicking sign and
    # the PDF landing. Follows the visible venue (group if bound).
    ui_target = group_chat_id or chat_id
    if typing_factory is None:
        typing = TypingLoop(chat_id=ui_target, bot_token=get_bot_token())
    else:
        typing = typing_factory(ui_target)
    typing.start()

    try:
        approved = poll_fn(
            pending_id=pending_id,
            sshsign_host=sshsign_host,
            timeout=timeout,
            interval=poll_interval,
        )
        if not approved:
            if not is_active_fn(output_dir):
                return 3
            sender(chat_id, message=(
                "Signature not received after waiting.\n"
                'When you sign, reply "signed" and I\'ll verify manually.'
            ))
            return 1

        if not is_active_fn(output_dir):
            return 3

        sender(ui_target, message="\u2705 Confirmed signature.")  # ✅
        _mark_signed(output_dir)
        sender(ui_target, message=(
            "\u23f3 Waiting for your counterparty to sign\u2026"  # ⏳
        ))

        # Poll the session until the founder finalizes.
        client = session_client or SshsignSession(host=sshsign_host)
        start = now_fn()
        while True:
            elapsed = now_fn() - start
            if elapsed > completion_timeout:
                if not is_active_fn(output_dir):
                    return 3
                sender(ui_target, message=(
                    "\u26a0\ufe0f Counterparty didn't finalize in time. "  # ⚠
                    "Check sshsign.dev/audit/" + session_id
                    + " for the latest session status. The executed PDF "
                    "will be available there once both sides complete."
                ))
                return 1
            try:
                sess = client.get_session(session_id=session_id)
            except SshsignSessionError as e:
                sys.stderr.write(f"get-session while waiting completion: {e}\n")
                sleep_fn(completion_poll_interval)
                continue
            status = (sess.get("status") or "").lower()
            if status == "completed":
                # Write the creator's pending_id locally so upstream's
                # finalize can find both envelopes (upstream's
                # _collect_all_signatures reads <neg>/output/
                # <neg>_<role>_pending.txt for each role; we only have
                # our own — extract the creator's from the session's
                # executed_artifact URI).
                artifact_uri = sess.get("executed_artifact") or ""
                creator_pid, creator_role = _parse_artifact_uri(artifact_uri)
                if creator_pid and creator_role:
                    _write_counterparty_pending(
                        output_dir, session_id, creator_role, creator_pid,
                    )
                break
            if status == "expired":
                body = format_event({"type": "session_expired"})
                if body:
                    sender(ui_target, message=body)
                return 2
            if status == "rescinded_after_sign":
                body = format_event({
                    "type": "rescinded_after_sign_observer",
                    "by": "Your counterparty",
                })
                if body:
                    sender(ui_target, message=body)
                return 2
            if status == "canceled":
                body = format_event({
                    "type": "canceled_after_deal_observer",
                    "by": "Your counterparty",
                })
                if body:
                    sender(ui_target, message=body)
                return 2
            sleep_fn(completion_poll_interval)

        if not is_active_fn(output_dir):
            return 3

        sender(ui_target, message="\U0001f4c4 Generating executed file\u2026")  # 📄
        pdf_path = finalize_fn(output_dir, pending_id, sshsign_host)
        if not pdf_path:
            if not is_active_fn(output_dir):
                return 3
            sender(ui_target, message=(
                "Signature received but I couldn't generate the executed PDF. "
                "Check the session on sshsign.dev for the shared artifact."
            ))
            return 2

        if not is_active_fn(output_dir):
            return 3

        # Final artifact: post to the visible venue AND the DM for archival.
        sender(ui_target, media_path=str(pdf_path))
        if group_chat_id and str(group_chat_id) != str(chat_id):
            sender(chat_id, media_path=str(pdf_path))
        return 0
    finally:
        typing.stop()


def run_negotiate(output_dir: str, chat_id_flag: str | None = None) -> int:
    """Full negotiate flow: mint tokens then stream the negotiation to chat."""
    out = Path(output_dir)
    config_path = out / "config.json"
    if not config_path.exists():
        sys.stderr.write(f"No config.json in {output_dir}. Run 'prepare' first.\n")
        return 2

    config = json.loads(config_path.read_text())

    # Claim this output dir for this process. Any prior process still polling
    # an envelope on behalf of a previous negotiation will see a PID mismatch
    # on its next `_is_still_active_session` check and exit silently.
    try:
        (out / ".session.pid").write_text(str(os.getpid()))
    except OSError:
        pass

    # Push an interstitial so the user knows we're alive during the slow
    # mint step. run_mint does: generate APOA key pair → mint founder+
    # investor tokens via create_tokens.py → register signing session via
    # sshsign (two-party). That's several subprocess calls totaling
    # 15-30s; without this the chat sits silent between "🚀 Starting
    # negotiation" and the authorization card.
    chat_id = resolve_chat_id(chat_id_flag)
    if chat_id:
        send_telegram(chat_id, message=(
            "\U0001f510 Setting up your secure session\u2026"  # 🔐
        ))

    # In a Telegram DM, chat_id == user_id (positive int). Thread it through
    # run_mint → _register_signing_session so the founder's user_id gets
    # stashed on the sshsign session's metadata_member for Phase 8 /bind ACL.
    # Positive-only: group chat_ids are negative; we don't want a group id
    # here even if the skill were somehow invoked from a group.
    tg_user_id: int | None = None
    if chat_id:
        try:
            cid = int(chat_id)
            if cid > 0:
                tg_user_id = cid
        except ValueError:
            tg_user_id = None

    rc = run_mint(output_dir, config, telegram_user_id=tg_user_id)
    if rc != 0:
        return rc

    if not chat_id:
        sys.stderr.write(
            "No chat_id: pass --chat-id or ensure /root/.openclaw/agents/main/sessions/sessions.json has a telegram:direct entry.\n"
        )
        return 2

    # Authorization card — first surfacing of APOA after mint. Fires once,
    # before the founder-two-party wait or the first round. User sees their
    # bounds spelled out in plain English so the later "agent tried X,
    # rejected" event has context.
    ttl_seconds = int(os.environ.get("NEGOTIATION_TTL", "3600"))
    auth_body = format_event({
        "type": "authorized",
        "constraints": config.get("constraints") or {},
        "ttl_hours": max(1, ttl_seconds // 3600),
    })
    if auth_body:
        send_telegram(chat_id, message=auth_body)

    # Two-party mode: the founder waits for the investor to join; the
    # investor (who just joined) goes straight to streaming. Demo mode
    # skips both paths.
    try:
        mint = json.loads((out / "mint.json").read_text())
    except (OSError, json.JSONDecodeError):
        mint = {}
    if mint.get("mode") == "two_party":
        if mint.get("user_role") == "founder":
            wait_rc = _founder_two_party_gate(
                out=out,
                chat_id=chat_id,
                mint=mint,
                constraints=config.get("constraints") or {},
            )
            if wait_rc != 0:
                return wait_rc
        elif mint.get("user_role") == "investor":
            # Investor just joined — push a short "joined; starting" card
            # so they know the flow is progressing before upstream's first
            # offer lands.
            sender = send_telegram
            sender(chat_id, message=(
                "\u2705 Joined the negotiation. Starting now\u2026"  # ✅
            ))
    else:
        # Demo (solo) mode: push the "starting" card directly from the
        # script now that SKILL.md tells the model not to emit its own
        # preamble. This ensures the order is always
        #   🔐 setup → 🔒 auth → 🚀 starting → rounds
        # matching the two-party flow's
        #   🔐 setup → 🔒 auth → 🤝 invite | ✅ joined → rounds
        send_telegram(chat_id, message="\U0001f680 Starting negotiation\u2026")  # 🚀

    # Phase 8 K2/K3: if the session has been bound to a Telegram group
    # via /bind (K1), route rounds / outcome / status cards / the PDF to
    # the group; keep signing URL + personal errors in the DM. A None
    # here means demo mode or a two-party session the founder never
    # bound — behavior stays identical to Phase 7.
    #
    # K3 detail: this block fires for BOTH roles, so the investor's bot
    # picks up the same group_chat_id the founder bound and streams into
    # the same venue. The kickoff card is role-aware so the group sees
    # one distinct arrival message per side (instead of two identical
    # "starting" cards).
    sshsign_host = os.environ.get("SSHSIGN_HOST", "sshsign.dev")
    group_chat_id: str | None = None
    if mint.get("mode") == "two_party":
        session_id_for_wait = _sshsign_session_id(mint.get("negotiation_id") or "")
        group_chat_id = _resolve_group_chat_id(session_id_for_wait)
        role = (mint.get("user_role") or "").lower()

        # P7-5 investor-side gate: don't let run_distributed spawn
        # until the founder's agent has flipped founder_streaming_at.
        # Otherwise upstream hangs on "waiting for founder's opening
        # offer" until its internal timeout. The gate fires ONLY on
        # the investor side; the founder has already resumed locally
        # (or is about to, via this turn's _stream_to_telegram).
        if role == "investor" and group_chat_id:
            wait_status = _investor_wait_for_founder_streaming(
                session_id=session_id_for_wait,
                group_chat_id=group_chat_id,
            )
            if wait_status != "streaming":
                # Timeout or terminal: surface happened already; the
                # caller (run_negotiate) should NOT spawn the stream.
                return 0 if wait_status == "terminal" else 4

        if group_chat_id:
            if role == "founder":
                kickoff = (
                    "\U0001f3ac Founder's agent is live — rounds start next."  # 🎬
                )
            elif role == "investor":
                kickoff = (
                    "\U0001f4bc Investor's agent joined — both sides live now."  # 💼
                )
            else:
                kickoff = (
                    "\U0001f3ac Live negotiation starting — watch here."  # 🎬
                )
            send_telegram(group_chat_id, message=kickoff)

    bot_username = os.environ.get("TELEGRAM_BOT_USERNAME", "AgenticPOA_bot")
    # P8-1: in two-party mode, upstream's run_distributed only emits the
    # final `signing` event to stdout — offer/counter/accept rounds
    # happen but are only visible in sshsign's negotiation_offers log.
    # Passing `negotiation_id` enables the supplemental history poller
    # so the group sees rounds live. Demo mode (run_local) emits rounds
    # on stdout directly; no poll needed, and not passing the id keeps
    # the behavior identical to Phase 7.
    history_neg_id = (
        mint.get("negotiation_id") if mint.get("mode") == "two_party" else None
    )
    stream_rc, signing_event = _stream_to_telegram(
        output_dir=out,
        chat_id=chat_id,
        constraints=config.get("constraints"),
        bot_username=bot_username,
        group_chat_id=group_chat_id,
        negotiation_id=history_neg_id,
        sshsign_host=sshsign_host,
    )
    if stream_rc != 0 or not signing_event:
        return stream_rc

    pending_id = signing_event.get("pending_id") or ""
    if not pending_id:
        return 0

    # Two-party mode: role determines who finalizes.
    #   Creator (founder): wait for own sig, finalize locally, push PDF,
    #     then call complete-session so the investor's OC unblocks.
    #   Non-creator (investor): wait for own sig, then poll the session
    #     until status=completed (founder finalized on their side), then
    #     finalize locally using both signatures and push PDF.
    if mint.get("mode") == "two_party":
        is_creator = mint.get("user_role") == "founder"
        if is_creator:
            return _creator_await_sign_and_finalize(
                output_dir=out,
                chat_id=chat_id,
                sshsign_host=sshsign_host,
                pending_id=pending_id,
                session_id=_sshsign_session_id(mint.get("negotiation_id") or ""),
                group_chat_id=group_chat_id,
            )
        return _joiner_await_sign_and_finalize(
            output_dir=out,
            chat_id=chat_id,
            sshsign_host=sshsign_host,
            pending_id=pending_id,
            session_id=_sshsign_session_id(mint.get("negotiation_id") or ""),
            group_chat_id=group_chat_id,
        )

    return _await_sign_and_push(
        output_dir=out,
        chat_id=chat_id,
        sshsign_host=sshsign_host,
        pending_id=pending_id,
    )


def _join_signing_session(
    mint_output: dict,
    shared_session: dict,
    user_role: str,
    neg_dir: Path,
    repo: Path,
    session_client=None,
) -> dict | None:
    """Call sshsign join-session with the joiner's APOA pubkey, then
    re-fetch the session (now member-authenticated) to retrieve the
    counterparty's APOA pubkey and write it to disk so the stream helper
    can reference it.

    On success, returns a dict of session fields to merge into mint.json
    (session_code, status, counterparty pubkey path). On failure, None.
    """
    pubkey_path = neg_dir / "keys" / f"{user_role}_public.pem"
    if not pubkey_path.exists():
        sys.stderr.write(
            f"Cannot join: APOA pubkey for role={user_role} not found at {pubkey_path}\n"
        )
        return None

    try:
        our_pubkey_pem = pubkey_path.read_text()
    except OSError as e:
        sys.stderr.write(f"reading {pubkey_path}: {e}\n")
        return None

    session_code = shared_session.get("session_code")
    if not session_code:
        sys.stderr.write("Cannot join: shared_session has no session_code\n")
        return None

    client = session_client or SshsignSession(
        host=os.environ.get("SSHSIGN_HOST", "sshsign.dev"),
    )

    try:
        join_result = client.join_session(
            session_code=session_code,
            role=user_role,
            apoa_pubkey_pem=our_pubkey_pem,
            party_did=os.environ.get("USER_DID") or None,
        )
    except SshsignSessionError as e:
        sys.stderr.write(f"join-session failed: {e}\n")
        return None

    # Re-fetch as member to pick up the counterparty's pubkey.
    try:
        member_view = client.get_session(session_code=session_code)
    except SshsignSessionError as e:
        sys.stderr.write(f"post-join get-session failed: {e}\n")
        # Non-fatal — we joined successfully; stream helper can work without
        # the counterparty pubkey for the distributed flow since sshsign
        # validates offers by envelope signature at submission time.
        member_view = join_result

    counterparty_role = "investor" if user_role == "founder" else "founder"
    counterparty_pubkey_pem = ""
    for m in (member_view.get("members") or []):
        if m.get("role") == counterparty_role:
            counterparty_pubkey_pem = m.get("apoa_pubkey_pem") or ""
            break

    # Write counterparty pubkey to the mint dir so _stream_negotiate can
    # reference it via its usual founder.json/investor.json path.
    counterparty_pubkey_path = neg_dir / "keys" / f"{counterparty_role}_public.pem"
    if counterparty_pubkey_pem:
        try:
            counterparty_pubkey_path.write_text(counterparty_pubkey_pem)
        except OSError as e:
            sys.stderr.write(f"writing counterparty pubkey: {e}\n")

    return {
        "session_code": session_code,
        "session_created_at": member_view.get("created_at"),
        "session_expires_at": member_view.get("expires_at"),
        "session_status": member_view.get("status"),
        "counterparty_pubkey_path": (
            str(counterparty_pubkey_path) if counterparty_pubkey_pem else ""
        ),
    }


def _fetch_session_for_join(
    session_code: str,
    session_client=None,
) -> tuple[dict | None, str | None]:
    """Call sshsign get-session to validate a join-code and pull founder's
    APOA pubkey + member metadata.

    Returns (session_payload, error_message). Exactly one is non-None.
    """
    client = session_client or SshsignSession(
        host=os.environ.get("SSHSIGN_HOST", "sshsign.dev"),
    )
    try:
        sess = client.get_session(session_code=session_code)
    except SessionNotFoundError:
        return None, (
            "That code doesn't match a current negotiation. "
            "Double-check the code with your counterparty — "
            "it should look like INV-XXXXX."
        )
    except SessionExpiredError:
        return None, (
            "That negotiation expired. Ask your counterparty for a new code."
        )
    except SshsignSessionError as e:
        return None, f"Couldn't look up session: {e}"

    status = (sess.get("status") or "").lower()
    if status in ("canceled", "rescinded_after_sign"):
        return None, "That negotiation was canceled. Ask your counterparty for a new code."
    if status == "expired":
        return None, "That negotiation expired. Ask your counterparty for a new code."
    if status == "completed":
        return None, "That negotiation has already completed."
    if status == "joined":
        # Counterparty role already has a member. We'd be a third wheel —
        # sshsign will reject join-session anyway, but this lets us surface
        # a clearer message before minting wasted tokens.
        return None, (
            "That negotiation has already started with someone else. "
            "Ask your counterparty if they meant to share a different code."
        )
    if status != "open":
        return None, f"Can't join a session in state: {status}"

    # status=open — session is joinable. (We used to accept joined here and
    # defer role-conflict detection to join-session; now the joined branch
    # above short-circuits with a clearer error.)
    return sess, None


def _enrich_constraints_from_session(
    constraints: dict,
    session_payload: dict,
) -> dict:
    """Merge founder-side metadata from the session into the investor's
    parsed constraints. Investor-supplied fields always win — the merge
    only fills gaps.

    Reads from metadata_public always (company_name lives there so
    non-members can see it on the confirm card). Reads from metadata_member
    if the caller is a session member (founder-side view, or investor
    after join-session).
    """
    out = dict(constraints)

    def _parse(raw):
        if raw is None:
            return {}
        if isinstance(raw, dict):
            return raw
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    public_md = _parse(session_payload.get("metadata_public"))
    member_md = _parse(session_payload.get("metadata_member"))

    for key in ("company_name", "founder_name", "founder_title",
                "investor_name", "investor_firm"):
        if out.get(key):
            continue
        if public_md.get(key):
            out[key] = public_md[key]
        elif member_md.get(key):
            out[key] = member_md[key]
    return out


def _founder_two_party_gate(
    out: Path,
    chat_id: str,
    mint: dict,
    constraints: dict,
    sender=send_telegram,
    wait_fn=_wait_for_counterparty,
) -> int:
    """Push the invitation card, wait for the counterparty to join, and
    return 0 to proceed with streaming or a non-zero rc if the session
    ended before negotiation could begin.

    rc=0    → counterparty joined; caller should run the stream
    rc=1    → invitation expired; user notified
    rc=2    → session was canceled; user notified (or stays silent if we
              canceled ourselves; the parser owns that path in J5)
    rc=3    → missing session_code or persistent sshsign error
    """
    session_code = mint.get("session_code")
    session_id = _sshsign_session_id(mint.get("negotiation_id") or "")
    if not session_code or not session_id:
        sender(chat_id, message=(
            "\u26a0\ufe0f Internal error: two-party session was not registered. "  # ⚠️
            "Try again."
        ))
        return 3

    # Counterparty label for the card — prefer the user's own NL phrasing
    # (investor_name + firm) over a generic word.
    label_parts = [
        p for p in (
            constraints.get("investor_name"),
            constraints.get("investor_firm"),
        ) if p
    ]
    counterparty_label = ", ".join(label_parts) if label_parts else "your counterparty"

    invitation_body = format_event({
        "type": "invitation",
        "session_code": session_code,
        "invite_url": _build_invite_url(session_code),
        "expires_at": mint.get("session_expires_at") or "",
        "ttl_hours": 24,
        "counterparty_label": counterparty_label,
    })
    if invitation_body:
        sender(chat_id, message=invitation_body)

    # Phase 8 opt-in: go-live card offers group-mode as an alternative to
    # DM-only. Non-breaking — ignoring it keeps the existing flow intact.
    # K2 will replace this "secondary card" pattern with a fully restructured
    # flow; K1 ships it additively so it can be verified in isolation.
    bot_username = os.environ.get("TELEGRAM_BOT_USERNAME", "AgenticPOA_bot").lstrip("@")
    investor_bot = os.environ.get("TELEGRAM_BOT_USERNAME_INVESTOR", "AgenticPOAInvestor_bot").lstrip("@")
    investor_handle = (os.environ.get("INVESTOR_TELEGRAM_HANDLE") or "").lstrip("@")
    go_live_body = format_event({
        "type": "go_live",
        "session_code": session_code,
        "founder_bot": f"@{bot_username}",
        "investor_bot": f"@{investor_bot}",
        "counterparty_handle": f"@{investor_handle}" if investor_handle else "",
    })
    if go_live_body:
        sender(chat_id, message=go_live_body)

    status = wait_fn(
        session_id=session_id,
        session_code=session_code,
        chat_id=chat_id,
        counterparty_label=counterparty_label,
    )
    if status == "joined":
        return 0
    if status == "expired":
        body = format_event({"type": "invitation_expired"})
        if body:
            sender(chat_id, message=body)
        return 1
    if status == "canceled":
        # Cancel message content depends on who canceled — J5 owns the
        # user-facing copy. Keep this path silent for now; the stream
        # loop wouldn't run anyway.
        return 2
    # status == "error" or unknown
    sender(chat_id, message=(
        "\u26a0\ufe0f Lost connection to the signing service while waiting. "
        "Your session still exists; try the command again."
    ))
    return 3


def _build_invite_url(session_code: str) -> str:
    """Generate a one-click invite URL for the given session code, or
    empty when the provisioning service isn't available.

    The one-click flow depends on an external provisioning service that
    spins up a fresh OpenClaw instance for the joiner. Until that service
    is deployed, we return an empty URL so the invitation card falls back
    to the "reply to your own bot with 'Join INV-XXXXX'" instructions
    (better than a broken link).

    Environment:
      PROVISION_BASE_URL — opt-in base URL for the provisioning service.
        Only when set does the invitation card include a join URL.
    """
    code = (session_code or "").strip()
    if not code:
        return ""
    base = (os.environ.get("PROVISION_BASE_URL") or "").strip()
    if not base:
        return ""
    return f"{base.rstrip('/')}/join/{code}"


_IDENTITY_TO_ENV: list[tuple[str, str]] = [
    # (identity-dict key, env var name we set via openclaw config set)
    # `role` is intentionally omitted — it's derived per-negotiation from the
    # NL, not pinned to the user's profile (they can play either side in demo
    # mode).
    ("name_founder", "FOUNDER_NAME"),
    ("title_founder", "FOUNDER_TITLE"),
    ("company", "COMPANY_NAME"),
    ("name_investor", "INVESTOR_NAME"),
    ("firm", "INVESTOR_FIRM"),
]


def _build_env_updates(identity: dict) -> dict[str, str]:
    """Map a parsed identity to the full set of env vars we need to persist.

    When the user self-identifies as founder, we seed FOUNDER_NAME with their
    name and leave INVESTOR_NAME at its current value (for the demo's AI
    counterparty). When they self-identify as investor, we seed INVESTOR_NAME
    and INVESTOR_FIRM. Either way COMPANY_NAME / title fields are set if the
    user mentioned them.
    """
    updates: dict[str, str] = {}
    name = (identity.get("name") or "").strip()
    title = (identity.get("title") or "").strip()
    company = (identity.get("company") or "").strip()
    firm = (identity.get("firm") or "").strip()
    role = identity.get("role", "founder")

    if role == "founder":
        if name:
            updates["FOUNDER_NAME"] = name
        if title:
            updates["FOUNDER_TITLE"] = title
        if company:
            updates["COMPANY_NAME"] = company
    else:
        if name:
            updates["INVESTOR_NAME"] = name
        if firm:
            updates["INVESTOR_FIRM"] = firm
        # Investors often self-identify with title too ("Partner", "MD")
        if title:
            updates["INVESTOR_FIRM"] = updates.get("INVESTOR_FIRM", firm)  # firm stays
    return updates


def _persist_env_updates(updates: dict[str, str], runner=subprocess.run) -> list[str]:
    """Apply the identity updates via `openclaw config set` — one call per
    env var so OpenClaw's config validator sees each change individually.

    Returns the list of env vars that failed to persist (empty on success).
    """
    failures: list[str] = []
    for key, value in updates.items():
        path = f"skills.entries.negotiate_safe.env.{key}"
        try:
            result = runner(
                ["openclaw", "config", "set", path, value],
                capture_output=True, text=True, timeout=10,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            failures.append(key)
            continue
        if result.returncode != 0:
            failures.append(key)
    return failures


_BIND_CODE_RE = re.compile(r"(INV-[A-Z0-9]+)", re.IGNORECASE)


def _extract_bind_code(message: str) -> str | None:
    """Pull the INV-XXXXX code out of a `/bind ...` message body.

    Tolerates `/bind INV-XYZ`, `/bind@Bot INV-XYZ`, extra whitespace, or
    the code being the only token. Case-insensitive match; returned code
    is upper-cased since the server stores codes upper-case.
    """
    if not message:
        return None
    m = _BIND_CODE_RE.search(message)
    if not m:
        return None
    return m.group(1).upper()


CRON_JOB_NAME = "negotiate_safe-scan"


# P7-5 investor-side wait tuning. Exposed as module constants so tests
# can monkeypatch to fast values and ops can env-override for the
# post-Day-4 tuning pass.
INVESTOR_WAIT_POLL_INTERVAL = float(os.environ.get("CLAW_NEGOTIATE_WAIT_POLL", "3"))
INVESTOR_WAIT_HEARTBEAT_AT = float(os.environ.get("CLAW_NEGOTIATE_WAIT_HEARTBEAT", "15"))
INVESTOR_WAIT_TIMEOUT = float(os.environ.get("CLAW_NEGOTIATE_WAIT_TIMEOUT", "180"))


def _investor_wait_for_founder_streaming(
    session_id: str,
    group_chat_id: str,
    session_client=None,
    sender=send_telegram,
    typing_factory=None,
    sleep_fn=None,
    now_fn=None,
) -> str:
    """Bounded poll on founder_streaming_at (not founder_resumed_at).

    Invoked immediately after the investor's join completes, before
    ``_stream_to_telegram`` (because upstream's run_distributed will
    hang on the missing opening offer if the founder's agent hasn't
    resumed yet). Posts a waiting card, starts a typing indicator in
    the group, and polls the session row every 3s for up to 180s.

    Return values (all lowercase strings for easy grep):
      "streaming"     — founder_streaming_at set; caller proceeds to stream
      "timeout"       — 180s elapsed; caller returns without streaming
      "terminal"      — session canceled/rescinded/completed/expired mid-wait

    Cards emitted:
      waiting          → right after join (investor_waiting_for_founder)
      heartbeat        → at ~15s if still waiting (investor_waiting_heartbeat)
      both_online      → on streaming_at hit (investor_both_online)
      timeout          → on 180s cap (investor_wake_timeout)
      session_ended    → on terminal status (investor_session_ended)

    The returned value determines what the caller does next:
      streaming → call _stream_to_telegram
      timeout or terminal → return from run_negotiate without streaming
    """
    import time

    if sleep_fn is None:
        sleep_fn = time.sleep
    if now_fn is None:
        now_fn = time.time

    client = session_client or SshsignSession(
        host=os.environ.get("SSHSIGN_HOST", "sshsign.dev"),
    )

    # Waiting card first — sets the expectation even if the poll
    # returns immediately (same tick the founder flipped streaming_at).
    body = format_event({"type": "investor_waiting_for_founder"})
    if body:
        sender(group_chat_id, message=body)

    # Typing indicator in the group. Factory injection is for tests;
    # production uses the real bot token.
    if typing_factory is None:
        typing = TypingLoop(chat_id=group_chat_id, bot_token=get_bot_token())
    else:
        typing = typing_factory(group_chat_id)
    typing.start()

    try:
        start = now_fn()
        heartbeat_sent = False
        # Add one extra iteration past the cap so we definitively
        # timeout rather than miss the final check by a hair.
        while True:
            elapsed = now_fn() - start
            if elapsed >= INVESTOR_WAIT_TIMEOUT:
                body = format_event({"type": "investor_wake_timeout"})
                if body:
                    sender(group_chat_id, message=body)
                return "timeout"

            try:
                sess = client.get_session(session_id=session_id)
            except SshsignSessionError as e:
                # Transient: log and keep polling. A persistent error
                # will time out eventually and surface via the timeout
                # card. We don't want a network blip to abort the wait
                # given the founder's resume is the actual blocker.
                sys.stderr.write(f"wait-for-founder: get-session: {e}\n")
                sleep_fn(INVESTOR_WAIT_POLL_INTERVAL)
                continue

            status = (sess.get("status") or "").lower()
            if status in ("canceled", "rescinded", "rescinded_after_sign",
                          "completed", "expired"):
                body = format_event({
                    "type": "investor_session_ended", "status": status,
                })
                if body:
                    sender(group_chat_id, message=body)
                return "terminal"

            # Check founder's streaming_at. Members list is the
            # authoritative source; scan the founder row.
            streaming_at = None
            for m in (sess.get("members") or []):
                if (m.get("role") or "").lower() == "founder":
                    streaming_at = m.get("founder_streaming_at")
                    break
            if streaming_at:
                body = format_event({"type": "investor_both_online"})
                if body:
                    sender(group_chat_id, message=body)
                return "streaming"

            if not heartbeat_sent and elapsed >= INVESTOR_WAIT_HEARTBEAT_AT:
                body = format_event({"type": "investor_waiting_heartbeat"})
                if body:
                    sender(group_chat_id, message=body)
                heartbeat_sent = True

            sleep_fn(INVESTOR_WAIT_POLL_INTERVAL)
    finally:
        try:
            typing.stop()
        except Exception:
            pass


def ensure_cron(
    interval: str = "30s",
    runner: "callable | None" = None,
) -> tuple[bool, str | None]:
    """Install the ``negotiate_safe-scan`` cron job if absent.

    Invoked from the founder's mint path (``_register_signing_session``
    tail) so every fresh two-party session guarantees a running cron
    job on this droplet. Idempotent: if the job already exists, do
    nothing. Non-fatal on any failure — cron is a recovery mechanism,
    not a correctness gate, and the founder's current turn still
    works inline. Ops-visible errors go to stderr.

    Parameters
    ----------
    interval
        ``--every`` argument. Default ``30s``; the plan notes that
        OC may floor at ``60s`` and we re-tune during live dry-run.
    runner
        Injectable subprocess runner for tests; defaults to
        ``subprocess.run``.

    Returns
    -------
    (installed, error_message)
        ``installed`` is True if we called ``cron add`` (or True if
        the job already existed — both are "in place"). ``False``
        means an error prevented us from ensuring the job; error
        message is returned for caller-side logging.
    """
    if runner is None:
        runner = subprocess.run

    # list + parse-json to detect the existing job.
    try:
        list_res = runner(
            ["openclaw", "cron", "list", "--json"],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        return False, f"openclaw cron list failed: {e}"

    if list_res.returncode != 0:
        # Pairing wall, unpaired gateway, etc. Log + continue; scan
        # can be installed manually by ops as a fallback.
        return False, (
            f"openclaw cron list rc={list_res.returncode}: "
            f"{(list_res.stderr or list_res.stdout or '').strip()[:200]}"
        )

    try:
        jobs = json.loads(list_res.stdout or "[]")
    except json.JSONDecodeError as e:
        return False, f"openclaw cron list: invalid JSON: {e}"

    if isinstance(jobs, dict):
        # Some OC versions wrap the list under a top-level key.
        jobs = jobs.get("jobs") or jobs.get("items") or []

    for job in jobs or []:
        if isinstance(job, dict) and job.get("name") == CRON_JOB_NAME:
            # Already installed. Don't touch it — operator may have
            # tuned the interval since mint-time.
            return True, None

    # Not installed — add it. Flag set discovered live (OC 2026.4.x):
    #   * `--exact` is rejected for `--every` schedules (it's a `--cron`
    #     stagger control, not an `--every` knob).
    #   * `--session isolated` requires `--message` (agentTurn). For our
    #     case a system event in main session is the right shape — A.5
    #     in SKILL.md routes "negotiate_safe_scan" to `run_safe.py scan`
    #     and the scan turn is short, so it doesn't disrupt active
    #     conversations meaningfully.
    #   * `--no-deliver` is only valid for non-main sessions; with main
    #     session we just don't pass it (the system event payload
    #     doesn't generate user-visible chatter on its own).
    add_argv = [
        "openclaw", "cron", "add",
        "--name", CRON_JOB_NAME,
        "--every", interval,
        "--system-event", "negotiate_safe_scan",
        "--keep-after-run",
    ]
    try:
        add_res = runner(add_argv, capture_output=True, text=True, timeout=10)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        return False, f"openclaw cron add failed: {e}"

    if add_res.returncode != 0:
        return False, (
            f"openclaw cron add rc={add_res.returncode}: "
            f"{(add_res.stderr or add_res.stdout or '').strip()[:200]}"
        )
    return True, None


def _run_founder_resume(
    state: dict,
    sshsign_host: str | None = None,
    session_client=None,
    sender=send_telegram,
    now_fn=None,
) -> int:
    """P7-5 shared resume path.

    Called from two sites:
      * ``run_bind`` when the investor has ALREADY joined before the
        founder pasted ``/bind`` (fast path: founder is live in the
        bind turn; resume inline).
      * ``run_scan`` when an OpenClaw cron tick finds a waiting session
        that has advanced to ``joined`` but hasn't yet started streaming
        (slow path: founder's original process was reaped; wake fresh).

    Idempotency: if ``founder_resumed_at`` is already non-null on the
    sshsign session, exit without re-streaming. A concurrent scan tick
    or a bind racing with a scan can legitimately fire twice; the
    sshsign row is the single coordination point.

    State cleanup: on terminal session status, delete the local state
    pointer so the next scan doesn't keep poking a dead session.

    Return codes:
      0  resume completed (or cleanly no-op'd)
      1  resume not yet ready (status != joined; caller tries again later)
      2  terminal session — cleaned up; do not retry
      3  sshsign transport error
    """
    import time
    if now_fn is None:
        now_fn = time.time

    negotiation_id = state.get("negotiation_id") or ""
    output_dir_raw = state.get("output_dir") or ""
    if not negotiation_id or not output_dir_raw:
        sys.stderr.write(
            "resume: state missing negotiation_id or output_dir; skipping\n"
        )
        return 2

    out = Path(output_dir_raw)
    if not out.exists():
        # Output_dir was wiped (e.g., /tmp cleaned) — nothing to resume.
        # Drop the pointer so future scans don't keep re-reading it.
        sys.stderr.write(
            f"resume: output_dir {out} missing; cleaning state pointer\n"
        )
        state_store.delete_state(negotiation_id)
        return 2

    sshsign_host = sshsign_host or os.environ.get("SSHSIGN_HOST", "sshsign.dev")
    client = session_client or SshsignSession(host=sshsign_host)
    session_id = _sshsign_session_id(negotiation_id)

    try:
        sess = client.get_session(session_id=session_id)
    except SessionNotFoundError:
        # sshsign no longer knows this session. Local state is stale.
        state_store.delete_state(negotiation_id)
        return 2
    except SshsignSessionError as e:
        sys.stderr.write(f"resume: get-session failed: {e}\n")
        return 3

    status = (sess.get("status") or "").lower()
    if status in ("canceled", "rescinded", "rescinded_after_sign", "completed", "expired"):
        # Terminal. Drop the pointer; don't re-emit status cards (the
        # turn that terminated the session already sent the right one).
        sys.stderr.write(
            f"resume: session {session_id} is terminal ({status}); "
            f"cleaning state pointer\n"
        )
        state_store.delete_state(negotiation_id)
        return 2

    if status != "joined":
        # Investor hasn't joined yet. Scan will try again on next tick.
        return 1

    # Find this caller's member row and check dedup / founder_resumed_at.
    members = sess.get("members") or []
    founder_row: dict | None = None
    for m in members:
        if (m.get("role") or "").lower() == "founder":
            founder_row = m
            break
    if founder_row is None:
        sys.stderr.write("resume: session has no founder member row; cleaning\n")
        state_store.delete_state(negotiation_id)
        return 2

    if founder_row.get("founder_resumed_at"):
        # Another tick (or an in-process bind) already started the
        # resume. Stay idle; that turn owns the stream.
        return 0

    # Mark ourselves as the resuming turn. This is the idempotency
    # gate: sshsign's whitelisted field set makes the write creator-
    # only, and a subsequent scan on the same tick will see non-null
    # and bail above.
    now = int(now_fn())
    try:
        client.update_session_member(
            session_id, field="founder_resumed_at", value=now,
        )
    except SshsignSessionError as e:
        sys.stderr.write(f"resume: update-session-member resumed_at: {e}\n")
        return 3

    group_chat_id = _resolve_group_chat_id(session_id, session_client=client)

    # Orienting card (never starts with '/', per P7-5 invariant).
    orient_body = format_event({
        "type": "founder_resumed",
        "session_code": state.get("session_code"),
    })
    if orient_body:
        target = group_chat_id or ""
        if target:
            sender(target, message=orient_body)

    # Re-hydrate mint + constraints from disk. These were written at
    # mint-time and output_dir is stable across OC reap events.
    try:
        config = json.loads((out / "config.json").read_text())
    except (OSError, json.JSONDecodeError) as e:
        sys.stderr.write(f"resume: loading config.json: {e}\n")
        return 3
    try:
        mint = json.loads((out / "mint.json").read_text())
    except (OSError, json.JSONDecodeError) as e:
        sys.stderr.write(f"resume: loading mint.json: {e}\n")
        return 3

    # Founder's DM chat_id is preserved in config.json by run_prepare
    # (chat_id_flag / discovered from OC sessions). Fall back to empty
    # if truly absent — the stream still works, signing URL just lands
    # in whichever DM the typing loop defaults to.
    founder_dm = config.get("chat_id") or ""

    bot_username = os.environ.get("TELEGRAM_BOT_USERNAME", "AgenticPOA_bot")
    history_neg_id = mint.get("negotiation_id")

    # Mark streaming_at BEFORE invoking _stream_to_telegram. The function
    # blocks for the duration of the entire negotiation (rounds + signing
    # path + finalize), and the investor's bounded poll is gating run_
    # distributed on this signal. Setting it post-stream sequences both
    # sides incorrectly: the investor would only unblock after the
    # founder is DONE streaming, by which point the founder's
    # run_distributed has exited and there's no counterparty for the
    # investor's offers. INV-D67C9 (Apr 25) hit this race with the
    # streaming_at write sitting after _stream_to_telegram in earlier
    # code; moved here so both sides are concurrent.
    try:
        client.update_session_member(
            session_id, field="founder_streaming_at", value=int(now_fn()),
        )
    except SshsignSessionError as e:
        # Non-fatal: the investor will time out at 180s and emergency
        # card, but we'd rather the founder's stream keep going than
        # abort here. The audit trail on sshsign captures the gap.
        sys.stderr.write(f"resume: update-session-member streaming_at: {e}\n")

    stream_rc, signing_event = _stream_to_telegram(
        output_dir=out,
        chat_id=str(founder_dm),
        constraints=config.get("constraints"),
        bot_username=bot_username,
        group_chat_id=group_chat_id,
        negotiation_id=history_neg_id,
        sshsign_host=sshsign_host,
    )

    if stream_rc != 0 or not signing_event:
        # Stream failed or exited before a signing event landed. State
        # stays (not terminal yet); next tick will re-check and dedup
        # if resumed_at is still set. Real failures are rare; let the
        # scan loop + audit log guide recovery.
        return stream_rc

    pending_id = signing_event.get("pending_id") or ""
    if not pending_id:
        return 0

    # Creator (founder) finalize path — same as run_negotiate.
    rc = _creator_await_sign_and_finalize(
        output_dir=out,
        chat_id=str(founder_dm),
        sshsign_host=sshsign_host,
        pending_id=pending_id,
        session_id=session_id,
        group_chat_id=group_chat_id,
    )
    # Terminal outcome either way: clean up the pointer.
    state_store.delete_state(negotiation_id)
    return rc


def run_scan(
    session_client=None,
    sender=send_telegram,
    now_fn=None,
) -> int:
    """P7-5 cron entrypoint.

    Invoked by an OpenClaw cron job (every 30s, configured at mint
    time). Iterates every active state pointer under
    ``~/.openclaw/skill-state/negotiate_safe/`` and, for each, asks
    sshsign whether the session has advanced enough to warrant a
    resume. Fully idempotent: running twice in the same tick is safe
    (the second invocation sees founder_resumed_at set and no-ops).

    Returns 0 always — a single bad pointer shouldn't fail the whole
    tick. Individual errors go to stderr for audit.
    """
    try:
        pointers = state_store.list_active()
    except Exception as e:
        sys.stderr.write(f"scan: list_active failed: {e}\n")
        return 0

    for state in pointers:
        try:
            _run_founder_resume(
                state,
                session_client=session_client,
                sender=sender,
                now_fn=now_fn,
            )
        except Exception as e:
            # Contain per-pointer failures so one bad session doesn't
            # halt the tick; the next cron run re-attempts.
            sys.stderr.write(
                f"scan: resume failed for {state.get('negotiation_id')}: {e}\n"
            )
    return 0


def run_bind(
    session_code: str,
    group_chat_id: int,
    from_user_id: int,
    dm_chat_id_flag: str | None = None,
    sender=send_telegram,
    session_client=None,
) -> int:
    """Phase 8 /bind handler.

    Invoked when the founder types `/bind INV-XXXXX` in a Telegram group.
    Verifies the caller is the session founder (via metadata_member's
    telegram.founder_user_id, captured at create-session time), then
    calls sshsign `bind-group` to associate the group_chat_id with the
    session. Write-once on the server side, so a second /bind to a
    different group returns a typed error.

    Parameters are all plumbed from the OC Telegram envelope by the
    caller (SKILL.md instructs the model to supply them as flags).

    Return codes:
      0  success (bind applied)
      1  already bound to a different group (idempotent same-group ok)
      2  non-founder tried to bind / unknown code / bad chat type
      3  ssh/transport error

    Reply target: errors that need to reach the founder explicitly go
    to their DM (if dm_chat_id_flag resolves); generic group-context
    errors and the success confirmation go to the group. The wrong-chat
    error prefers DM because /bind in a DM means "I haven't created
    a group yet" — the user is still in the DM.
    """
    if group_chat_id >= 0:
        # Positive or zero chat_id means this wasn't a group — either a
        # DM (positive) or an invalid value. Steer the user to a group.
        body = format_event({"type": "bind_wrong_chat_type"})
        if body:
            target = group_chat_id if group_chat_id > 0 else (
                int(dm_chat_id_flag) if dm_chat_id_flag else 0
            )
            if target:
                sender(str(target), message=body)
        return 2

    client = session_client or SshsignSession(
        host=os.environ.get("SSHSIGN_HOST", "sshsign.dev"),
    )

    try:
        sess = client.get_session(session_code=session_code)
    except SessionNotFoundError:
        body = format_event({"type": "bind_unknown_code"})
        if body:
            sender(str(group_chat_id), message=body)
        return 2
    except SshsignSessionError as e:
        sys.stderr.write(f"bind: get-session failed: {e}\n")
        return 3

    # Pull founder_user_id from metadata_member. Best-effort: if it's
    # missing (older session minted before K1), we refuse the bind —
    # safer to fail closed than to let anyone in the group bind it.
    meta_raw = sess.get("metadata_member") or "{}"
    try:
        meta = json.loads(meta_raw) if isinstance(meta_raw, str) else (meta_raw or {})
    except (json.JSONDecodeError, TypeError):
        meta = {}
    expected_user_id = (meta.get("telegram") or {}).get("founder_user_id")
    if not expected_user_id or int(expected_user_id) != int(from_user_id):
        body = format_event({"type": "bind_wrong_user"})
        if body:
            sender(str(group_chat_id), message=body)
        return 2

    session_id = sess.get("session_id") or _sshsign_session_id(
        _negotiation_id_from_sshsign_session_id(sess.get("session_id", ""))
    )
    if not session_id:
        sys.stderr.write("bind: get-session returned no session_id\n")
        return 3

    try:
        client.bind_group(session_id, int(group_chat_id))
    except GroupAlreadyBoundError:
        body = format_event({"type": "bind_already_bound"})
        if body:
            sender(str(group_chat_id), message=body)
        return 1
    except SessionNotMemberError:
        # Creator IS a member, so this shouldn't fire unless the stored
        # user_id doesn't match a sshsign member row. Treat as auth failure.
        body = format_event({"type": "bind_wrong_user"})
        if body:
            sender(str(group_chat_id), message=body)
        return 2
    except SshsignSessionError as e:
        sys.stderr.write(f"bind-group failed: {e}\n")
        return 3

    # Success: post confirmation IN THE GROUP (not the DM).
    counterparty_label = (
        (meta.get("investor_name") or "") + (
            (", " + meta["investor_firm"]) if meta.get("investor_firm") else ""
        )
    ).strip(", ") or "your investor"
    body = format_event({
        "type": "group_bound",
        "session_code": sess.get("session_code") or session_code,
        "counterparty_label": counterparty_label,
    })
    if body:
        sender(str(group_chat_id), message=body)

    # P7-5: if the investor joined BEFORE this /bind turn landed, the
    # session is already in `joined` state. Don't wait for cron —
    # resume inline. The founder's process is live for this turn;
    # scan would eventually handle it, but the UX is better if we
    # skip the wait and stream now.
    status = (sess.get("status") or "").lower()
    if status == "joined":
        negotiation_id = _negotiation_id_from_sshsign_session_id(session_id)
        state = state_store.read_state(negotiation_id)
        if state is not None:
            # Fire-and-forget return value; resume owns its own exit code.
            _run_founder_resume(state, session_client=client, sender=sender)

    return 0


def run_setup(
    message: str,
    chat_id_flag: str | None = None,
    sender=send_telegram,
    persister=_persist_env_updates,
) -> int:
    """Parse the user's self-intro, persist identity env vars, and — if a
    stashed negotiation message exists from a prior first-run attempt —
    hand off to prepare automatically so the user doesn't have to retype.
    """
    chat_id = resolve_chat_id(chat_id_flag)
    typing = TypingLoop(chat_id=chat_id or "", bot_token=get_bot_token())
    typing.start()

    try:
        try:
            identity = extract_identity(message)
        except (ValueError, RuntimeError) as e:
            sys.stderr.write(f"Identity parse error: {e}\n")
            if chat_id:
                sender(chat_id, message=(
                    "\u26a0\ufe0f I couldn't read that. Try: "  # ⚠️
                    "\"I'm Name, Title at Company\" or \"Name, Firm\"."
                ))
            return 1

        if not identity.get("name"):
            if chat_id:
                sender(chat_id, message=(
                    "\u26a0\ufe0f I need at least your name. "
                    "Try: \"I'm Juan Figuera, CEO of APOA Inc\"."
                ))
            return 1

        updates = _build_env_updates(identity)
        failures = persister(updates)
        if failures:
            sys.stderr.write(f"Failed to persist: {failures}\n")
            if chat_id:
                sender(chat_id, message=(
                    "\u26a0\ufe0f Saved partial profile. "
                    f"Couldn't write: {', '.join(failures)}. "
                    "The admin can fix via `openclaw config set`."
                ))

        if chat_id:
            who = identity.get("name", "you")
            where = identity.get("company") or identity.get("firm") or ""
            hello = f"\u2705 Got it, {who}"  # ✅
            if where:
                hello += f" at {where}"
            hello += ". I'll remember for next time."
            sender(chat_id, message=hello)
    finally:
        typing.stop()

    # If the user tried to negotiate BEFORE setup, we stashed their request —
    # pick it up now and hand off to prepare so they don't have to retype.
    if IDENTITY_SENTINEL_PATH.exists():
        try:
            pending = IDENTITY_SENTINEL_PATH.read_text().strip()
            IDENTITY_SENTINEL_PATH.unlink()
        except OSError:
            pending = ""
        if pending:
            # Env updates are hot-reloaded, but this Python process's
            # os.environ is a snapshot from spawn time. Apply the updates
            # in-process so the downstream prepare + mint see them.
            for key, value in updates.items():
                os.environ[key] = value
            if chat_id:
                sender(chat_id, message="Picking up your negotiation request\u2026")
            return run_prepare(
                message=pending,
                output_dir="/tmp/safe_negotiate",
                chat_id_flag=chat_id_flag,
                sender=sender,
            )

    return 0


_SIGNED_MARKER = ".signed"


def _mark_signed(output_dir: Path) -> None:
    """Drop a sentinel file indicating that the user's envelope was approved.

    Used by run_cancel to distinguish cancel-before-sign (→ `canceled`)
    from cancel-after-sign (→ `rescinded_after_sign`). A plain file beats
    re-querying sshsign since the state needs to survive across processes
    (user signs, then later says "cancel" as a fresh invocation).
    """
    try:
        (output_dir / _SIGNED_MARKER).write_text("1")
    except OSError:
        pass


def _has_signed(output_dir: Path) -> bool:
    return (output_dir / _SIGNED_MARKER).exists()


def run_cancel(
    output_dir: str,
    chat_id_flag: str | None = None,
    sender=send_telegram,
    session_client=None,
) -> int:
    """Cancel an in-flight negotiation.

    State model (see PLAN.md PR J5):
      state 1/2: open/joined → cancel-session, push "canceled before sign"
      state 3:   user has signed → cancel-session --rescind, push rescinded
      state 4:   completed → refuse, push "already executed"

    State 1 vs state 2 (before-deal vs after-deal-pre-sign) is not
    distinguished here: both route through cancel-session with the same
    user-facing copy for the cancel initiator. The observer-side copy
    is pushed by each OC's own wait/poll loop when it detects the
    status transition.
    """
    out = Path(output_dir)
    chat_id = resolve_chat_id(chat_id_flag)

    mint_path = out / "mint.json"
    if not mint_path.exists():
        if chat_id:
            sender(chat_id, message=(
                "\u2139\ufe0f No active negotiation to cancel."  # ℹ
            ))
        return 2

    try:
        mint = json.loads(mint_path.read_text())
    except json.JSONDecodeError:
        return 2

    session_id = _sshsign_session_id(mint.get("negotiation_id") or "")
    if not session_id:
        if chat_id:
            sender(chat_id, message=(
                "\u26a0\ufe0f Couldn't find a session to cancel."  # ⚠
            ))
        return 2

    client = session_client or SshsignSession(
        host=os.environ.get("SSHSIGN_HOST", "sshsign.dev"),
    )

    # State 4 check: refuse if already completed.
    try:
        sess = client.get_session(session_id=session_id)
    except SshsignSessionError as e:
        sys.stderr.write(f"get-session on cancel: {e}\n")
        if chat_id:
            sender(chat_id, message=(
                "\u26a0\ufe0f Couldn't reach the signing service. "  # ⚠
                "Try again in a moment."
            ))
        return 3
    status = (sess.get("status") or "").lower()
    if status == "completed":
        body = format_event({"type": "cancel_completed_refused"})
        if chat_id and body:
            sender(chat_id, message=body)
        return 1
    if status in ("canceled", "rescinded_after_sign", "expired"):
        # Already in a terminal state — nothing to do. Tell the user so
        # they don't think the command silently failed.
        if chat_id:
            sender(chat_id, message=(
                f"\u2139\ufe0f This negotiation is already {status.replace('_', ' ')}."  # ℹ
            ))
        return 0

    # State 1/2 vs 3 split based on local .signed marker.
    rescind = _has_signed(out)
    try:
        client.cancel_session(session_id=session_id, rescind=rescind)
    except SshsignSessionError as e:
        sys.stderr.write(f"cancel-session failed: {e}\n")
        if chat_id:
            sender(chat_id, message=(
                "\u26a0\ufe0f Couldn't cancel — signing service error. "  # ⚠
                "Try again, or contact support if it persists."
            ))
        return 3

    if rescind:
        body = format_event({"type": "rescinded_after_sign_initiator"})
    else:
        body = format_event({"type": "canceled_before_deal_initiator"})
    if chat_id and body:
        sender(chat_id, message=body)
    return 0


def run_profile(chat_id_flag: str | None = None, sender=send_telegram) -> int:
    """Show the user's saved identity (read from env vars) as a chat card."""
    chat_id = resolve_chat_id(chat_id_flag)
    profile = {
        "founder_name": os.environ.get("FOUNDER_NAME", ""),
        "founder_title": os.environ.get("FOUNDER_TITLE", ""),
        "company_name": os.environ.get("COMPANY_NAME", ""),
        "investor_name": os.environ.get("INVESTOR_NAME", ""),
        "investor_firm": os.environ.get("INVESTOR_FIRM", ""),
    }
    body = format_event({"type": "profile", "profile": profile})
    if not body:
        return 2
    if chat_id:
        sender(chat_id, message=body)
    else:
        sys.stdout.write(body + "\n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="SAFE negotiation skill")
    sub = parser.add_subparsers(dest="command")

    prep = sub.add_parser("prepare", help="Parse NL message into constraints")
    prep.add_argument("--message", default="", help="The negotiation request text")
    prep.add_argument("--message-file", default="", help="Path to file containing the request (avoids shell $ expansion)")
    prep.add_argument("--output-dir", required=True)
    prep.add_argument("--founder-name", default=os.environ.get("FOUNDER_NAME", ""))
    prep.add_argument("--founder-title", default="CEO")
    prep.add_argument(
        "--chat-id",
        default="",
        help="Telegram chat id to push the confirm card to (falls back to openclaw sessions.json)",
    )

    neg = sub.add_parser("negotiate", help="Mint tokens and run negotiation")
    neg.add_argument("--output-dir", required=True)
    neg.add_argument(
        "--chat-id",
        default="",
        help="Telegram chat id to push events to (falls back to openclaw sessions.json)",
    )

    setup = sub.add_parser(
        "setup",
        help="Parse a self-introduction and persist identity env vars "
             "(also used for profile updates — any fields present in the "
             "message overwrite the previous values).",
    )
    setup.add_argument("--message", default="", help="Self-intro text")
    setup.add_argument("--message-file", default="")
    setup.add_argument("--chat-id", default="")

    profile = sub.add_parser(
        "profile",
        help="Show the user's currently saved identity",
    )
    profile.add_argument("--chat-id", default="")

    cancel = sub.add_parser(
        "cancel",
        help="Cancel the in-flight negotiation. Automatically chooses "
             "between canceled and rescinded_after_sign based on whether "
             "the user has already signed their side.",
    )
    cancel.add_argument("--output-dir", required=True)
    cancel.add_argument("--chat-id", default="")

    bind = sub.add_parser(
        "bind",
        help="Phase 8: bind a two-party negotiation to the current "
             "Telegram group. Invoked by OC when the founder types "
             "/bind INV-XXXXX in a group.",
    )
    bind.add_argument(
        "--message",
        default="",
        help="The full /bind message body; the code is extracted from it.",
    )
    bind.add_argument(
        "--session-code",
        default="",
        help="Explicit INV-XXXXX (overrides --message parsing).",
    )
    bind.add_argument(
        "--chat-id",
        required=True,
        help="The GROUP chat_id where /bind was typed (negative int).",
    )
    bind.add_argument(
        "--from-id",
        required=True,
        help="The Telegram user_id of the sender of /bind.",
    )
    bind.add_argument(
        "--dm-chat-id",
        default="",
        help="Founder's DM chat_id (positive int). Used to steer the "
             "wrong-chat-type error back to the DM when /bind is typed "
             "in the DM by mistake.",
    )

    sub.add_parser(
        "scan",
        help="P7-5: resume any two-party negotiation whose investor "
             "has joined but the founder hasn't yet started streaming. "
             "Invoked by an OpenClaw cron job; idempotent across ticks.",
    )

    args = parser.parse_args()

    if args.command == "prepare":
        message = args.message
        if not message and args.message_file:
            message = Path(args.message_file).read_text().strip()
        if not message:
            sys.stderr.write("Provide --message or --message-file.\n")
            return 2
        return run_prepare(
            message,
            args.output_dir,
            args.founder_name,
            args.founder_title,
            chat_id_flag=args.chat_id or None,
        )
    elif args.command == "negotiate":
        return run_negotiate(args.output_dir, chat_id_flag=args.chat_id or None)
    elif args.command == "setup":
        message = args.message
        if not message and args.message_file:
            message = Path(args.message_file).read_text().strip()
        if not message:
            sys.stderr.write("Provide --message or --message-file for setup.\n")
            return 2
        return run_setup(message, chat_id_flag=args.chat_id or None)
    elif args.command == "profile":
        return run_profile(chat_id_flag=args.chat_id or None)
    elif args.command == "cancel":
        return run_cancel(args.output_dir, chat_id_flag=args.chat_id or None)
    elif args.command == "bind":
        code = (args.session_code or "").strip().upper()
        if not code:
            code = _extract_bind_code(args.message) or ""
        if not code:
            sys.stderr.write("bind: no INV-XXXXX code found in --session-code or --message.\n")
            return 2
        # OC's Telegram envelope encodes ids as `telegram:<numeric>`;
        # tolerate that prefix plus any leading/trailing whitespace.
        def _coerce_id(raw: str) -> int:
            s = (raw or "").strip()
            if ":" in s:
                s = s.rsplit(":", 1)[-1]
            return int(s)
        try:
            group_chat_id = _coerce_id(args.chat_id)
            from_user_id = _coerce_id(args.from_id)
        except ValueError as e:
            sys.stderr.write(f"bind: --chat-id and --from-id must be integers: {e}\n")
            return 2
        dm_flag = args.dm_chat_id or None
        if dm_flag and ":" in dm_flag:
            dm_flag = dm_flag.rsplit(":", 1)[-1]
        return run_bind(
            session_code=code,
            group_chat_id=group_chat_id,
            from_user_id=from_user_id,
            dm_chat_id_flag=dm_flag,
        )
    elif args.command == "scan":
        return run_scan()
    else:
        parser.print_help()
        return 2


if __name__ == "__main__":
    sys.exit(main())
