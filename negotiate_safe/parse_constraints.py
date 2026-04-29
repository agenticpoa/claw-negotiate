#!/usr/bin/env python3
"""Parse a founder's natural-language SAFE request into APOA-flat constraints.

Reads the message on stdin, writes JSON to stdout matching the schema in
SKILL.md Step 1. Null values mean "ambiguous, ask the user" — do not guess.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

SYSTEM_PROMPT = """You extract structured SAFE negotiation constraints from a user's natural-language message. The user may be negotiating as the FOUNDER (raising money for their company) or as the INVESTOR (evaluating an investment in someone else's company).

Return ONLY a JSON object (no prose, no code fences). It MUST contain ALL 14 of these fields — never omit any, especially `role` and `mode`:

{
  "role": "founder" | "investor",
  "mode": "demo" | "two_party",
  "session_code": <string or null>,
  "founder_bot_handle": <string or null>,
  "valuation_cap_min": <integer dollars>,
  "valuation_cap_max": <integer dollars>,
  "discount_min": <decimal 0-1>,
  "pro_rata": "required" | "preferred" | "indifferent",
  "mfn": "required" | "preferred" | "indifferent",
  "company_name": <string or null>,
  "founder_name": <string or null>,
  "founder_title": <string or null>,
  "investor_name": <string or null>,
  "investor_firm": <string or null>,
  "investment_amount": <float dollars or null>
}

CRITICAL: `role` and `mode` are ALWAYS REQUIRED. Every response must include role as "founder" or "investor" (lowercase) and mode as "demo" or "two_party" (lowercase). Never omit either.

Mode detection rules:
- `"two_party"` when the user indicates a real counterparty is on the other side. Signals include:
    - Phrasing like "with my investor", "my real counterparty", "with Central Park Labs (Alex Smith) who will join separately"
    - Presence of a session code (see session_code rules below — if present, mode is always "two_party")
    - Explicit phrasing like "live negotiation", "real deal", "for a real negotiation"
- `"demo"` for all other cases — the AI plays the counterparty. This is the DEFAULT when the signal is ambiguous. Most messages without an explicit counterparty signal are demo mode.

Session code (`session_code`) rules:
- Always null unless the user is joining an existing session.
- A session code looks like `INV-XXXXX` (dash-separated uppercase alphanumeric, e.g. `INV-7K3X9`).
- Set when the user's message contains a session code to join, e.g. "Join negotiation INV-7K3X9 as investor".
- When `session_code` is set, `role` is typically "investor" (since founders create; investors join) — but respect what the user says.
- When `session_code` is set, `mode` is always "two_party".

Founder bot handle (`founder_bot_handle`) rules:
- Always null on founder-side requests (the founder doesn't address their own bot in their own message).
- On investor-side join messages: extract the founder's Telegram bot handle when mentioned. The conventional shape is "Joining INV-XXXXX via @<founder_bot>" — capture `@<founder_bot>` (with the leading @).
- Common variants to recognize: "via @Bot", "founder bot is @Bot", "founder agent: @Bot".
- Null if the user's message doesn't reference a founder bot handle.

Identity fields (strings or null):
- `company_name`: the startup's company name (same regardless of user's role).
- `founder_name`: the individual founder's full name.
- `founder_title`: the founder's title (CEO, Cofounder, etc.) — omit if not stated.
- `investor_name`: the individual investor's full name (the person signing, not the fund).
- `investor_firm`: the investor's firm, VC, or fund name.
- Founder-side shorthand like "with Nora at Babes Fund", "my investor Nora at Babes Fund",
  or "raising from Nora at Babes Fund" means `investor_name` is "Nora" and
  `investor_firm` is "Babes Fund".
Use null for any field the user did not mention; do not invent names.

Role detection rules:
- If user says "my SAFE", "raise", "on my behalf", uses "my company" language, or mentions the investor as a third party → "founder"
- If user says "evaluate", "review", "investing in", "as an investor", "from this company" language, or mentions the company as the counterparty → "investor"
- When ambiguous, default to "founder"

Other rules:
- If the user doesn't mention pro_rata, default to "preferred".
- If the user doesn't mention mfn, default to "indifferent".
- If a required numeric field is genuinely ambiguous, use null.
- Double-check every number against the original message before returning.
- The constraint values represent what the USER (whoever they are) is authorizing. If the user is the founder, `pro_rata=required` means the deal must include pro-rata. If the user is the investor, `pro_rata=required` means they insist on being granted pro-rata.

Founder-side examples (demo mode — AI plays the investor):

Input: "Negotiate my SAFE. I'm Jane Doe, CEO of Acme Corp, raising from Bay Capital (Mark Stone). Cap $8M-$12M, 20% discount, pro-rata, MFN preferred. $500k investment."
Output: {"role": "founder", "mode": "demo", "session_code": null, "founder_bot_handle": null, "valuation_cap_min": 8000000, "valuation_cap_max": 12000000, "discount_min": 0.20, "pro_rata": "required", "mfn": "preferred", "company_name": "Acme Corp", "founder_name": "Jane Doe", "founder_title": "CEO", "investor_name": "Mark Stone", "investor_firm": "Bay Capital", "investment_amount": 500000.0}

Input: "SAFE for TechCo. Looking for $150M cap, no less than $120M. Discount 15%. Pro-rata is a must. Investment: $1.5M."
Output: {"role": "founder", "mode": "demo", "session_code": null, "founder_bot_handle": null, "valuation_cap_min": 120000000, "valuation_cap_max": 150000000, "discount_min": 0.15, "pro_rata": "required", "mfn": "indifferent", "company_name": "TechCo", "founder_name": null, "founder_title": null, "investor_name": null, "investor_firm": null, "investment_amount": 1500000.0}

Input: "Cap no lower than $5M, up to $10M. Twenty percent discount minimum. No strong feelings on pro-rata or MFN. $250,000."
Output: {"role": "founder", "mode": "demo", "session_code": null, "founder_bot_handle": null, "valuation_cap_min": 5000000, "valuation_cap_max": 10000000, "discount_min": 0.20, "pro_rata": "preferred", "mfn": "indifferent", "company_name": null, "founder_name": null, "founder_title": null, "investor_name": null, "investor_firm": null, "investment_amount": 250000.0}

Founder-side two-party examples (AI does NOT play the investor; a real investor joins separately):

Input: "Live negotiation: I'm Juan Figuera, CEO of APOA Inc. My investor is Alex Smith at Central Park Labs and will join separately. Cap $20M-$50M, 10% discount, pro-rata required, MFN preferred. $500K."
Output: {"role": "founder", "mode": "two_party", "session_code": null, "founder_bot_handle": null, "valuation_cap_min": 20000000, "valuation_cap_max": 50000000, "discount_min": 0.10, "pro_rata": "required", "mfn": "preferred", "company_name": "APOA Inc", "founder_name": "Juan Figuera", "founder_title": "CEO", "investor_name": "Alex Smith", "investor_firm": "Central Park Labs", "investment_amount": 500000.0}

Input: "Real negotiation with my investor. Cap $8M-$12M, 20% discount, pro-rata, MFN preferred. $500K. I'll share the code with them."
Output: {"role": "founder", "mode": "two_party", "session_code": null, "founder_bot_handle": null, "valuation_cap_min": 8000000, "valuation_cap_max": 12000000, "discount_min": 0.20, "pro_rata": "required", "mfn": "preferred", "company_name": null, "founder_name": null, "founder_title": null, "investor_name": null, "investor_firm": null, "investment_amount": 500000.0}

Input: "Live negotiation with Nora at Babes Fund. She'll join separately and I'll share the invitation code. Cap $30M to $40M, 10% discount, pro-rata required."
Output: {"role": "founder", "mode": "two_party", "session_code": null, "founder_bot_handle": null, "valuation_cap_min": 30000000, "valuation_cap_max": 40000000, "discount_min": 0.10, "pro_rata": "required", "mfn": "indifferent", "company_name": null, "founder_name": null, "founder_title": null, "investor_name": "Nora", "investor_firm": "Babes Fund", "investment_amount": null}

Investor-side examples (demo mode — AI plays the founder):

Input: "As Alex Chen from Blue Fund, evaluate an investment in QuantumLabs (CEO Dr. Rivera). Cap between $20M and $40M, need at least a 15% discount. Pro-rata required, MFN nice to have. $500K check."
Output: {"role": "investor", "mode": "demo", "session_code": null, "founder_bot_handle": null, "valuation_cap_min": 20000000, "valuation_cap_max": 40000000, "discount_min": 0.15, "pro_rata": "required", "mfn": "preferred", "company_name": "QuantumLabs", "founder_name": "Dr. Rivera", "founder_title": "CEO", "investor_name": "Alex Chen", "investor_firm": "Blue Fund", "investment_amount": 500000.0}

Input: "Evaluate an investment in Helios Robotics. I can take a cap between $25M and $50M with a 12% discount floor. Pro-rata required, MFN preferred. $500K check."
Output: {"role": "investor", "mode": "demo", "session_code": null, "founder_bot_handle": null, "valuation_cap_min": 25000000, "valuation_cap_max": 50000000, "discount_min": 0.12, "pro_rata": "required", "mfn": "preferred", "company_name": "Helios Robotics", "founder_name": null, "founder_title": null, "investor_name": null, "investor_firm": null, "investment_amount": 500000.0}

Investor joining an existing session (two_party, code supplied):

Input: "Join negotiation INV-7K3X9 as investor. Cap up to $40M, 12% discount floor, pro-rata required. $500K check."
Output: {"role": "investor", "mode": "two_party", "session_code": "INV-7K3X9", "founder_bot_handle": null, "valuation_cap_min": 0, "valuation_cap_max": 40000000, "discount_min": 0.12, "pro_rata": "required", "mfn": "indifferent", "company_name": null, "founder_name": null, "founder_title": null, "investor_name": null, "investor_firm": null, "investment_amount": 500000.0}

Input: "I'm Mark Stone from Blue Fund. Joining INV-3BQ7K as investor. Cap $15M-$30M, 15% discount, pro-rata required, MFN preferred."
Output: {"role": "investor", "mode": "two_party", "session_code": "INV-3BQ7K", "founder_bot_handle": null, "valuation_cap_min": 15000000, "valuation_cap_max": 30000000, "discount_min": 0.15, "pro_rata": "required", "mfn": "preferred", "company_name": null, "founder_name": null, "founder_title": null, "investor_name": "Mark Stone", "investor_firm": "Blue Fund", "investment_amount": null}

Investor join WITH the founder bot handle (the standard inverted-invitation shape):

Input: "Joining INV-7K3X9 via @AgenticPOA_bot, cap up to $40M, 10% discount, pro-rata required."
Output: {"role": "investor", "mode": "two_party", "session_code": "INV-7K3X9", "founder_bot_handle": "@AgenticPOA_bot", "valuation_cap_min": 0, "valuation_cap_max": 40000000, "discount_min": 0.10, "pro_rata": "required", "mfn": "indifferent", "company_name": null, "founder_name": null, "founder_title": null, "investor_name": null, "investor_firm": null, "investment_amount": null}

Input: "Joining INV-7K3X9 via @AgenticPOA_bot, I am Nora Vassileva at SD Fund, cap up to $35M, 10% discount, pro-rata required."
Output: {"role": "investor", "mode": "two_party", "session_code": "INV-7K3X9", "founder_bot_handle": "@AgenticPOA_bot", "valuation_cap_min": 0, "valuation_cap_max": 35000000, "discount_min": 0.10, "pro_rata": "required", "mfn": "indifferent", "company_name": null, "founder_name": null, "founder_title": null, "investor_name": "Nora Vassileva", "investor_firm": "SD Fund", "investment_amount": null}

Input: "Join INV-DEMO99 as investor. Founder bot is @alice_negotiator_bot. Cap $20M-$45M, 12% discount, pro-rata required. $750K."
Output: {"role": "investor", "mode": "two_party", "session_code": "INV-DEMO99", "founder_bot_handle": "@alice_negotiator_bot", "valuation_cap_min": 20000000, "valuation_cap_max": 45000000, "discount_min": 0.12, "pro_rata": "required", "mfn": "indifferent", "company_name": null, "founder_name": null, "founder_title": null, "investor_name": null, "investor_firm": null, "investment_amount": 750000.0}
"""


def extract_constraints(message: str) -> dict[str, Any]:
    try:
        from anthropic import Anthropic
    except ImportError as e:
        raise RuntimeError("anthropic SDK not installed. Run: pip install anthropic") from e

    client = Anthropic()
    # Haiku 4.5 is fast (~sub-second) and the few-shots below keep it accurate
    # for this structured-extraction task. Null-field validation upstream
    # catches ambiguity regardless, so mis-parses degrade gracefully.
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": message}],
    )
    text = response.content[0].text.strip()

    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json\n"):
            text = text[5:]

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"Claude returned non-JSON: {text!r}") from e

    # Defensive: Haiku occasionally omits fields even when the prompt
    # demands them. Ensure every documented field is present (null-ok for
    # the identity fields; role/mode normalized to a valid value).
    if not parsed.get("role"):
        parsed["role"] = "founder"
    parsed["role"] = str(parsed["role"]).lower()
    if parsed["role"] not in ("founder", "investor"):
        parsed["role"] = "founder"

    # Mode: default to "demo" when missing/ambiguous; coerce to "two_party"
    # whenever a session_code is present (the two fields are dependent).
    if not parsed.get("mode"):
        parsed["mode"] = "demo"
    parsed["mode"] = str(parsed["mode"]).lower()
    if parsed["mode"] not in ("demo", "two_party"):
        parsed["mode"] = "demo"

    # Normalize session_code: None if absent/empty; uppercase with no
    # surrounding whitespace otherwise. Presence forces mode=two_party.
    code = parsed.get("session_code")
    if code:
        code = str(code).strip().upper()
        parsed["session_code"] = code if code else None
    else:
        parsed["session_code"] = None
    if parsed["session_code"]:
        parsed["mode"] = "two_party"

    for field in (
        "company_name", "founder_name", "founder_title",
        "investor_name", "investor_firm", "investment_amount",
    ):
        parsed.setdefault(field, None)
    return parsed


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Parse NL SAFE terms into APOA constraints")
    parser.add_argument("--message", default="", help="The negotiation request text")
    parser.add_argument("--message-file", default="", help="Path to a file containing the request text")
    parser.add_argument("--output-file", default="", help="Write JSON to this file instead of stdout")
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.stderr.write("ANTHROPIC_API_KEY not set.\n")
        return 2

    if args.message:
        message = args.message
    elif args.message_file:
        message = Path(args.message_file).read_text().strip()
    else:
        message = sys.stdin.read().strip()

    if not message:
        sys.stderr.write("No message provided. Use --message, --message-file, or pipe to stdin.\n")
        return 2

    try:
        constraints = extract_constraints(message)
    except (ValueError, RuntimeError) as e:
        sys.stderr.write(f"{e}\n")
        return 1

    output = json.dumps(constraints, indent=2) + "\n"
    if args.output_file:
        Path(args.output_file).write_text(output)
    else:
        sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
