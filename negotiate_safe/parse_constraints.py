#!/usr/bin/env python3
"""Parse a founder's natural-language SAFE request into APOA-flat constraints.

Reads the message on stdin, writes JSON to stdout matching the schema in
SKILL.md Step 1. Null values mean "ambiguous, ask the user" — do not guess.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

SYSTEM_PROMPT = """You extract structured SAFE negotiation constraints from a user's natural-language message. The user may be negotiating as the FOUNDER (raising money for their company) or as the INVESTOR (evaluating an investment in someone else's company).

Return ONLY a JSON object (no prose, no code fences). It MUST contain ALL 18 of these fields — never omit any, especially `role` and `mode`:

{
  "role": "founder" | "investor",
  "mode": "demo" | "two_party",
  "session_code": <string or null>,
  "founder_bot_handle": <string or null>,
  "valuation_cap_min": <integer dollars>,
  "valuation_cap_max": <integer dollars>,
  "discount_min": <decimal 0-1>,
  "discount_max": <decimal 0-1>,
  "pro_rata": "required" | "preferred" | "indifferent",
  "mfn": "required" | "preferred" | "indifferent",
  "company_name": <string or null>,
  "founder_name": <string or null>,
  "founder_title": <string or null>,
  "investor_name": <string or null>,
  "investor_firm": <string or null>,
  "investment_amount": <float dollars or null>,
  "investment_amount_min": <float dollars or null>,
  "investment_amount_max": <float dollars or null>
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

Investment/check size rules:
- Extract a single check size into `investment_amount`.
- Extract a check size range into `investment_amount_min` and `investment_amount_max`.
- For a range, set `investment_amount` to the lower bound for compatibility with
  legacy SAFE generation that still requires one initial amount.
- Treat "check", "check size", "investment", "invest", and "$X check" as
  investment amount signals.

Discount rules:
- Extract a discount range such as "Discount: 5%-10%" into `discount_min`
  and `discount_max`.
- A single plain discount such as "Discount: 0%" or "10% discount" means the
  exact authorized discount, so set `discount_min` and `discount_max` to the
  same value.
- Directional wording such as "at least 10% discount", "10% discount floor",
  or "minimum 10% discount" means a lower bound. Set `discount_min` to that
  value and `discount_max` to 1.0 unless the user also gives an upper bound.
- Directional wording such as "up to 10% discount" means an upper bound. Set
  `discount_min` to 0 and `discount_max` to that value.

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
Output: {"role": "founder", "mode": "demo", "session_code": null, "founder_bot_handle": null, "valuation_cap_min": 8000000, "valuation_cap_max": 12000000, "discount_min": 0.20, "discount_max": 0.20, "pro_rata": "required", "mfn": "preferred", "company_name": "Acme Corp", "founder_name": "Jane Doe", "founder_title": "CEO", "investor_name": "Mark Stone", "investor_firm": "Bay Capital", "investment_amount": 500000.0, "investment_amount_min": null, "investment_amount_max": null}

Input: "SAFE for TechCo. Looking for $150M cap, no less than $120M. Discount 15%. Pro-rata is a must. Investment: $1.5M."
Output: {"role": "founder", "mode": "demo", "session_code": null, "founder_bot_handle": null, "valuation_cap_min": 120000000, "valuation_cap_max": 150000000, "discount_min": 0.15, "discount_max": 0.15, "pro_rata": "required", "mfn": "indifferent", "company_name": "TechCo", "founder_name": null, "founder_title": null, "investor_name": null, "investor_firm": null, "investment_amount": 1500000.0, "investment_amount_min": null, "investment_amount_max": null}

Input: "Cap no lower than $5M, up to $10M. Twenty percent discount minimum. No strong feelings on pro-rata or MFN. $250,000."
Output: {"role": "founder", "mode": "demo", "session_code": null, "founder_bot_handle": null, "valuation_cap_min": 5000000, "valuation_cap_max": 10000000, "discount_min": 0.20, "discount_max": 0.20, "pro_rata": "preferred", "mfn": "indifferent", "company_name": null, "founder_name": null, "founder_title": null, "investor_name": null, "investor_firm": null, "investment_amount": 250000.0, "investment_amount_min": null, "investment_amount_max": null}

Founder-side two-party examples (AI does NOT play the investor; a real investor joins separately):

Input: "Live negotiation: I'm Juan Figuera, CEO of APOA Inc. My investor is Alex Smith at Central Park Labs and will join separately. Cap $20M-$50M, 10% discount, pro-rata required, MFN preferred. $500K."
Output: {"role": "founder", "mode": "two_party", "session_code": null, "founder_bot_handle": null, "valuation_cap_min": 20000000, "valuation_cap_max": 50000000, "discount_min": 0.10, "discount_max": 0.10, "pro_rata": "required", "mfn": "preferred", "company_name": "APOA Inc", "founder_name": "Juan Figuera", "founder_title": "CEO", "investor_name": "Alex Smith", "investor_firm": "Central Park Labs", "investment_amount": 500000.0, "investment_amount_min": null, "investment_amount_max": null}

Input: "Real negotiation with my investor. Cap $8M-$12M, 20% discount, pro-rata, MFN preferred. $500K. I'll share the code with them."
Output: {"role": "founder", "mode": "two_party", "session_code": null, "founder_bot_handle": null, "valuation_cap_min": 8000000, "valuation_cap_max": 12000000, "discount_min": 0.20, "discount_max": 0.20, "pro_rata": "required", "mfn": "preferred", "company_name": null, "founder_name": null, "founder_title": null, "investor_name": null, "investor_firm": null, "investment_amount": 500000.0, "investment_amount_min": null, "investment_amount_max": null}

Input: "Live negotiation with Nora at Babes Fund. She'll join separately and I'll share the invitation code. Cap $30M to $40M, 10% discount, pro-rata required."
Output: {"role": "founder", "mode": "two_party", "session_code": null, "founder_bot_handle": null, "valuation_cap_min": 30000000, "valuation_cap_max": 40000000, "discount_min": 0.10, "discount_max": 0.10, "pro_rata": "required", "mfn": "indifferent", "company_name": null, "founder_name": null, "founder_title": null, "investor_name": "Nora", "investor_firm": "Babes Fund", "investment_amount": null, "investment_amount_min": null, "investment_amount_max": null}

Input: "Live negotiation for Series Seed SAFE with Nora Vassileva (SD Capital). Cap: $15M-$30M post. Check: $250k-$750k. Pro rata: required. Discount: 0%"
Output: {"role": "founder", "mode": "two_party", "session_code": null, "founder_bot_handle": null, "valuation_cap_min": 15000000, "valuation_cap_max": 30000000, "discount_min": 0.0, "discount_max": 0.0, "pro_rata": "required", "mfn": "indifferent", "company_name": null, "founder_name": null, "founder_title": null, "investor_name": "Nora Vassileva", "investor_firm": "SD Capital", "investment_amount": 250000.0, "investment_amount_min": 250000.0, "investment_amount_max": 750000.0}

Investor-side examples (demo mode — AI plays the founder):

Input: "As Alex Chen from Blue Fund, evaluate an investment in QuantumLabs (CEO Dr. Rivera). Cap between $20M and $40M, need at least a 15% discount. Pro-rata required, MFN nice to have. $500K check."
Output: {"role": "investor", "mode": "demo", "session_code": null, "founder_bot_handle": null, "valuation_cap_min": 20000000, "valuation_cap_max": 40000000, "discount_min": 0.15, "discount_max": 1.0, "pro_rata": "required", "mfn": "preferred", "company_name": "QuantumLabs", "founder_name": "Dr. Rivera", "founder_title": "CEO", "investor_name": "Alex Chen", "investor_firm": "Blue Fund", "investment_amount": 500000.0, "investment_amount_min": null, "investment_amount_max": null}

Input: "Evaluate an investment in Helios Robotics. I can take a cap between $25M and $50M with a 12% discount floor. Pro-rata required, MFN preferred. $500K check."
Output: {"role": "investor", "mode": "demo", "session_code": null, "founder_bot_handle": null, "valuation_cap_min": 25000000, "valuation_cap_max": 50000000, "discount_min": 0.12, "discount_max": 1.0, "pro_rata": "required", "mfn": "preferred", "company_name": "Helios Robotics", "founder_name": null, "founder_title": null, "investor_name": null, "investor_firm": null, "investment_amount": 500000.0, "investment_amount_min": null, "investment_amount_max": null}

Investor joining an existing session (two_party, code supplied):

Input: "Join negotiation INV-7K3X9 as investor. Cap up to $40M, 12% discount floor, pro-rata required. $500K check."
Output: {"role": "investor", "mode": "two_party", "session_code": "INV-7K3X9", "founder_bot_handle": null, "valuation_cap_min": 0, "valuation_cap_max": 40000000, "discount_min": 0.12, "discount_max": 1.0, "pro_rata": "required", "mfn": "indifferent", "company_name": null, "founder_name": null, "founder_title": null, "investor_name": null, "investor_firm": null, "investment_amount": 500000.0, "investment_amount_min": null, "investment_amount_max": null}

Input: "I'm Mark Stone from Blue Fund. Joining INV-3BQ7K as investor. Cap $15M-$30M, 15% discount, pro-rata required, MFN preferred."
Output: {"role": "investor", "mode": "two_party", "session_code": "INV-3BQ7K", "founder_bot_handle": null, "valuation_cap_min": 15000000, "valuation_cap_max": 30000000, "discount_min": 0.15, "discount_max": 0.15, "pro_rata": "required", "mfn": "preferred", "company_name": null, "founder_name": null, "founder_title": null, "investor_name": "Mark Stone", "investor_firm": "Blue Fund", "investment_amount": null, "investment_amount_min": null, "investment_amount_max": null}

Investor join WITH the founder bot handle (the standard inverted-invitation shape):

Input: "Joining INV-7K3X9 via @AgenticPOA_bot, cap up to $40M, 10% discount, pro-rata required."
Output: {"role": "investor", "mode": "two_party", "session_code": "INV-7K3X9", "founder_bot_handle": "@AgenticPOA_bot", "valuation_cap_min": 0, "valuation_cap_max": 40000000, "discount_min": 0.10, "discount_max": 0.10, "pro_rata": "required", "mfn": "indifferent", "company_name": null, "founder_name": null, "founder_title": null, "investor_name": null, "investor_firm": null, "investment_amount": null, "investment_amount_min": null, "investment_amount_max": null}

Input: "Joining INV-7K3X9 via @AgenticPOA_bot, I am Nora Vassileva at SD Fund, cap up to $35M, 10% discount, pro-rata required."
Output: {"role": "investor", "mode": "two_party", "session_code": "INV-7K3X9", "founder_bot_handle": "@AgenticPOA_bot", "valuation_cap_min": 0, "valuation_cap_max": 35000000, "discount_min": 0.10, "discount_max": 0.10, "pro_rata": "required", "mfn": "indifferent", "company_name": null, "founder_name": null, "founder_title": null, "investor_name": "Nora Vassileva", "investor_firm": "SD Fund", "investment_amount": null, "investment_amount_min": null, "investment_amount_max": null}

Input: "Join INV-DEMO99 as investor. Founder bot is @alice_negotiator_bot. Cap $20M-$45M, 12% discount, pro-rata required. $750K."
Output: {"role": "investor", "mode": "two_party", "session_code": "INV-DEMO99", "founder_bot_handle": "@alice_negotiator_bot", "valuation_cap_min": 20000000, "valuation_cap_max": 45000000, "discount_min": 0.12, "discount_max": 0.12, "pro_rata": "required", "mfn": "indifferent", "company_name": null, "founder_name": null, "founder_title": null, "investor_name": null, "investor_firm": null, "investment_amount": 750000.0, "investment_amount_min": null, "investment_amount_max": null}
"""


def _money_to_dollars(text: str) -> int:
    raw = text.strip().replace("$", "").replace(",", "")
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([kmb])?", raw, re.IGNORECASE)
    if not m:
        raise ValueError(f"Could not parse money value: {text!r}")
    n = float(m.group(1))
    suffix = (m.group(2) or "").lower()
    if suffix == "k":
        n *= 1_000
    elif suffix == "m":
        n *= 1_000_000
    elif suffix == "b":
        n *= 1_000_000_000
    return int(n)


def _normalize_constraints(parsed: dict[str, Any]) -> dict[str, Any]:
    # Defensive: LLM and deterministic parsers share this normalization.
    if not parsed.get("role"):
        parsed["role"] = "founder"
    parsed["role"] = str(parsed["role"]).lower()
    if parsed["role"] not in ("founder", "investor"):
        parsed["role"] = "founder"

    if not parsed.get("mode"):
        parsed["mode"] = "demo"
    parsed["mode"] = str(parsed["mode"]).lower()
    if parsed["mode"] not in ("demo", "two_party"):
        parsed["mode"] = "demo"

    code = parsed.get("session_code")
    if code:
        code = str(code).strip().upper()
        parsed["session_code"] = code if code else None
    else:
        parsed["session_code"] = None
    if parsed["session_code"]:
        parsed["mode"] = "two_party"
    if parsed.get("discount_max") is None and parsed.get("discount_min") is not None:
        parsed["discount_max"] = parsed["discount_min"]

    for field in (
        "company_name", "founder_name", "founder_title",
        "investor_name", "investor_firm", "investment_amount",
        "investment_amount_min", "investment_amount_max",
    ):
        parsed.setdefault(field, None)
    if parsed.get("investment_amount") is None and parsed.get("investment_amount_min") is not None:
        parsed["investment_amount"] = parsed["investment_amount_min"]
    return parsed


def _extract_constraints_deterministic(message: str) -> dict[str, Any]:
    """Parse the constrained demo copy without any external LLM dependency."""
    text = " ".join(message.strip().split())
    lower = text.lower()
    code_m = re.search(r"\bINV-[A-Z0-9]{5,}\b", text, re.IGNORECASE)
    role = "investor" if code_m or re.search(r"\b(joining|join)\b", lower) else "founder"
    mode = "two_party" if code_m or "live negotiation" in lower or "real negotiation" in lower else "demo"

    founder_bot = None
    bot_m = re.search(r"\bvia\s+(@[A-Za-z0-9_]+)", text)
    if bot_m:
        founder_bot = bot_m.group(1)

    valuation_min = None
    valuation_max = None
    range_m = re.search(
        r"\bcap\b[^$]*(\$?\d+(?:\.\d+)?\s*[kmb]?)\s*(?:-|–|to|through)\s*(\$?\d+(?:\.\d+)?\s*[kmb]?)",
        text,
        re.IGNORECASE,
    )
    up_to_m = re.search(r"\bcap\s+up\s+to\s+(\$?\d+(?:\.\d+)?\s*[kmb]?)", text, re.IGNORECASE)
    single_m = re.search(r"\bcap\b[^$]*(\$?\d+(?:\.\d+)?\s*[kmb]?)", text, re.IGNORECASE)
    if range_m:
        valuation_min = _money_to_dollars(range_m.group(1))
        valuation_max = _money_to_dollars(range_m.group(2))
    elif up_to_m:
        valuation_min = 0
        valuation_max = _money_to_dollars(up_to_m.group(1))
    elif single_m:
        cap = _money_to_dollars(single_m.group(1))
        valuation_min = cap
        valuation_max = cap

    discount_min = None
    discount_max = None
    disc_range_m = re.search(
        r"(?:discount[^0-9]*)?(\d+(?:\.\d+)?)\s*%\s*(?:-|–|to|through)\s*(\d+(?:\.\d+)?)\s*%\s*(?:discount)?",
        text,
        re.IGNORECASE,
    )
    disc_floor_m = re.search(
        r"(?:at\s+least|minimum|floor|no\s+less\s+than)[^0-9]*(\d+(?:\.\d+)?)\s*%\s*(?:discount)?|(\d+(?:\.\d+)?)\s*%\s+discount\s+(?:minimum|floor)",
        text,
        re.IGNORECASE,
    )
    disc_ceiling_m = re.search(
        r"(?:up\s+to|maximum|no\s+more\s+than)[^0-9]*(\d+(?:\.\d+)?)\s*%\s*(?:discount)?",
        text,
        re.IGNORECASE,
    )
    disc_single_m = re.search(r"(\d+(?:\.\d+)?)\s*%\s+discount", text, re.IGNORECASE)
    if not disc_single_m:
        disc_single_m = re.search(r"discount[^0-9]*(\d+(?:\.\d+)?)\s*%", text, re.IGNORECASE)
    if disc_range_m:
        discount_min = float(disc_range_m.group(1)) / 100
        discount_max = float(disc_range_m.group(2)) / 100
    elif disc_floor_m:
        discount_min = float(disc_floor_m.group(1) or disc_floor_m.group(2)) / 100
        discount_max = 1.0
    elif disc_ceiling_m:
        discount_min = 0.0
        discount_max = float(disc_ceiling_m.group(1)) / 100
    elif disc_single_m:
        discount_min = float(disc_single_m.group(1)) / 100
        discount_max = discount_min

    pro_rata = "required" if re.search(r"pro[- ]?rata[^.,;]*(required|must|included)|pro[- ]?rata required", lower) else "preferred"
    mfn = "required" if re.search(r"\bmfn\b[^.,;]*(required|must)", lower) else "indifferent"

    amount = None
    amount_min = None
    amount_max = None
    check_range_m = re.search(
        r"\b(?:check(?:\s+size)?|investment|invest)\b[^$]*(\$?\d+(?:\.\d+)?\s*[kmb]?)\s*(?:-|–|to|through)\s*(\$?\d+(?:\.\d+)?\s*[kmb]?)",
        text,
        re.IGNORECASE,
    )
    check_single_m = re.search(
        r"\b(?:check(?:\s+size)?|investment|invest)\b[^$]*(\$?\d+(?:\.\d+)?\s*[kmb]?)|(\$?\d+(?:\.\d+)?\s*[kmb]?)\s+check\b",
        text,
        re.IGNORECASE,
    )
    if check_range_m:
        amount_min = float(_money_to_dollars(check_range_m.group(1)))
        amount_max = float(_money_to_dollars(check_range_m.group(2)))
        amount = amount_min
    elif check_single_m:
        amount = float(_money_to_dollars(check_single_m.group(1) or check_single_m.group(2)))

    investor_name = investor_firm = None
    if role == "founder":
        im = re.search(r"\bwith\s+(.+?)\s+at\s+(.+?)(?=\.|,|\s+cap\b|$)", text, re.IGNORECASE)
        paren_im = re.search(r"\bwith\s+(.+?)\s*\((.+?)\)", text, re.IGNORECASE)
        if im:
            investor_name = im.group(1).strip()
            investor_firm = im.group(2).strip()
        elif paren_im:
            investor_name = paren_im.group(1).strip()
            investor_firm = paren_im.group(2).strip()
    else:
        im = re.search(r"\bI\s*(?:am|'m)\s+(.+?)\s+(?:at|from)\s+(.+?)(?=,\s*cap\b|\.|$)", text, re.IGNORECASE)
        if im:
            investor_name = im.group(1).strip(" ,")
            investor_firm = im.group(2).strip(" ,")

    return _normalize_constraints({
        "role": role,
        "mode": mode,
        "session_code": code_m.group(0).upper() if code_m else None,
        "founder_bot_handle": founder_bot,
        "valuation_cap_min": valuation_min,
        "valuation_cap_max": valuation_max,
        "discount_min": discount_min,
        "discount_max": discount_max,
        "pro_rata": pro_rata,
        "mfn": mfn,
        "company_name": None,
        "founder_name": None,
        "founder_title": None,
        "investor_name": investor_name,
        "investor_firm": investor_firm,
        "investment_amount": amount,
        "investment_amount_min": amount_min,
        "investment_amount_max": amount_max,
    })


def extract_constraints(message: str) -> dict[str, Any]:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return _extract_constraints_deterministic(message)

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

    return _normalize_constraints(parsed)


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Parse NL SAFE terms into APOA constraints")
    parser.add_argument("--message", default="", help="The negotiation request text")
    parser.add_argument("--message-file", default="", help="Path to a file containing the request text")
    parser.add_argument("--output-file", default="", help="Write JSON to this file instead of stdout")
    args = parser.parse_args()

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
