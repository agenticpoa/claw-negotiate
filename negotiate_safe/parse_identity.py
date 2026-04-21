#!/usr/bin/env python3
"""Parse a user's short self-introduction into structured identity fields.

Used by the `run_safe.py setup` subcommand to populate the installed user's
identity (FOUNDER_NAME, FOUNDER_TITLE, COMPANY_NAME, INVESTOR_NAME,
INVESTOR_FIRM, ROLE) once, so they don't have to repeat it in every
negotiation request.

Kept as a separate parser from parse_constraints.py because the inputs here
are much shorter and the schema has no term/constraint fields — a focused
prompt gets clean output with less token spend.
"""
from __future__ import annotations

import json
import sys
from typing import Any

SYSTEM_PROMPT = """You extract the user's identity from a short self-introduction.

Return ONLY a JSON object (no prose, no code fences) with these exact fields:

{
  "role": "founder" | "investor",
  "name": <string or null>,
  "title": <string or null>,
  "company": <string or null>,
  "firm": <string or null>
}

Rules:
- `role` is REQUIRED. If the user says "CEO of X" / "CTO of X" / "founder of X" → "founder". If "at X" where X is a fund / VC / capital firm, or "I'm investing" / "investor" → "investor". Default "founder" when ambiguous.
- `company` applies to founders (their startup). Leave null for investors.
- `firm` applies to investors (their VC / fund). Leave null for founders.
- `title` is a free-form string ("CEO", "Cofounder", "Partner", "Managing Director", etc). Null if not stated.
- Every field must appear in the response even if null.

Examples:

Input: "I'm Juan Figuera, CEO of APOA Inc"
Output: {"role": "founder", "name": "Juan Figuera", "title": "CEO", "company": "APOA Inc", "firm": null}

Input: "I'm Alice Chen, cofounder at Stellaris Labs"
Output: {"role": "founder", "name": "Alice Chen", "title": "Cofounder", "company": "Stellaris Labs", "firm": null}

Input: "Mark Stone, partner at Blue Fund"
Output: {"role": "investor", "name": "Mark Stone", "title": "Partner", "company": null, "firm": "Blue Fund"}

Input: "I'm Dr. Rivera, CTO of Helios Robotics"
Output: {"role": "founder", "name": "Dr. Rivera", "title": "CTO", "company": "Helios Robotics", "firm": null}

Input: "Jordan Lee from Bay Capital"
Output: {"role": "investor", "name": "Jordan Lee", "title": null, "company": null, "firm": "Bay Capital"}
"""


def extract_identity(message: str) -> dict[str, Any]:
    try:
        from anthropic import Anthropic
    except ImportError as e:
        raise RuntimeError("anthropic SDK not installed. Run: pip install anthropic") from e

    client = Anthropic()
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
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

    # Defensive normalization — same pattern as parse_constraints.
    if not parsed.get("role"):
        parsed["role"] = "founder"
    parsed["role"] = str(parsed["role"]).lower()
    if parsed["role"] not in ("founder", "investor"):
        parsed["role"] = "founder"

    for field in ("name", "title", "company", "firm"):
        parsed.setdefault(field, None)

    return parsed


def main() -> int:
    message = sys.stdin.read().strip()
    if not message:
        sys.stderr.write("Provide identity text on stdin.\n")
        return 2
    try:
        identity = extract_identity(message)
    except (ValueError, RuntimeError) as e:
        sys.stderr.write(f"Parse error: {e}\n")
        return 1
    sys.stdout.write(json.dumps(identity, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
