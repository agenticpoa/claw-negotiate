#!/usr/bin/env python3
"""Skill wrapper around $NEGOTIATE_REPO_PATH/negotiate.py.

Imports upstream's NegotiationConfig and run_negotiation() directly,
bypassing main() and auto_setup(). Uses --json-events for structured
offer streaming (no sshsign polling thread needed).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from pathlib import Path


def parse_args():
    import argparse
    parser = argparse.ArgumentParser(description="Run a SAFE negotiation via upstream negotiate.py")
    parser.add_argument("--mint-output", required=True,
                        help="JSON output from mint_token.py (path, or - for stdin)")
    parser.add_argument("--negotiate-repo",
                        default=os.environ.get("NEGOTIATE_REPO_PATH", ""))
    parser.add_argument("--no-sshsign", action="store_true",
                        help="Skip sshsign audit trail (dry run)")
    return parser.parse_args()


def load_mint(ref: str) -> dict:
    raw = sys.stdin.read() if ref == "-" else Path(ref).read_text()
    return json.loads(raw)


def load_config(path: str) -> dict:
    return json.loads(Path(path).read_text())


def check_token_expiry(jwt_str: str, buffer_seconds: int = 60) -> str | None:
    """Check if a JWT token is expired or near expiry.

    Returns 'expired', 'expiring_soon', or None (valid).
    """
    import base64
    try:
        _, payload_b64, _ = jwt_str.split(".")
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:
        return None

    exp = payload.get("exp")
    if exp is None:
        return None

    import time
    now = time.time()
    if now >= exp:
        return "expired"
    if now >= exp - buffer_seconds:
        return "expiring_soon"
    return None


def build_config_dict(
    mint: dict,
    repo: Path,
    f_cfg: dict,
    i_cfg: dict,
    no_sshsign: bool = False,
) -> dict:
    """Build a dict matching NegotiationConfig fields from mint output + configs.

    Returned as a plain dict so unit tests don't need to import the upstream
    module. main() converts to NegotiationConfig at runtime.
    """
    neg_id = mint["negotiation_id"]
    neg_dir = Path(mint["founder_config_path"]).parent
    output_dir = str(neg_dir / "output")

    return {
        "negotiate_repo": repo,
        "negotiation_id": neg_id,
        "founder_token_path": mint["founder_token_path"],
        "investor_token_path": mint["investor_token_path"],
        "founder_pubkey_path": f_cfg["pubkey"],
        "investor_pubkey_path": i_cfg["pubkey"],
        "company_name": f_cfg["company_name"],
        "founder_name": f_cfg["name"],
        "founder_title": f_cfg.get("title", ""),
        "investor_name": i_cfg["name"],
        "investor_firm": i_cfg.get("firm", ""),
        "investment_amount": f_cfg["investment_amount"],
        "sshsign_host": f_cfg.get("sshsign_host") or os.environ.get("SSHSIGN_HOST", "sshsign.dev"),
        "no_sshsign": no_sshsign,
        "output_dir": output_dir,
        "signing_key_id": f_cfg.get("founder_signing_key_id") or f_cfg.get("signing_key_id", ""),
        "founder_signing_key_id": f_cfg.get("founder_signing_key_id") or f_cfg.get("signing_key_id", ""),
        "investor_signing_key_id": i_cfg.get("investor_signing_key_id") or i_cfg.get("signing_key_id", ""),
        "json_events": True,
        "poll": False,
    }


def load_upstream_module(repo: Path):
    """Import the upstream negotiate module via importlib.

    Returns the module object (with NegotiationConfig, run_negotiation, etc.)
    or None on failure.
    """
    import importlib.util

    path = repo / "negotiate.py"
    if not path.exists():
        sys.stderr.write(f"negotiate.py not found at {path}\n")
        return None

    spec = importlib.util.spec_from_file_location("negotiate_upstream", path)
    if spec is None or spec.loader is None:
        sys.stderr.write(f"cannot build import spec for {path}\n")
        return None

    sys.path.insert(0, str(repo))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        sys.stderr.write(f"cannot import negotiate: {e}\n")
        del sys.modules[spec.name]
        return None

    return module


def load_get_history(repo: Path):
    """Import sshsign_client.get_history from the negotiate repo.

    Kept as a fallback for environments where --json-events isn't available.
    """
    import importlib.util

    path = repo / "sshsign_client.py"
    if not path.exists():
        sys.stderr.write(f"[stream] cannot import sshsign_client: no such file {path}\n")
        return None

    spec = importlib.util.spec_from_file_location("sshsign_client_wrapper", path)
    if spec is None or spec.loader is None:
        sys.stderr.write(f"[stream] cannot build import spec for {path}\n")
        return None

    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        sys.stderr.write(f"[stream] cannot import sshsign_client: {e}\n")
        return None
    return getattr(module, "get_history", None)


def stream_offers(
    get_history_fn,
    host: str,
    negotiation_id: str,
    stop: threading.Event,
    interval: float,
) -> None:
    """Tail sshsign history via the injected callable; emit new entries as NDJSON.

    Fallback streaming mechanism. Prefer --json-events when available.
    """
    seen = 0
    while not stop.is_set():
        try:
            history = get_history_fn(host=host, negotiation_id=negotiation_id)
            if isinstance(history, list) and len(history) > seen:
                for entry in history[seen:]:
                    entry.setdefault("type", "offer")
                    sys.stdout.write(json.dumps(entry) + "\n")
                    sys.stdout.flush()
                seen = len(history)
        except Exception as e:
            sys.stderr.write(f"[stream] {e}\n")
        stop.wait(interval)


def main() -> int:
    args = parse_args()

    if not args.negotiate_repo:
        sys.stderr.write("NEGOTIATE_REPO_PATH not set.\n")
        return 2

    repo = Path(args.negotiate_repo).resolve()
    if not (repo / "negotiate.py").exists():
        sys.stderr.write(f"negotiate.py not found under {repo}\n")
        return 2

    mint = load_mint(args.mint_output)
    negotiation_id = mint["negotiation_id"]

    f_cfg = load_config(mint["founder_config_path"])
    i_cfg = load_config(mint["investor_config_path"])

    config_dict = build_config_dict(mint, repo, f_cfg, i_cfg, no_sshsign=args.no_sshsign)

    upstream = load_upstream_module(repo)
    if upstream is None:
        sys.stderr.write("Failed to load upstream negotiate module.\n")
        return 2

    config = upstream.NegotiationConfig(**config_dict)

    try:
        asyncio.run(upstream.run_negotiation(config))
        rc = 0
    except Exception as e:
        sys.stderr.write(f"Negotiation error: {e}\n")
        rc = 1

    # Emit PDF path if generated
    output_dir = Path(config_dict["output_dir"])
    for suffix in ("_executed.pdf", ".pdf"):
        pdf = output_dir / f"{negotiation_id}{suffix}"
        if pdf.exists():
            sys.stdout.write(json.dumps({"type": "pdf", "path": str(pdf)}) + "\n")
            break

    sys.stdout.write(json.dumps({"type": "exit", "code": rc}) + "\n")
    return rc


if __name__ == "__main__":
    sys.exit(main())
