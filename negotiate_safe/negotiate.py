#!/usr/bin/env python3
"""Skill wrapper around $NEGOTIATE_REPO_PATH/negotiate.py.

Directly imports and calls upstream's run_local() function, bypassing
main() and auto_setup(). This ensures:
- Our mint_token.py's negotiation ID, tokens, and keys are used (not duplicated)
- Constraints from APOA tokens drive agent behavior (not .env defaults)
- PDFs land in a per-negotiation output directory we control
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a SAFE negotiation via upstream negotiate.py")
    parser.add_argument("--mint-output", required=True,
                        help="JSON output from mint_token.py (path, or - for stdin)")
    parser.add_argument("--negotiate-repo",
                        default=os.environ.get("NEGOTIATE_REPO_PATH", ""))
    parser.add_argument("--no-sshsign", action="store_true",
                        help="Skip sshsign audit trail (dry run)")
    parser.add_argument("--poll-interval", type=float, default=2.0,
                        help="Seconds between sshsign history polls")
    return parser.parse_args()


def load_mint(ref: str) -> dict:
    raw = sys.stdin.read() if ref == "-" else Path(ref).read_text()
    return json.loads(raw)


def load_config(path: str) -> dict:
    return json.loads(Path(path).read_text())


def build_namespace(
    mint: dict,
    repo: Path,
    f_cfg: dict,
    i_cfg: dict,
    no_sshsign: bool = False,
) -> argparse.Namespace:
    """Build an argparse.Namespace matching upstream's parse_args() output.

    Populates every field run_local() reads, using values from mint_token.py
    output and per-negotiation config files. Bypasses auto_setup() entirely.
    """
    neg_id = mint["negotiation_id"]
    neg_dir = Path(mint["founder_config_path"]).parent
    output_dir = str(neg_dir / "output")

    fc = mint.get("founder_constraints", {})
    ic = mint.get("investor_constraints", {})

    return argparse.Namespace(
        schema=str(repo / "schemas" / "safe.json"),
        role="",
        negotiation_id=neg_id,
        session_id=f"session_{neg_id}",
        founder_token=mint["founder_token_path"],
        investor_token=mint["investor_token_path"],
        founder_pubkey=f_cfg["pubkey"],
        investor_pubkey=i_cfg["pubkey"],
        no_sshsign=no_sshsign,
        sshsign_host=f_cfg.get("sshsign_host") or os.environ.get("SSHSIGN_HOST", "sshsign.dev"),
        output_dir=output_dir,
        company_name=f_cfg["company_name"],
        founder_name=f_cfg["name"],
        founder_title=f_cfg.get("title", ""),
        investor_name=i_cfg["name"],
        investor_firm=i_cfg.get("firm", ""),
        investment_amount=f_cfg["investment_amount"],
        date="",
        signing_key_id="",
        founder_signing_key_id=f_cfg.get("founder_signing_key_id") or f_cfg.get("signing_key_id", ""),
        investor_signing_key_id=i_cfg.get("investor_signing_key_id") or i_cfg.get("signing_key_id", ""),
        founder_require_signature=True,
        investor_require_signature=True,
        poll=True,
        poll_timeout=300,
        founder_cap_min=fc.get("cap_min", 8_000_000),
        founder_cap_max=fc.get("cap_max", 12_000_000),
        founder_discount_min=fc.get("discount_min", 0.20),
        founder_discount_max=fc.get("discount_max", 0.25),
        founder_pro_rata_required=fc.get("pro_rata_required", True),
        founder_mfn_required=fc.get("mfn_required", False),
        investor_cap_min=ic.get("cap_min", 6_000_000),
        investor_cap_max=ic.get("cap_max", 10_000_000),
        investor_discount_min=ic.get("discount_min", 0.15),
        investor_discount_max=ic.get("discount_max", 0.25),
        investor_pro_rata_required=ic.get("pro_rata_required", False),
        investor_mfn_required=ic.get("mfn_required", False),
        verbose=False,
        finalize="",
    )


def load_run_local(repo: Path):
    """Import run_local from the upstream negotiate module.

    Uses importlib.util with a distinct module name ('negotiate_upstream')
    to avoid collision with this wrapper module. Adds repo to sys.path
    temporarily so upstream's internal imports (protocol, agents, etc.) resolve.
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
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        sys.stderr.write(f"cannot import negotiate: {e}\n")
        return None

    return getattr(module, "run_local", None)


def load_get_history(repo: Path):
    """Import sshsign_client.get_history from the negotiate repo.

    Uses importlib.util to load the module from its file path directly, so
    this does not pollute sys.path or sys.modules (which leaks across tests
    and can mask the "module missing" code path).
    Returns the callable on success, or None on failure with a stderr log.
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
    """Tail sshsign history via the injected callable; emit new entries as NDJSON."""
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

    ns = build_namespace(mint, repo, f_cfg, i_cfg, no_sshsign=args.no_sshsign)

    run_local_fn = load_run_local(repo)
    if run_local_fn is None:
        sys.stderr.write("Failed to load run_local from upstream.\n")
        return 2

    # Start sshsign history streaming in background
    host = ns.sshsign_host
    stop = threading.Event()
    streamer = None
    if not ns.no_sshsign:
        get_history_fn = load_get_history(repo)
        if get_history_fn is not None:
            streamer = threading.Thread(
                target=stream_offers,
                args=(get_history_fn, host, negotiation_id, stop, args.poll_interval),
                daemon=True,
            )
            streamer.start()

    original_cwd = os.getcwd()
    os.chdir(str(repo))
    try:
        asyncio.run(run_local_fn(ns))
        rc = 0
    except Exception as e:
        sys.stderr.write(f"Negotiation error: {e}\n")
        rc = 1
    finally:
        os.chdir(original_cwd)
        if streamer is not None:
            time.sleep(args.poll_interval + 0.5)
            stop.set()
            streamer.join(timeout=5)

    # Emit PDF path if generated
    output_dir = Path(ns.output_dir)
    for suffix in ("_executed.pdf", ".pdf"):
        pdf = output_dir / f"{negotiation_id}{suffix}"
        if pdf.exists():
            sys.stdout.write(json.dumps({"type": "pdf", "path": str(pdf)}) + "\n")
            break

    sys.stdout.write(json.dumps({"type": "exit", "code": rc}) + "\n")
    return rc


if __name__ == "__main__":
    sys.exit(main())
