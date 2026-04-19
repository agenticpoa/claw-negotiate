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

SYSTEM_PROMPT = """You extract structured SAFE negotiation constraints from a founder's natural-language message.

Return ONLY a JSON object (no prose, no code fences) with these exact fields:

{
  "valuation_cap_min": <integer dollars, or null>,
  "valuation_cap_max": <integer dollars, or null>,
  "discount_min": <decimal 0-1 e.g. 0.20, or null>,
  "pro_rata": "required" | "preferred" | "indifferent",
  "mfn": "required" | "preferred" | "indifferent",
  "company_name": <string or null>,
  "investor_name": <string or null>,
  "investment_amount": <float dollars or null>
}

Rules:
- "$8M", "8 million", "8MM" -> 8000000.
- "20%", "twenty percent" -> 0.20.
- If only a minimum cap is given, set valuation_cap_max to null.
- If the user doesn't mention pro_rata, default to "preferred".
- If the user doesn't mention mfn, default to "indifferent".
- If a required numeric field is genuinely ambiguous, use null. The caller will ask the user to clarify rather than guess.
"""


def extract_constraints(message: str) -> dict[str, Any]:
    try:
        from anthropic import Anthropic
    except ImportError as e:
        raise RuntimeError("anthropic SDK not installed. Run: pip install anthropic") from e

    client = Anthropic()
    response = client.messages.create(
        model="claude-sonnet-4-6",
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
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"Claude returned non-JSON: {text!r}") from e


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
