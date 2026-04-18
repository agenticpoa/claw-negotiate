#!/usr/bin/env python3
"""Format negotiation events as short Telegram messages.

Reads a JSON event on stdin, writes a formatted message on stdout.
Event 'type' field selects the template. Templates mirror SKILL.md exactly.
"""
from __future__ import annotations

import json
import sys
from typing import Any

CHECK = "\u2713"


def fmt_dollars(n: float | int | None) -> str:
    if n is None:
        return "-"
    return f"${int(n):,}"


def fmt_percent(d: float | None) -> str:
    if d is None:
        return "-"
    return f"{d * 100:.0f}%"


def format_confirm(event: dict[str, Any]) -> str:
    c = event["constraints"]
    pro_rata_label = {
        "required": "required (I won't agree without this)",
        "preferred": "preferred (I'll push for it but can concede)",
        "indifferent": "indifferent",
    }[c["pro_rata"]]
    mfn_label = {
        "required": "required (I won't agree without this)",
        "preferred": "preferred (I'll push for it but can concede)",
        "indifferent": "indifferent",
    }[c["mfn"]]
    return (
        "Got it. Here's what I'll enforce during the negotiation:\n\n"
        f"Valuation cap: {fmt_dollars(c['valuation_cap_min'])} to {fmt_dollars(c['valuation_cap_max'])}\n"
        f"Discount rate: {fmt_percent(c['discount_min'])} or better\n"
        f"Pro-rata rights: {pro_rata_label}\n"
        f"MFN clause: {mfn_label}\n\n"
        "I'll need your approval before signing anything.\n\n"
        'Does this look right? Say "go" to start or correct me.'
    )


def format_authorized(event: dict[str, Any]) -> str:
    return (
        "Authorization signed.\n\n"
        f"Token: {event['tid']}\n"
        f"Scope: {event['service']}\n"
        f"Expires: {event['expires_at']}\n\n"
        f"Revoke anytime: apoa revoke {event['tid']}"
    )


def format_offer(event: dict[str, Any]) -> str:
    terms = event.get("terms", {})
    cap = terms.get("valuation_cap")
    discount = terms.get("discount_rate")
    lines = [f"[Round {event.get('round', '?')} - {event.get('party', '?')}]"]
    if event.get("rationale"):
        lines.append(f'"{event["rationale"].strip()}"')
    lines.extend([
        "",
        f"Cap: {fmt_dollars(cap)} {CHECK} "
        f"(range: {fmt_dollars(event.get('cap_min'))}-{fmt_dollars(event.get('cap_max'))})",
        f"Discount: {fmt_percent(discount)} {CHECK} "
        f"(min: {fmt_percent(event.get('discount_min'))})",
        f"immudb tx: {event.get('immudb_tx', 'pending')}",
    ])
    return "\n".join(lines)


def format_agreed(event: dict[str, Any]) -> str:
    terms = event.get("terms", {})
    return (
        "Agreement reached!\n\n"
        f"Cap: {fmt_dollars(terms.get('valuation_cap'))}\n"
        f"Discount: {fmt_percent(terms.get('discount_rate'))}\n"
        f"Pro-rata: {'yes' if terms.get('pro_rata') else 'no'}\n"
        f"MFN: {'yes' if terms.get('mfn') else 'no'}\n\n"
        f"All terms within your authorization {CHECK}\n\n"
        "Generating SAFE document..."
    )


def format_cosign_requested(event: dict[str, Any]) -> str:
    pending_id = event["pending_id"]
    key_path = event.get("sshsign_key_path", "$SSHSIGN_KEY_PATH")
    return (
        "SAFE document generated. Waiting for your co-sign.\n\n"
        "Approve from any terminal:\n"
        f"  ssh -i {key_path} sshsign.dev approve --id {pending_id}\n\n"
        "I'll confirm once signed."
    )


def format_signed(event: dict[str, Any]) -> str:
    tx = event.get("audit_tx", "?")
    terms = event.get("terms", {})
    pro_rata_tag = ", pro-rata" if terms.get("pro_rata") else ""
    return (
        "Signed!\n\n"
        "Document: SAFE Agreement\n"
        f"Terms: {fmt_dollars(terms.get('valuation_cap'))} cap, "
        f"{fmt_percent(terms.get('discount_rate'))} discount{pro_rata_tag}\n"
        f"Audit TX: {tx}\n"
        f"Verify: sshsign.dev/verify/{tx}\n\n"
        f"Full negotiation: {event.get('total_offers', '?')} offers "
        f"in {event.get('duration_seconds', '?')} seconds.\n"
        "All entries cryptographically verified."
    )


def format_canceled(event: dict[str, Any]) -> str:
    return f"Negotiation canceled. Token {event.get('tid', '?')} revoked."


def format_expired(event: dict[str, Any]) -> str:
    return (
        "APOA token expired mid-negotiation. Protocol halted.\n\n"
        "Mint a fresh token to continue, or cancel."
    )


def format_deadlock(event: dict[str, Any]) -> str:
    founder = event.get("founder_final", {})
    investor = event.get("investor_final", {})
    return (
        "No agreement reached after 10 rounds.\n\n"
        f"Founder final: cap {fmt_dollars(founder.get('valuation_cap'))}, "
        f"discount {fmt_percent(founder.get('discount_rate'))}\n"
        f"Investor final: cap {fmt_dollars(investor.get('valuation_cap'))}, "
        f"discount {fmt_percent(investor.get('discount_rate'))}"
    )


FORMATTERS = {
    "confirm": format_confirm,
    "authorized": format_authorized,
    "offer": format_offer,
    "agreed": format_agreed,
    "cosign_requested": format_cosign_requested,
    "signed": format_signed,
    "canceled": format_canceled,
    "expired": format_expired,
    "deadlock": format_deadlock,
}


def main() -> int:
    try:
        event = json.loads(sys.stdin.read())
    except json.JSONDecodeError as e:
        sys.stderr.write(f"Invalid JSON: {e}\n")
        return 2

    kind = event.get("type", "offer")
    formatter = FORMATTERS.get(kind)
    if formatter is None:
        sys.stderr.write(f"Unknown event type: {kind}. Known: {sorted(FORMATTERS)}\n")
        return 2

    sys.stdout.write(formatter(event))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
