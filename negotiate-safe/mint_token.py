#!/usr/bin/env python3
"""Mint a per-negotiation APOA token pair by wrapping create_tokens.py.

Takes the parsed constraints from Step 1 plus party info, calls the negotiate
repo's create_tokens.py with a bounded TTL and a per-negotiation config dir,
then emits JSON on stdout with the paths downstream steps consume.

Why a wrapper and not a direct APOA SDK call: create_tokens.py is already the
canonical way to produce compatible tokens + sshsign signing keys + config
JSON for negotiate.py. Reimplementing it would drift over time.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

REQUIRED_CONSTRAINTS = (
    "valuation_cap_min",
    "valuation_cap_max",
    "discount_min",
    "pro_rata",
    "mfn",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Mint APOA tokens for a SAFE negotiation")
    parser.add_argument("--constraints-json", required=True,
                        help="JSON string from parse_constraints.py")
    parser.add_argument("--company-name", required=True)
    parser.add_argument("--founder-name", required=True)
    parser.add_argument("--founder-title", default="CEO")
    parser.add_argument("--investor-name", required=True)
    parser.add_argument("--investment-amount", type=float, required=True)
    parser.add_argument("--negotiation-id", default="",
                        help="Reuse an existing ID; otherwise a fresh one is generated")
    parser.add_argument("--ttl-seconds", type=int,
                        default=int(os.environ.get("NEGOTIATION_TTL", "3600")))
    parser.add_argument("--principal-id",
                        default=os.environ.get("FOUNDER_DID", "did:apoa:principal"))
    parser.add_argument("--negotiate-repo",
                        default=os.environ.get("NEGOTIATE_REPO_PATH", ""))
    parser.add_argument("--skip-sshsign-keys", action="store_true",
                        help="Skip registering signing keys on sshsign (only for testing)")
    return parser.parse_args()


def validate_constraints(raw: str) -> dict:
    try:
        constraints = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SystemExit(f"Invalid --constraints-json: {e}")

    missing = [k for k in REQUIRED_CONSTRAINTS
               if k not in constraints or constraints[k] is None]
    if missing:
        raise SystemExit(f"Constraints missing required fields: {missing}. "
                         f"Re-run parse_constraints.py or clarify with the user.")
    return constraints


def slugify(s: str) -> str:
    return "".join(c.lower() if c.isalnum() else "-" for c in s).strip("-")


def main() -> int:
    args = parse_args()

    if not args.negotiate_repo:
        sys.stderr.write("NEGOTIATE_REPO_PATH not set and --negotiate-repo not given.\n")
        return 2

    repo = Path(args.negotiate_repo).resolve()
    if not (repo / "create_tokens.py").exists():
        sys.stderr.write(f"create_tokens.py not found under {repo}\n")
        return 2

    constraints = validate_constraints(args.constraints_json)

    negotiation_id = args.negotiation_id or f"neg_{uuid.uuid4().hex[:12]}"
    out_dir = repo / "negotiations" / negotiation_id
    out_dir.mkdir(parents=True, exist_ok=True)

    expires_at = datetime.now(timezone.utc) + timedelta(seconds=args.ttl_seconds)
    expires_str = expires_at.strftime("%Y-%m-%dT%H:%M:%SZ")

    pro_rata_required = constraints["pro_rata"] == "required"
    mfn_required = constraints["mfn"] == "required"
    # Upstream requires a max discount. The user only specified a floor;
    # cap the ceiling at 25% or the floor, whichever is higher.
    discount_max = max(0.25, float(constraints["discount_min"]))

    cmd = [
        sys.executable, str(repo / "create_tokens.py"),
        "--negotiation-id", negotiation_id,
        "--principal-id", args.principal_id,
        "--expires", expires_str,
        "--company-name", args.company_name,
        "--founder-name", args.founder_name,
        "--founder-title", args.founder_title,
        "--investor-name", args.investor_name,
        "--investment-amount", str(args.investment_amount),
        "--founder-cap-min", str(constraints["valuation_cap_min"]),
        "--founder-cap-max", str(constraints["valuation_cap_max"]),
        "--founder-discount-min", str(constraints["discount_min"]),
        "--founder-discount-max", str(discount_max),
        "--founder-pro-rata-required", "true" if pro_rata_required else "false",
        "--founder-mfn-required", "true" if mfn_required else "false",
        "--keys-dir", str(out_dir / "keys"),
        "--tokens-dir", str(out_dir / "tokens"),
        "--config-dir", str(out_dir),
    ]
    investor_env_flags = {
        "INVESTOR_CAP_MIN": "--investor-cap-min",
        "INVESTOR_CAP_MAX": "--investor-cap-max",
        "INVESTOR_DISCOUNT_MIN": "--investor-discount-min",
        "INVESTOR_DISCOUNT_MAX": "--investor-discount-max",
        "INVESTOR_PRO_RATA_REQUIRED": "--investor-pro-rata-required",
        "INVESTOR_MFN_REQUIRED": "--investor-mfn-required",
    }
    for env_key, flag in investor_env_flags.items():
        val = os.environ.get(env_key)
        if val:
            cmd.extend([flag, val])

    if not args.skip_sshsign_keys:
        cmd.append("--create-keys")

    # cwd=repo because create_tokens.py does `from sshsign_client import ...`
    result = subprocess.run(cmd, cwd=repo, capture_output=True, text=True)
    if result.returncode != 0:
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        return result.returncode

    output = {
        "negotiation_id": negotiation_id,
        "founder_config_path": str(out_dir / "founder.json"),
        "investor_config_path": str(out_dir / "investor.json"),
        "founder_token_path": str(out_dir / "tokens" / "founder.jwt"),
        "investor_token_path": str(out_dir / "tokens" / "investor.jwt"),
        "expires_at": expires_str,
        # Intended service string per SKILL.md. Upstream currently hardcodes
        # "safe-agreement" in the token. See open item in SKILL.md.
        "intended_service": f"safe:{slugify(args.company_name)}:{negotiation_id}",
        "actual_service_in_token": "safe-agreement",
    }
    json.dump(output, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
