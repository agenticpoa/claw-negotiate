#!/usr/bin/env python3
"""Streaming helper: runs upstream's `run_negotiation()` with unbuffered stdout.

Spawned as a subprocess by run_safe.py so each NDJSON event upstream emits via
--json-events flows line-by-line through the stdout pipe to the parent in
real time (vs. the old captured-StringIO pattern that only exposed events at
completion).

The helper reads `mint.json` (written by run_safe.py's mint step) from the
given --output-dir and reconstructs a NegotiationConfig. It does NOT go
through upstream's main() / auto_setup() — it calls `run_negotiation()`
directly, same as the in-process path.

Usage:
  python3 -u _stream_negotiate.py --output-dir /tmp/safe_xxx --negotiate-repo /root/negotiate
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path


def _load_upstream(repo: Path):
    path = repo / "negotiate.py"
    spec = importlib.util.spec_from_file_location("negotiate_upstream", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot build import spec for {path}")
    sys.path.insert(0, str(repo))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _build_config(module, output_dir: Path, sshsign_host: str):
    mint = json.loads((output_dir / "mint.json").read_text())

    # Demo mode mints both sides; join (two-party investor) mints only the
    # joiner's side and the counterparty's config file may not exist yet.
    # Load what we can and fall back to defaults where needed.
    def _maybe_load(path_field: str) -> dict:
        path = mint.get(path_field, "")
        if not path:
            return {}
        try:
            return json.loads(Path(path).read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    f_cfg = _maybe_load("founder_config_path")
    i_cfg = _maybe_load("investor_config_path")

    # Determine neg_dir from whichever config path is available.
    config_anchor = mint.get("founder_config_path") or mint.get("investor_config_path") or ""
    neg_dir = Path(config_anchor).parent if config_anchor else output_dir

    user_role = mint.get("user_role", "founder")
    mode = mint.get("mode", "demo")

    # Counterparty pubkey path: in join mode, run_safe.py writes it to
    # neg_dir/keys/<counterparty_role>_public.pem. Prefer the explicit
    # mint.counterparty_pubkey_path field if present.
    counterparty_role = "investor" if user_role == "founder" else "founder"
    counterparty_pubkey = mint.get("counterparty_pubkey_path", "")
    if not counterparty_pubkey:
        candidate = neg_dir / "keys" / f"{counterparty_role}_public.pem"
        if candidate.exists():
            counterparty_pubkey = str(candidate)

    founder_pubkey = f_cfg.get("pubkey") or (
        counterparty_pubkey if user_role == "investor" else ""
    )
    investor_pubkey = i_cfg.get("pubkey") or (
        counterparty_pubkey if user_role == "founder" else ""
    )

    kwargs = dict(
        negotiate_repo=Path(module.__file__).parent,
        negotiation_id=mint["negotiation_id"],
        founder_token_path=mint.get("founder_token_path", ""),
        investor_token_path=mint.get("investor_token_path", ""),
        founder_pubkey_path=founder_pubkey,
        investor_pubkey_path=investor_pubkey,
        company_name=f_cfg.get("company_name") or i_cfg.get("company_name", ""),
        founder_name=f_cfg.get("name") or f_cfg.get("founder_name", ""),
        founder_title=f_cfg.get("title", ""),
        investor_name=i_cfg.get("name") or i_cfg.get("investor_name", ""),
        investor_firm=i_cfg.get("firm", ""),
        investment_amount=f_cfg.get("investment_amount") or i_cfg.get("investment_amount", 500000.0),
        sshsign_host=f_cfg.get("sshsign_host") or i_cfg.get("sshsign_host") or sshsign_host,
        no_sshsign=False,
        output_dir=str(neg_dir / "output"),
        signing_key_id=(
            f_cfg.get("founder_signing_key_id")
            or i_cfg.get("investor_signing_key_id")
            or f_cfg.get("signing_key_id")
            or i_cfg.get("signing_key_id", "")
        ),
        founder_signing_key_id=f_cfg.get("founder_signing_key_id") or f_cfg.get("signing_key_id", ""),
        investor_signing_key_id=i_cfg.get("investor_signing_key_id") or i_cfg.get("signing_key_id", ""),
        json_events=True,
        poll=False,
    )

    # Two-party dispatches to upstream's run_distributed (which coordinates
    # via sshsign). Demo mode leaves role="" so upstream runs run_local.
    if mode == "two_party":
        kwargs["role"] = user_role

    import dataclasses
    fields = {f.name for f in dataclasses.fields(module.NegotiationConfig)}
    if "signer_role" in fields:
        kwargs["signer_role"] = user_role
    return module.NegotiationConfig(**kwargs)


def main() -> int:
    parser = argparse.ArgumentParser(description="Stream negotiation events to stdout")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument(
        "--negotiate-repo",
        default=os.environ.get("NEGOTIATE_REPO_PATH", ""),
    )
    parser.add_argument(
        "--sshsign-host",
        default=os.environ.get("SSHSIGN_HOST", "sshsign.dev"),
    )
    args = parser.parse_args()

    if not args.negotiate_repo:
        sys.stderr.write("NEGOTIATE_REPO_PATH not set and --negotiate-repo not given\n")
        return 2

    repo = Path(args.negotiate_repo).resolve()
    if not (repo / "negotiate.py").exists():
        sys.stderr.write(f"negotiate.py not found under {repo}\n")
        return 2

    module = _load_upstream(repo)
    config = _build_config(module, Path(args.output_dir), args.sshsign_host)

    try:
        asyncio.run(module.run_negotiation(config))
    except Exception as e:
        sys.stderr.write(f"Negotiation error: {e}\n")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
