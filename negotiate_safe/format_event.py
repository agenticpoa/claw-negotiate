#!/usr/bin/env python3
"""Format negotiation events as short chat messages.

OpenClaw's Telegram plugin uses GitHub-flavored Markdown rendering:
  **bold**     — emphasis
  `code`       — inline code (also safe escape for strings with `_` or `*`)
  [text](url)  — explicit links
Single `*` and `_` both render as italic; avoid them in copy per product
direction. Any user-supplied text that might contain `_`, `*`, `` ` ``, or `[`
is escaped via `_escape_md` to prevent accidental formatting.

Event schema reflects what upstream agenticpoa/negotiate emits via
`--json-events` plus two events our wrappers emit themselves:

  offer / counter / accept   (upstream emit_json_event)
  outcome                    (upstream emit_outcome_event)
  signing                    (upstream, when awaiting cosign approval)
  confirm                    (our prepare step)
  authorized                 (our mint step)
  signed                     (synthesized by us after envelope=approved)
"""
from __future__ import annotations

import json
import sys
from typing import Any


# ──────────────────────────────────────────────────────────────
# Formatting helpers
# ──────────────────────────────────────────────────────────────


def fmt_dollars(n: float | int | None) -> str:
    """Smart-shorten dollar amounts: $8M, $12.5M, $500K, $1,000, $12."""
    if n is None:
        return "-"
    n = int(n)
    if n == 0:
        return "$0"
    sign = "-" if n < 0 else ""
    n = abs(n)
    if n >= 1_000_000:
        if n % 1_000_000 == 0:
            return f"{sign}${n // 1_000_000}M"
        return f"{sign}${n / 1_000_000:.1f}M"
    if n >= 10_000 and n % 1000 == 0:
        return f"{sign}${n // 1000}K"
    return f"{sign}${n:,}"


def fmt_percent(d: float | None) -> str:
    if d is None:
        return "-"
    return f"{d * 100:.0f}%"


def _yn(flag: Any) -> str:
    return "yes" if flag else "no"


def _escape_md(s: str) -> str:
    """Escape Markdown formatting chars for use in plain text spans."""
    return (
        s.replace("\\", "\\\\")
         .replace("_", "\\_")
         .replace("*", "\\*")
         .replace("`", "\\`")
         .replace("[", "\\[")
    )


def _format_founder_identity(
    founder_name: str | None,
    founder_title: str | None,
    company_name: str | None,
) -> str | None:
    if founder_name and founder_title and company_name:
        return f"{founder_name}, {founder_title} of {company_name}"
    if founder_name and founder_title:
        return f"{founder_name}, {founder_title}"
    if founder_name and company_name:
        return f"{founder_name} of {company_name}"
    if founder_name:
        return founder_name
    return company_name or None


def _format_investor_identity(
    investor_name: str | None,
    investor_firm: str | None,
) -> str | None:
    if investor_name and investor_firm:
        return f"{investor_name} at {investor_firm}"
    return investor_name or investor_firm or None


_PARTY_ICON = {"founder": "\U0001f464", "investor": "\U0001f4bc"}  # 👤, 💼

_OFFER_LABELS = {"offer": "Offer", "counter": "Counter", "accept": "Accepted"}


# ──────────────────────────────────────────────────────────────
# Wrapper-side events
# ──────────────────────────────────────────────────────────────


def format_confirm(event: dict[str, Any]) -> str:
    c = event["constraints"]
    labels = {
        "required": "required",
        "preferred": "preferred",
        "indifferent": "no preference",
    }
    mfn_labels = {
        "required": "required",
        "preferred": "preferred",
        "indifferent": "no preference",
    }
    role = (c.get("role") or "founder").lower()
    role_label = "founder" if role == "founder" else "investor"
    role_icon = "\U0001f464" if role == "founder" else "\U0001f4bc"  # 👤 / 💼

    founder_name = c.get("founder_name")
    founder_title = c.get("founder_title")
    company_name = c.get("company_name")
    investor_name = c.get("investor_name")
    investor_firm = c.get("investor_firm")

    # Build "You" and "Counterparty" lines based on the user's role. Show
    # only what was captured — omit missing pieces so the line stays clean.
    founder_line = _format_founder_identity(
        founder_name, founder_title, company_name,
    )
    investor_line = _format_investor_identity(investor_name, investor_firm)

    if role == "founder":
        you_line = founder_line
        counterparty_line = investor_line
        confirm_cta = "Reply **GO** to create the invitation code, or send edits."
    else:
        you_line = investor_line
        counterparty_line = founder_line
        confirm_cta = "Reply **GO** to join the negotiation, or send edits."

    identity_lines = [
        f"{role_icon} **Review your {role_label}-side authorization**",
    ]
    if you_line:
        identity_lines.append(f"**You:** {you_line}")
    if counterparty_line:
        identity_lines.append(f"**Counterparty:** {counterparty_line}")

    lines = [
        "\n".join(identity_lines),
        "",
        "Your agent will only agree to terms within these limits:",
        "",
        f"• Valuation cap: **{fmt_dollars(c['valuation_cap_min'])} – {fmt_dollars(c['valuation_cap_max'])}**",
        f"• Discount: **at least {fmt_percent(c['discount_min'])}**",
        f"• Pro-rata rights: {labels[c['pro_rata']]}",
        f"• MFN: {mfn_labels[c['mfn']]}",
        "",
        "You will personally review and sign any final SAFE.",
        "",
        confirm_cta,
    ]
    return "\n".join(lines)


def format_authorized(event: dict[str, Any]) -> str:
    """Authorization card, framed user-first.

    Fires once after mint completes, before the first round. Shows the
    bounds in plain English so the user knows what their agent can and
    can't do. APOA is cited in a lightweight footer, not the headline —
    the concept earns its keep on the next screens (constraint
    violations, signed audit trail).
    """
    c = event.get("constraints") or {}
    cap_min = c.get("valuation_cap_min")
    cap_max = c.get("valuation_cap_max")
    disc_min = c.get("discount_min")
    pro_rata = (c.get("pro_rata") or "indifferent").lower()
    mfn = (c.get("mfn") or "indifferent").lower()

    pr_labels = {
        "required": "required",
        "preferred": "preferred",
        "indifferent": "no preference",
    }
    ttl_hours = event.get("ttl_hours") or 1

    lines = [
        "\U0001f512 **Your agent's authorization is set**",  # 🔒
        "",
        "Your agent will only agree to terms within these limits:",
        "",
    ]
    if cap_min is not None and cap_max is not None:
        lines.append(
            f"• Cap: **{fmt_dollars(cap_min)} – {fmt_dollars(cap_max)}**"
        )
    if disc_min is not None:
        lines.append(f"• Discount: **at least {fmt_percent(disc_min)}**")
    lines.append(f"• Pro-rata rights: {pr_labels.get(pro_rata, pro_rata)}")
    lines.append(f"• MFN: {pr_labels.get(mfn, mfn)}")
    lines.extend([
        "",
        f"\u23f1\ufe0f  Valid for {ttl_hours} hour"  # ⏱
        f"{'s' if ttl_hours != 1 else ''}. Reply \"cancel\" anytime to revoke.",
        "",
        "_Powered by APOA: every offer your agent makes is "
        "cryptographically bound to this authorization._",
    ])
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────
# Upstream events (from --json-events)
# ──────────────────────────────────────────────────────────────


def format_offer(
    event: dict[str, Any],
    constraints: dict[str, Any] | None = None,
) -> str:
    """Render an offer/counter/accept event.

    offer/counter → round card showing the party, rationale, and terms.
    accept        → celebration card ("🤝 Deal!") with agreed terms. The
                    accept event is the last event before signing, so this
                    is the user's big win-moment and should feel celebratory.
    """
    etype = event.get("type", "offer")
    if etype == "accept":
        return _format_accept(event)

    label = _OFFER_LABELS.get(etype, "Offer")
    party_raw = (event.get("party") or "").lower()
    icon = _PARTY_ICON.get(party_raw, "")
    party = party_raw.capitalize() or "?"
    round_num = event.get("round", "?")

    terms = event.get("terms") or {}
    cap = terms.get("valuation_cap")
    discount = terms.get("discount_rate")

    # In solo-demo (user plays one role, AI plays the other), clearly
    # label the AI side as "(AI)" so no one mistakes the agent's counter
    # for a real person. In two_party mode both sides are humans; no
    # suffix. `user_role` comes from the constraints/mint context.
    mode = (constraints or {}).get("mode") if constraints else None
    user_role = ((constraints or {}).get("role") or "").lower() if constraints else ""
    ai_suffix = ""
    if mode == "demo" and user_role and party_raw and party_raw != user_role:
        ai_suffix = " (AI)"

    header_prefix = f"{icon} " if icon else ""
    header = f"{header_prefix}**Round {round_num} — {party}{ai_suffix}**"
    lines = [header]

    message = (event.get("message") or "").strip()
    if message:
        lines.append(f'"{_escape_md(message)}"')

    # PRIVACY: in two-party mode round cards land in the SHARED group
    # where the counterparty can read them. Each side's bounds are
    # private. Suppress the "your range" / "your min" annotations
    # entirely in two-party. Demo mode renders to the user's own DM,
    # so the hint is safe to keep there.
    if constraints and mode != "two_party":
        cap_min = constraints.get("valuation_cap_min")
        cap_max = constraints.get("valuation_cap_max")
        disc_min = constraints.get("discount_min")
        cap_range = f" (your range: {fmt_dollars(cap_min)}–{fmt_dollars(cap_max)})" if cap_min is not None else ""
        disc_range = f" (your min: {fmt_percent(disc_min)})" if disc_min is not None else ""
    else:
        cap_range = disc_range = ""

    lines.extend([
        "",
        f"• Cap: **{fmt_dollars(cap)}**{cap_range}",
        f"• Discount: **{fmt_percent(discount)}**{disc_range}",
        f"• Pro-rata: {_yn(terms.get('pro_rata'))}",
        f"• MFN: {_yn(terms.get('mfn'))}",
    ])

    return "\n".join(lines)


def _format_accept(event: dict[str, Any]) -> str:
    """Celebration card rendered on the accept event (fires before outcome).

    Pulling the celebration forward to the accept event (instead of waiting
    for outcome) eliminates the ~second-long delay upstream introduces
    between the two; user sees the deal land the instant it's struck.
    """
    terms = event.get("terms") or {}
    return (
        "\U0001f91d **Deal!**\n\n"  # 🤝
        "Terms agreed:\n"
        f"• Cap: **{fmt_dollars(terms.get('valuation_cap'))}**\n"
        f"• Discount: **{fmt_percent(terms.get('discount_rate'))}**\n"
        f"• Pro-rata: {_yn(terms.get('pro_rata'))}\n"
        f"• MFN: {_yn(terms.get('mfn'))}"
    )


def format_outcome(
    event: dict[str, Any],
    constraints: dict[str, Any] | None = None,
) -> str | None:
    """Render only failure outcomes.

    For `result=accepted`, return None so the caller skips it — the preceding
    `accept` event has already shown the full terms card with an "Accepted"
    header, and the upcoming `signing` event is the next clear action. Emitting
    a redundant "Deal!" card between them just adds a ~second of delay with no
    new information.

    For `result=max_rounds`, we're in a no-ZOPA case: the two parties' ranges
    didn't overlap. Spell that out in plain English so the user understands
    WHY we stopped and knows the next move is to revisit their bounds.
    """
    result = event.get("result")

    if result == "accepted":
        return None
    if result == "max_rounds":
        cap_min = (constraints or {}).get("valuation_cap_min") if constraints else None
        cap_max = (constraints or {}).get("valuation_cap_max") if constraints else None
        lines = ["\U0001f937 **No agreement reached.**"]  # 🤷
        if cap_min is not None and cap_max is not None:
            lines.append("")
            lines.append(
                "Your range and your counterparty's didn't overlap enough "
                "to close on terms."
            )
            lines.append(
                f"Your cap range was **{fmt_dollars(cap_min)} – "
                f"{fmt_dollars(cap_max)}**."
            )
        else:
            lines.append("")
            lines.append(
                "Your range and your counterparty's didn't overlap. "
                "No SAFE was executed."
            )
        return "\n".join(lines)
    if result == "rejected":
        return "Negotiation rejected. No agreement reached."
    return None


def format_propose_new_terms(event: dict[str, Any]) -> str:
    """Follow-up card pushed after a no-ZOPA outcome.

    Invites the user to try again with updated bounds. Kept intentionally
    short — the outcome card did the explaining; this one just points at
    the next action.
    """
    counterparty = (event.get("counterparty_label") or "").strip()
    if counterparty:
        with_line = f" with {counterparty}"
    else:
        with_line = ""
    return (
        "\U0001f504 **Try again?**\n\n"  # 🔄
        f"Reply with updated terms and I'll start a new negotiation{with_line}. "
        "Example: \"Negotiate again, cap $15M-$25M, 15% discount.\""
    )


def format_signing(event: dict[str, Any]) -> str:
    approval_url = (event.get("approval_url") or "").strip()
    pending_id = event.get("pending_id") or ""

    if approval_url:
        # Do NOT escape the URL — Telegram auto-detects and links it. Adding
        # backslash escapes breaks the underscore in `pnd_XXX` when Telegram
        # hands the URL back to sshsign (observed: "Invalid pending ID").
        return (
            "\u270d\ufe0f **Almost done — your signature, please.**\n\n"  # ✍️
            "Tap the link below and draw your signature. "
            "I'll share the executed SAFE here within a minute of signing.\n\n"
            f"{approval_url}"
        )
    if pending_id:
        return (
            "Awaiting your co-signature.\n\n"
            f"Approve from any terminal:\n`ssh sshsign.dev approve --id {pending_id}`"
        )
    return "Awaiting your co-signature."


# ──────────────────────────────────────────────────────────────
# Post-envelope synthesized event
# ──────────────────────────────────────────────────────────────


def format_signed(event: dict[str, Any]) -> str:
    terms = event.get("terms") or {}
    cap = terms.get("valuation_cap")
    discount = terms.get("discount_rate")
    pro_rata = terms.get("pro_rata")

    if cap is not None or discount is not None:
        lines = ["\u2705 **Signed & sealed.**", "", "Here's your executed SAFE:"]
        if cap is not None:
            lines.append(f"• Cap: {fmt_dollars(cap)}")
        if discount is not None:
            lines.append(f"• Discount: {fmt_percent(discount)}")
        if pro_rata is not None:
            lines.append(f"• Pro-rata: {_yn(pro_rata)}")
        lines.append("")
        lines.append(
            "Full audit trail is available on sshsign.dev if you ever need to "
            "prove authenticity."
        )
        return "\n".join(lines)
    return (
        "\u2705 **Signed & sealed.**\n\n"
        "Full audit trail is available on sshsign.dev if you ever need to "
        "prove authenticity."
    )


# ──────────────────────────────────────────────────────────────
# Dispatcher + CLI
# ──────────────────────────────────────────────────────────────


def format_profile(event: dict[str, Any]) -> str:
    """Render the user's saved identity (from env vars) as a chat card.

    Only shows lines for fields that are set — missing fields quietly
    disappear so the card doesn't advertise placeholders.
    """
    p = event.get("profile") or {}
    founder_name = (p.get("founder_name") or "").strip()
    founder_title = (p.get("founder_title") or "").strip()
    company_name = (p.get("company_name") or "").strip()
    investor_name = (p.get("investor_name") or "").strip()
    investor_firm = (p.get("investor_firm") or "").strip()

    has_founder = bool(founder_name or founder_title or company_name)
    has_investor = bool(investor_name or investor_firm)

    if not has_founder and not has_investor:
        return (
            "\U0001f464 Your profile is empty. "  # 👤
            "Say \"I'm Name, Title at Company\" to set it up."
        )

    lines = ["\U0001f464 **Your saved profile**"]  # 👤

    if has_founder:
        lines.append("")
        lines.append("**Founder side** \U0001f464")  # 👤
        if founder_name:
            lines.append(f"• Name: **{founder_name}**")
        if founder_title:
            lines.append(f"• Title: {founder_title}")
        if company_name:
            lines.append(f"• Company: {company_name}")

    if has_investor:
        lines.append("")
        lines.append("**Investor side** \U0001f4bc")  # 💼
        if investor_name:
            lines.append(f"• Name: **{investor_name}**")
        if investor_firm:
            lines.append(f"• Firm: {investor_firm}")

    lines.append("")
    lines.append("To update, say something like: "
                 "\"Update my profile — I'm now Jane Smith, CFO of NewCo\".")
    return "\n".join(lines)


def format_invitation(event: dict[str, Any]) -> str:
    """Founder-side card shown after a two-party session is created.

    Inverted-invitation design: the founder doesn't know the
    investor's bot handle, so we cannot author a tap-to-join Telegram
    link. Instead, hand the founder a single copy-pasteable block
    that includes BOTH the session code and the founder's own bot
    handle. The investor's job is to paste this exact text to their
    own bot — the "via @<founder>" form is what the investor's parse
    layer extracts, with sshsign's metadata as the authoritative
    backstop.

    Required event fields: session_code, founder_bot_handle.
    Optional: expires_at, ttl_hours, counterparty_label.
    """
    code = (event.get("session_code") or "").strip()
    founder_bot = (event.get("founder_bot_handle") or "").strip()
    if founder_bot and not founder_bot.startswith("@"):
        founder_bot = "@" + founder_bot
    expires_at = event.get("expires_at") or ""
    ttl_h = event.get("ttl_hours") or 24
    counterparty = (event.get("counterparty_label") or "your investor").strip()

    # Pre-build the exact join template the investor should paste.
    # Including a placeholder line for "your terms" makes the
    # required edit obvious; the investor knows to swap it for their
    # own cap/discount/etc. without us having to explain.
    identity_fragment = ""
    investor_name = (event.get("investor_name") or "").strip()
    investor_firm = (event.get("investor_firm") or "").strip()
    if investor_name and investor_firm:
        identity_fragment = f"I am {investor_name} at {investor_firm}, "
    elif investor_name:
        identity_fragment = f"I am {investor_name}, "
    elif investor_firm:
        identity_fragment = f"I am at {investor_firm}, "

    join_template = (
        f"Joining {code} via {founder_bot}, "
        f"{identity_fragment}cap up to $X, Y% discount, pro-rata required"
        if founder_bot
        else f"Joining {code} as investor, {identity_fragment}cap up to $X, "
             "Y% discount, pro-rata required"
    )

    lines = [
        "🤝 **Ready to start live negotiation.**",  # 🤝
        "",
        f"**Send this setup note to {counterparty}** "
        "(via Signal, SMS, email — whatever you already use):",
        "",
        "─────────────────────────",
        "Please join our SAFE negotiation.",
        "",
        "DM your investor agent on Telegram with this template, replacing "
        "the cap and discount with your investor-side limits:",
        "",
        f"`{join_template}`",
        "─────────────────────────",
        "",
    ]
    # If founder_bot is empty we silently fall back to the generic
    # template above. The op-side warning that used to live here was
    # confusing to end users (it was meant for ops). Misconfig logs
    # to stderr from the call site instead.

    lines.append("**While you wait:**")
    if expires_at:
        lines.append(
            f"• I'll notify you the moment {counterparty} joins. "
            f"The code is valid for {ttl_h} hours."
        )
    else:
        lines.append(f"• I'll notify you the moment {counterparty} joins.")
    lines.append(
        "• Once they're in, I'll tell you how to set up the live group "
        "chat for round-by-round visibility."
    )
    lines.append("• Reply \"cancel\" anytime to revoke and tear it down.")
    return "\n".join(lines)


def _fmt_hhmm(total_seconds: float) -> str:
    """Render a duration as HH:MM (e.g., '23:47')."""
    total = max(0, int(total_seconds))
    hours = total // 3600
    minutes = (total % 3600) // 60
    return f"{hours:02d}:{minutes:02d}"


def format_waiting(event: dict[str, Any]) -> str:
    """Periodic status card while the founder waits for their counterparty.

    Not fired on every poll — only when the wait has crossed a notable
    threshold (e.g., 1h, 6h, 12h elapsed) so the user doesn't get spammed.
    Shows a countdown to expiration so the user knows how much runway is
    left on the invitation.
    """
    elapsed_minutes = int(event.get("elapsed_minutes") or 0)
    remaining_hours = event.get("remaining_hours")
    lines = [
        f"\u23f3 Still waiting for your counterparty to join."  # ⏳
        f" ({elapsed_minutes} min elapsed)",
    ]
    if remaining_hours is not None:
        rem_seconds = float(remaining_hours) * 3600
        lines.append(
            f"Invitation expires in **{_fmt_hhmm(rem_seconds)}**. "
            "Share the code soon."
        )
    return "\n".join(lines)


def format_counterparty_joined(event: dict[str, Any]) -> str:
    """Fires the moment the other OC joins the session — flips founder OC
    from waiting to active-negotiation mode."""
    who = (event.get("counterparty_label") or "your counterparty").strip()
    return f"\u2705 {who} joined. Starting the negotiation\u2026"  # ✅


def format_invitation_expired(event: dict[str, Any]) -> str:
    return (
        "\u23f0 Your invitation expired before anyone joined.\n\n"  # ⏰
        "Say \"negotiate my SAFE with \u2026\" again to create a new one."
    )


def format_session_expired(event: dict[str, Any]) -> str:
    """Mid-negotiation APOA/session expiration.

    Distinct from invitation_expired (which fires before the counterparty
    joins) — this one fires after things were in flight but the session
    ran out of time before both parties signed.
    """
    return (
        "\u23f0 Your negotiation session expired mid-flight.\n\n"  # ⏰
        "No SAFE was executed. Your APOA authorization is no longer valid "
        "for this session. To pick up where you left off, say \"negotiate "
        "my SAFE with \u2026\" again."
    )


def _who_label(event: dict[str, Any]) -> str:
    who = (event.get("by") or "").strip()
    return who or "The other party"


def format_canceled_before_deal_initiator(event: dict[str, Any]) -> str:
    """You canceled — no agreement had been reached yet. Includes the
    session code so the user can confirm WHICH negotiation got
    canceled (especially when rapidly minting + canceling).
    """
    code = (event.get("session_code") or "").strip()
    code_line = f" **{code}**" if code else ""
    return (
        f"\u274c **Negotiation{code_line} canceled.**\n\n"  # ❌
        "No SAFE was executed. Your APOA authorization has been revoked."
    )


def format_canceled_before_deal_observer(event: dict[str, Any]) -> str:
    """Your counterparty canceled — no agreement had been reached yet."""
    who = _who_label(event)
    return (
        f"\u274c {who} stopped negotiating before an agreement was reached.\n\n"  # ❌
        "No SAFE was executed."
    )


def format_canceled_after_deal_initiator(event: dict[str, Any]) -> str:
    """You revoked the agreed deal before either side signed."""
    return (
        "\u274c You revoked the agreed deal before signing.\n\n"  # ❌
        "Your counterparty has been notified. No SAFE will be executed."
    )


def format_canceled_after_deal_observer(event: dict[str, Any]) -> str:
    who = _who_label(event)
    return (
        f"\u274c {who} revoked the agreed deal before signing.\n\n"  # ❌
        "No SAFE will be executed. The negotiation is closed."
    )


def format_rescinded_after_sign_initiator(event: dict[str, Any]) -> str:
    """You rescinded AFTER signing — your signature stays on record but
    the deal does not execute. Includes the session code so the user
    knows WHICH negotiation was rescinded.
    """
    code = (event.get("session_code") or "").strip()
    code_line = f" **{code}**" if code else ""
    return (
        f"\u26a0\ufe0f **Negotiation{code_line} rescinded after signing.**\n\n"  # ⚠
        "Your signature stays on record for audit purposes, "
        "but the SAFE will NOT execute. Your counterparty has been notified."
    )


def format_rescinded_after_sign_observer(event: dict[str, Any]) -> str:
    who = _who_label(event)
    return (
        f"\u26a0\ufe0f {who} rescinded after signing.\n\n"  # ⚠
        "The SAFE will NOT execute. Their signature is on record but void for this deal."
    )


def format_cancel_completed_deal_refused(event: dict[str, Any]) -> str:
    """User tried to cancel an already-executed SAFE — not allowed."""
    return (
        "\U0001f512 This SAFE is already executed.\n\n"  # 🔒
        "To unwind, you'll need a separate rescission agreement signed by both parties. "
        "I can help draft one — just say so."
    )


def format_go_live(event: dict[str, Any]) -> str:
    """DM card pushed after two-party mint: instructs the founder to
    create a Telegram group with both bots + the investor, then paste
    `/bind INV-XXXXX` in the group.

    Design note: the code and the /bind payload are wrapped in backtick
    inline-code spans because Telegram surfaces long-press-to-copy
    (mobile) and click-to-copy (desktop) on inline code entities. Do NOT
    render `/bind INV-...` as a highlighted bot_command entity — tapping
    a highlighted /command re-sends it FROM THE CURRENT CHAT (the DM),
    which would re-trigger the go-live card in the DM instead of
    binding the group.
    """
    code = (event.get("session_code") or "").strip()
    founder_bot = (event.get("founder_bot") or "@AgenticPOA_bot").strip()
    investor_bot = (event.get("investor_bot") or "@AgenticPOAInvestor_bot").strip()
    counterparty = (event.get("counterparty_handle") or "").strip()

    members = [
        f"  • {founder_bot}  (me)",
        f"  • {investor_bot}  (your investor's agent)",
    ]
    if counterparty:
        members.append(f"  • {counterparty}  (your investor)")
    else:
        members.append("  • your investor (add their @username here)")

    bind_payload = f"/bind {code}" if code else "/bind INV-XXXXX"

    lines = [
        "\U0001f3ac **Want to see both agents negotiate in one chat?**",  # 🎬
        "",
        "Create a new Telegram group with these three members:",
        "",
        *members,
        "",
        "Then paste this in the new group:",
        "",
        f"    `{bind_payload}`",
        "",
        f"_Or share the code_ `{code}` _with your investor via any other "
        "channel to stick with DM-only mode._",
    ]
    return "\n".join(lines)


def format_group_bound(event: dict[str, Any]) -> str:
    """Confirmation card posted IN the group after a successful /bind.
    Tells the founder and investor the venue is set up and what happens
    next."""
    code = (event.get("session_code") or "").strip()
    counterparty = (event.get("counterparty_label") or "your investor").strip()

    lines = [
        f"✅ **Negotiation {code} bound to this group.**",  # ✅
        "",
        f"Both agents will post their offers here so you and {counterparty} "
        "can watch the rounds live.",
        "",
        "_Signing stays private — when it's time to sign, each of you "
        "will get a private link in your own DM._",
    ]
    return "\n".join(lines)


def format_bind_wrong_user(event: dict[str, Any]) -> str:
    """Error reply when someone other than the session founder types /bind."""
    return (
        "⛔ Only the founder of this negotiation can bind it to a group.\n\n"  # ⛔
        "If you're the investor, ask the founder to paste /bind here instead."
    )


def format_bind_wrong_chat_type(event: dict[str, Any]) -> str:
    """Error reply when /bind is typed in a DM instead of a group."""
    return (
        "ℹ️ The `/bind` command only works in a group chat.\n\n"  # ℹ️
        "Create a new Telegram group with the two bots + your investor, "
        "then paste `/bind INV-XXXXX` there."
    )


def format_bind_unknown_code(event: dict[str, Any]) -> str:
    return (
        "❓ That code doesn't match a current negotiation. "  # ❓
        "Double-check the code with your founder."
    )


def format_bind_already_bound(event: dict[str, Any]) -> str:
    return (
        "⚠️ This negotiation is already bound to another group.\n\n"  # ⚠️
        "Cancel with `/cancel_negotiation` in your DM first "
        "if you need to start over."
    )


def format_active_negotiation_block(event: dict[str, Any]) -> str:
    """P7-5+ single-active-negotiation gate. Reject a new-mint attempt
    when there's already an in-flight negotiation on this bot. The
    descriptor identifies the conflict (an INV-XXXXX session code, or
    'a running negotiation' for demo mode).

    Critical: tells the user exactly how to free the slot — `cancel`
    the existing negotiation. The trigger word matches the SKILL.md
    A.7 cancel intent rule, so just typing `cancel` works without
    further qualification.
    """
    descriptor = (event.get("descriptor") or "an active negotiation").strip()
    return (
        f"⛔ You already have **{descriptor}** in progress.\n\n"  # ⛔
        f"Reply `/cancel` to abort it, then start a new one. "
        f"(Each bot handles one negotiation at a time so the agent "
        f"can't accidentally send your terms to the wrong counterparty.)"
    )


def format_founder_resumed(event: dict[str, Any]) -> str:
    """P7-5 orienting card. Posted in the group when an OC cron tick
    (or an in-process /bind fast path) picks up a waiting negotiation
    and is about to start streaming. Never starts with '/' (invariant
    enforced in tests to prevent any future self-trigger loops).
    """
    code = (event.get("session_code") or "").strip()
    header = "⚡ Founder's agent is back online."  # ⚡
    if code:
        header += f" (Session {code})"
    return (
        f"{header}\n\n"
        "Streaming the first offer in a moment…"
    )


def format_investor_waiting_for_founder(event: dict[str, Any]) -> str:
    """Investor-side, post-join: posted to investor's DM right after
    join completes, before the live group exists. Tells the investor
    the founder side is being prepared and what's about to happen.

    Inverted-invitation design: also surfaces the founder bot handle
    (pulled from sshsign session.metadata_public) so the investor
    knows whose agent they're waiting on.
    """
    founder_bot = (event.get("founder_bot_handle") or "").strip()
    if founder_bot and not founder_bot.startswith("@"):
        founder_bot = "@" + founder_bot
    if founder_bot:
        return (
            "✅ **Joined.** Waiting for the founder's agent.\n\n"  # ✅
            f"Founder agent: {founder_bot}\n\n"
            "They're setting up a Telegram group where the negotiation "
            "rounds will stream live. You'll be invited to it shortly. "
            "No action needed from you — sit tight."
        )
    return (
        "✅ **Joined.** Waiting for the founder's agent.\n\n"  # ✅
        "They're setting up a Telegram group where the negotiation "
        "rounds will stream live. You'll be invited to it shortly. "
        "No action needed from you — sit tight."
    )


def format_create_group_for_founder(event: dict[str, Any]) -> str:
    """Founder-side, post-investor-join: posted to founder's DM the
    moment the cron-driven scan picks up the joined session. Tells
    the founder how to create the live group, with BOTH bot handles
    pre-filled from sshsign session metadata.

    Asymmetric: only the founder gets a "create group" instruction.
    The investor's card is "wait" (above). This prevents both parties
    from racing to create separate groups.
    """
    code = (event.get("session_code") or "").strip()
    founder_bot = (event.get("founder_bot_handle") or "").strip()
    investor_bot = (event.get("investor_bot_handle") or "").strip()
    investor_label = (event.get("investor_label") or "your investor").strip()

    if founder_bot and not founder_bot.startswith("@"):
        founder_bot = "@" + founder_bot
    if investor_bot and not investor_bot.startswith("@"):
        investor_bot = "@" + investor_bot

    # When the investor's bot handle hasn't propagated yet (timing
    # window between join and our cron read), fall back to a
    # placeholder. The card is regenerated on the next scan tick
    # with the real handle. Better than blocking the founder.
    if not investor_bot:
        investor_bot = "(your investor's agent — handle not yet known)"

    lines = [
        f"✅ **{investor_label} joined.** Time to set up the live group.",  # ✅
        "",
        "**Create a Telegram group:**",
        "1. Tap **+** → **New Group**",
        "2. Search and add these members:",
        f"   • `{founder_bot}` (your agent)" if founder_bot else "   • your founder agent",
        f"   • `{investor_bot}`",
        f"   • {investor_label} (their personal Telegram account)",
        "   • You",
        "3. Name the group (e.g. \"SAFE round – Acme + Babes Fund\").",
        "4. In the group, paste:",
        "",
        f"`/bind {code}`",
        "",
        "Both agents will then post offers there round by round so "
        "everyone watches live. Signing stays in your DM (private).",
    ]
    return "\n".join(lines)


def format_investor_waiting_heartbeat(event: dict[str, Any]) -> str:
    """Heartbeat card at ~15s if the founder's agent hasn't yet
    signaled streaming_at. Keeps the chat from feeling dead during
    the cron window. Posted at most once per wait.
    """
    return "⏳ Still waking the founder's agent…"  # ⏳


def format_investor_both_online(event: dict[str, Any]) -> str:
    """Posted the moment founder_streaming_at flips non-null on the
    session row. Bridges the ~1-2s gap between "founder's agent is
    up" and the first offer card arriving from upstream.
    """
    return (
        "✅ Both sides are live — starting the negotiation now."  # ✅
    )


def format_investor_wake_timeout(event: dict[str, Any]) -> str:
    """180s bounded-poll timeout. Rare in practice (cron fires every
    30s), but surfaces clearly if the founder's droplet is offline or
    the cron job failed to install. Mentions the OC-restart hint so
    the user knows what to try first.
    """
    return (
        "⏳ The founder's agent is taking longer than expected.\n\n"  # ⏳
        "If you know the founder, a quick nudge to them (sending any "
        "message to their bot) will force a retry. Otherwise the cron "
        "scan will keep trying — come back in a few minutes."
    )


def format_investor_session_ended(event: dict[str, Any]) -> str:
    """Terminal status detected during the investor's bounded poll.
    Not the primary cancellation notice (the canceling party's turn
    posts the authoritative card); this just tells the investor why
    streaming never started.
    """
    status = (event.get("status") or "ended").lower()
    pretty = {
        "canceled": "canceled",
        "rescinded": "canceled after signing",
        "rescinded_after_sign": "canceled after signing",
        "completed": "completed",
        "expired": "expired",
    }.get(status, status)
    return (
        f"ℹ️ Negotiation {pretty} before streaming started."  # ℹ️
    )


FORMATTERS = {
    "confirm": format_confirm,
    "authorized": format_authorized,
    "offer": format_offer,
    "counter": format_offer,
    "accept": format_offer,
    "outcome": format_outcome,
    "signing": format_signing,
    "signed": format_signed,
    "profile": format_profile,
    "invitation": format_invitation,
    "waiting": format_waiting,
    "counterparty_joined": format_counterparty_joined,
    "invitation_expired": format_invitation_expired,
    "session_expired": format_session_expired,
    "canceled_before_deal_initiator": format_canceled_before_deal_initiator,
    "canceled_before_deal_observer": format_canceled_before_deal_observer,
    "canceled_after_deal_initiator": format_canceled_after_deal_initiator,
    "canceled_after_deal_observer": format_canceled_after_deal_observer,
    "rescinded_after_sign_initiator": format_rescinded_after_sign_initiator,
    "rescinded_after_sign_observer": format_rescinded_after_sign_observer,
    "cancel_completed_refused": format_cancel_completed_deal_refused,
    "propose_new_terms": format_propose_new_terms,
    "go_live": format_go_live,
    "group_bound": format_group_bound,
    "bind_wrong_user": format_bind_wrong_user,
    "bind_wrong_chat_type": format_bind_wrong_chat_type,
    "bind_unknown_code": format_bind_unknown_code,
    "bind_already_bound": format_bind_already_bound,
    "active_negotiation_block": format_active_negotiation_block,
    "founder_resumed": format_founder_resumed,
    # P7-5 investor-side UX
    "investor_waiting_for_founder": format_investor_waiting_for_founder,
    "create_group_for_founder": format_create_group_for_founder,
    "investor_waiting_heartbeat": format_investor_waiting_heartbeat,
    "investor_both_online": format_investor_both_online,
    "investor_wake_timeout": format_investor_wake_timeout,
    "investor_session_ended": format_investor_session_ended,
}


def format_event(
    event: dict[str, Any],
    constraints: dict[str, Any] | None = None,
) -> str | None:
    """Dispatch an event to the right formatter.

    Returns the formatted message body, or None if the event type is unknown
    or a known formatter decides to skip (e.g. unknown outcome.result).
    """
    kind = event.get("type")
    formatter = FORMATTERS.get(kind or "")
    if formatter is None:
        return None
    if formatter is format_offer:
        return format_offer(event, constraints)
    if formatter is format_outcome:
        return format_outcome(event, constraints)
    return formatter(event)


def main() -> int:
    try:
        event = json.loads(sys.stdin.read())
    except json.JSONDecodeError as e:
        sys.stderr.write(f"Invalid JSON: {e}\n")
        return 2

    if not isinstance(event, dict):
        sys.stderr.write("Event must be a JSON object\n")
        return 2

    output = format_event(event)
    if output is None:
        kind = event.get("type", "<missing>")
        sys.stderr.write(f"Unknown event type: {kind}. Known: {sorted(FORMATTERS)}\n")
        return 2

    sys.stdout.write(output)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
