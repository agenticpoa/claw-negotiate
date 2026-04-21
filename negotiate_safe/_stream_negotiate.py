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
    f_cfg = json.loads(Path(mint["founder_config_path"]).read_text())
    i_cfg = json.loads(Path(mint["investor_config_path"]).read_text())

    neg_dir = Path(mint["founder_config_path"]).parent

    # Tell upstream which party the human user is signing as so the executed
    # PDF labels the handwritten signature on the correct block. Older
    # versions of upstream that predate the `signer_role` field simply
    # ignore it.
    user_role = mint.get("user_role", "founder")
    kwargs = dict(
        negotiate_repo=Path(module.__file__).parent,
        negotiation_id=mint["negotiation_id"],
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
        sshsign_host=f_cfg.get("sshsign_host") or sshsign_host,
        no_sshsign=False,
        output_dir=str(neg_dir / "output"),
        signing_key_id=f_cfg.get("founder_signing_key_id") or f_cfg.get("signing_key_id", ""),
        founder_signing_key_id=f_cfg.get("founder_signing_key_id") or f_cfg.get("signing_key_id", ""),
        investor_signing_key_id=i_cfg.get("investor_signing_key_id") or i_cfg.get("signing_key_id", ""),
        json_events=True,
        poll=False,
    )
    import dataclasses
    if "signer_role" in {f.name for f in dataclasses.fields(module.NegotiationConfig)}:
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
