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


_PARTY_ICON = {"founder": "\U0001f464", "investor": "\U0001f4bc"}  # 👤, 💼

_OFFER_LABELS = {"offer": "Offer", "counter": "Counter", "accept": "Accepted"}


# ──────────────────────────────────────────────────────────────
# Wrapper-side events
# ──────────────────────────────────────────────────────────────


def format_confirm(event: dict[str, Any]) -> str:
    c = event["constraints"]
    labels = {
        "required": "required (I won't agree without them)",
        "preferred": "preferred (I'll push for it but can concede)",
        "indifferent": "indifferent",
    }
    mfn_labels = {
        "required": "required (I won't agree without it)",
        "preferred": "preferred (I'll push for it but can concede)",
        "indifferent": "indifferent",
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
    def _format_founder_side() -> str:
        parts = []
        if founder_name and founder_title:
            parts.append(f"{founder_name}, {founder_title}")
        elif founder_name:
            parts.append(founder_name)
        if company_name:
            prefix = "of " if parts else ""
            parts.append(f"{prefix}{company_name}")
        return ", ".join(parts) if parts else None

    def _format_investor_side() -> str:
        parts = []
        if investor_name:
            parts.append(investor_name)
        if investor_firm:
            prefix = "at " if parts else ""
            parts.append(f"{prefix}{investor_firm}")
        return ", ".join(parts) if parts else None

    if role == "founder":
        you_line = _format_founder_side()
        counterparty_line = _format_investor_side()
    else:
        you_line = _format_investor_side()
        counterparty_line = _format_founder_side()

    identity_lines = [f"{role_icon} Negotiating as **{role_label}**."]
    if you_line:
        identity_lines.append(f"**You:** {you_line}")
    if counterparty_line:
        identity_lines.append(f"**Counterparty:** {counterparty_line}")

    return (
        "\n".join(identity_lines) + "\n\n"
        "Please review the terms below and confirm:\n\n"
        f"**Valuation cap:** {fmt_dollars(c['valuation_cap_min'])} – {fmt_dollars(c['valuation_cap_max'])}\n"
        f"**Discount:** {fmt_percent(c['discount_min'])} or better\n"
        f"**Pro-rata rights:** {labels[c['pro_rata']]}\n"
        f"**MFN clause:** {mfn_labels[c['mfn']]}\n\n"
        "I'll need your approval before signing anything.\n\n"
        "If these terms look right, reply **GO**. Otherwise, please send your edits."
    )


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
        "indifferent": "indifferent",
    }
    ttl_hours = event.get("ttl_hours") or 1

    lines = [
        "\U0001f512 **Your agent's authorization is set**",  # 🔒
        "",
        "Your agent will only agree to terms inside these bounds:",
        "",
    ]
    if cap_min is not None and cap_max is not None:
        lines.append(
            f"• Cap: **{fmt_dollars(cap_min)} – {fmt_dollars(cap_max)}** "
            "(cannot go higher, cannot go lower)"
        )
    if disc_min is not None:
        lines.append(f"• Discount: **\u2265 {fmt_percent(disc_min)}**")
    lines.append(f"• Pro-rata rights: {pr_labels.get(pro_rata, pro_rata)}")
    lines.append(f"• MFN clause: {pr_labels.get(mfn, mfn)}")
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

    header_prefix = f"{icon} " if icon else ""
    header = f"{header_prefix}**Round {round_num} — {party}**"
    lines = [header]

    message = (event.get("message") or "").strip()
    if message:
        lines.append(f'"{_escape_md(message)}"')

    if constraints:
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

    The code (`session_code`) is the only piece the user must share — copy
    is optimized for "paste this into Signal / SMS / email and send to your
    investor." Expiration is shown in relative terms.
    """
    code = (event.get("session_code") or "").strip()
    expires_at = event.get("expires_at") or ""
    ttl_h = event.get("ttl_hours") or 24
    counterparty = (event.get("counterparty_label") or "your investor").strip()

    lines = [
        "\U0001f91d **Live negotiation started.**",  # 🤝
        "",
        f"Share this code with {counterparty} so they can join:",
        "",
        f"    **{code}**",
        "",
        "They'll reply to their own bot with something like:",
        f"> Join negotiation {code} as investor. Cap up to $X, $Y% discount, \u2026",
        "",
    ]
    if expires_at:
        lines.append(f"Expires in ~{ttl_h} hours. I'll wait for them to join.")
    else:
        lines.append("I'll wait for them to join.")
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
    """You canceled — no agreement had been reached yet."""
    return (
        "\u274c You canceled the negotiation before any agreement was reached.\n\n"  # ❌
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
    """You rescinded AFTER signing — your signature stays on record but the
    deal does not execute."""
    return (
        "\u26a0\ufe0f You rescinded after signing.\n\n"  # ⚠
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
