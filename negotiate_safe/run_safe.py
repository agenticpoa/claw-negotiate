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
import asyncio
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from parse_constraints import extract_constraints


def run_prepare(
    message: str,
    output_dir: str,
    founder_name: str = "",
    founder_title: str = "CEO",
) -> int:
    """Parse NL constraints and write config.json to output_dir."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    try:
        constraints = extract_constraints(message)
    except (ValueError, RuntimeError) as e:
        sys.stderr.write(f"Parse error: {e}\n")
        return 1

    required_fields = ("valuation_cap_min", "valuation_cap_max", "discount_min", "pro_rata", "mfn")
    missing = [f for f in required_fields if constraints.get(f) is None]
    if missing:
        sys.stderr.write(f"Ambiguous constraints (null values): {missing}. Ask the user to clarify.\n")
        return 1

    config = {
        "constraints": constraints,
        "founder_name": founder_name or os.environ.get("FOUNDER_NAME", "Founder"),
        "founder_title": founder_title,
        "message": message,
    }
    (out / "config.json").write_text(json.dumps(config, indent=2))

    sys.stdout.write(json.dumps(constraints, indent=2) + "\n")
    return 0


def run_mint(output_dir: str, config: dict) -> int:
    """Mint APOA tokens using the constraints from config.json."""
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

    company = constraints.get("company_name", "Company")
    investor = constraints.get("investor_name", "Investor")
    amount = constraints.get("investment_amount", 500_000.0)
    slug = "".join(c.lower() if c.isalnum() else "-" for c in company).strip("-")
    service = f"safe:{slug}:{negotiation_id}"

    cmd = [
        sys.executable, str(repo / "create_tokens.py"),
        "--negotiation-id", negotiation_id,
        "--principal-id", os.environ.get("FOUNDER_DID", "did:apoa:default"),
        "--expires", expires_str,
        "--service", service,
        "--company-name", company,
        "--founder-name", config.get("founder_name", "Founder"),
        "--founder-title", config.get("founder_title", "CEO"),
        "--investor-name", investor,
        "--investment-amount", str(amount),
        "--founder-cap-min", str(constraints["valuation_cap_min"]),
        "--founder-cap-max", str(constraints["valuation_cap_max"]),
        "--founder-discount-min", str(constraints["discount_min"]),
        "--founder-discount-max", str(discount_max),
        "--founder-pro-rata-required", "true" if pro_rata_required else "false",
        "--founder-mfn-required", "true" if mfn_required else "false",
        "--keys-dir", str(neg_dir / "keys"),
        "--tokens-dir", str(neg_dir / "tokens"),
        "--config-dir", str(neg_dir),
        "--create-keys",
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
    }
    (out / "mint.json").write_text(json.dumps(mint_output, indent=2))

    sys.stdout.write(json.dumps({
        "type": "authorized",
        "negotiation_id": negotiation_id,
        "service": service,
        "expires_at": expires_str,
    }) + "\n")
    sys.stdout.flush()

    return 0


def run_negotiation_flow(output_dir: str) -> int:
    """Load upstream negotiate module and run the negotiation."""
    import importlib.util

    repo_str = os.environ.get("NEGOTIATE_REPO_PATH", "")
    if not repo_str:
        sys.stderr.write("NEGOTIATE_REPO_PATH not set.\n")
        return 2
    repo = Path(repo_str).resolve()

    out = Path(output_dir)
    mint = json.loads((out / "mint.json").read_text())

    f_cfg = json.loads(Path(mint["founder_config_path"]).read_text())
    i_cfg = json.loads(Path(mint["investor_config_path"]).read_text())

    neg_id = mint["negotiation_id"]
    neg_dir = Path(mint["founder_config_path"]).parent

    path = repo / "negotiate.py"
    spec = importlib.util.spec_from_file_location("negotiate_upstream", path)
    if spec is None or spec.loader is None:
        sys.stderr.write(f"cannot build import spec for {path}\n")
        return 2

    sys.path.insert(0, str(repo))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        sys.stderr.write(f"cannot import negotiate: {e}\n")
        del sys.modules[spec.name]
        return 2

    config = module.NegotiationConfig(
        negotiate_repo=repo,
        negotiation_id=neg_id,
        founder_token_path=mint["founder_token_path"],
        investor_token_path=mint["investor_token_path"],
        founder_pubkey_path=f_cfg["pubkey"],
        investor_pubkey_path=i_cfg["pubkey"],
        company_name=f_cfg["company_name"],
        founder_name=f_cfg["name"],
        founder_title=f_cfg.get("title", ""),
        investor_name=i_cfg["name"],
        investor_firm=i_cfg.get("firm", ""),
        investment_amount=f_cfg["investment_amount"],
        sshsign_host=f_cfg.get("sshsign_host") or os.environ.get("SSHSIGN_HOST", "sshsign.dev"),
        no_sshsign=False,
        output_dir=str(neg_dir / "output"),
        signing_key_id=f_cfg.get("founder_signing_key_id") or f_cfg.get("signing_key_id", ""),
        founder_signing_key_id=f_cfg.get("founder_signing_key_id") or f_cfg.get("signing_key_id", ""),
        investor_signing_key_id=i_cfg.get("investor_signing_key_id") or i_cfg.get("signing_key_id", ""),
        json_events=True,
        poll=False,
    )

    try:
        asyncio.run(module.run_negotiation(config))
        rc = 0
    except Exception as e:
        sys.stderr.write(f"Negotiation error: {e}\n")
        rc = 1

    output_path = Path(config.output_dir)
    for suffix in ("_executed.pdf", ".pdf"):
        pdf = output_path / f"{neg_id}{suffix}"
        if pdf.exists():
            sys.stdout.write(json.dumps({"type": "pdf", "path": str(pdf)}) + "\n")
            break

    sys.stdout.write(json.dumps({"type": "exit", "code": rc}) + "\n")
    return rc


def run_negotiate(output_dir: str) -> int:
    """Full negotiate flow: mint tokens then run negotiation."""
    out = Path(output_dir)
    config_path = out / "config.json"
    if not config_path.exists():
        sys.stderr.write(f"No config.json in {output_dir}. Run 'prepare' first.\n")
        return 2

    config = json.loads(config_path.read_text())

    rc = run_mint(output_dir, config)
    if rc != 0:
        return rc

    return run_negotiation_flow(output_dir)


def main() -> int:
    parser = argparse.ArgumentParser(description="SAFE negotiation skill")
    sub = parser.add_subparsers(dest="command")

    prep = sub.add_parser("prepare", help="Parse NL message into constraints")
    prep.add_argument("--message", required=True)
    prep.add_argument("--output-dir", required=True)
    prep.add_argument("--founder-name", default=os.environ.get("FOUNDER_NAME", ""))
    prep.add_argument("--founder-title", default="CEO")

    neg = sub.add_parser("negotiate", help="Mint tokens and run negotiation")
    neg.add_argument("--output-dir", required=True)

    args = parser.parse_args()

    if args.command == "prepare":
        return run_prepare(args.message, args.output_dir, args.founder_name, args.founder_title)
    elif args.command == "negotiate":
        return run_negotiate(args.output_dir)
    else:
        parser.print_help()
        return 2


if __name__ == "__main__":
    sys.exit(main())
