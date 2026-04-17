#!/usr/bin/env python3
"""Skill wrapper around $NEGOTIATE_REPO_PATH/negotiate.py.

Takes mint_token.py's output (JSON with per-negotiation config paths), expands
the founder config into CLI flags, execs the upstream negotiator, and in
parallel polls sshsign history to emit NDJSON offer events on stdout as they
land. Final exit code mirrors the upstream process.

Streaming strategy: upstream doesn't emit structured events on stdout yet.
Until a --json-events flag lands in agenticpoa/negotiate, we tail the sshsign
history instead. Offers are logged to sshsign before they are displayed, so
history-polling is the authoritative stream (and cheap: ~2s poll interval).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
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
    parser.add_argument("--role", default="", choices=["", "founder", "investor"],
                        help="Pass through to upstream when running distributed")
    parser.add_argument("--poll-interval", type=float, default=2.0,
                        help="Seconds between sshsign history polls")
    return parser.parse_args()


def load_mint(ref: str) -> dict:
    raw = sys.stdin.read() if ref == "-" else Path(ref).read_text()
    return json.loads(raw)


def load_config(path: str) -> dict:
    return json.loads(Path(path).read_text())


def build_upstream_cmd(
    repo: Path, f_cfg: dict, i_cfg: dict, role: str,
    founder_constraints: dict | None = None,
    investor_constraints: dict | None = None,
) -> list[str]:
    """Expand the per-negotiation config JSONs into upstream negotiate.py flags.

    Upstream does not accept a --config flag, despite create_tokens.py printing
    a hint that suggests it does. Local mode needs both parties' token and
    pubkey paths; role mode still benefits from passing both so the upstream
    validator doesn't trip on missing counterparty info.
    """
    cmd = [
        sys.executable, str(repo / "negotiate.py"),
        "--company-name", f_cfg["company_name"],
        "--founder-name", f_cfg["name"],
        "--founder-title", f_cfg.get("title", ""),
        "--investor-name", i_cfg["name"],
        "--investment-amount", str(f_cfg["investment_amount"]),
        "--founder-token", f_cfg["token"],
        "--founder-pubkey", f_cfg["pubkey"],
        "--founder-signing-key-id", f_cfg.get("founder_signing_key_id") or f_cfg.get("signing_key_id", ""),
        "--investor-token", i_cfg["token"],
        "--investor-pubkey", i_cfg["pubkey"],
        "--investor-signing-key-id", i_cfg.get("investor_signing_key_id") or i_cfg.get("signing_key_id", ""),
        "--poll",
    ]
    if founder_constraints:
        cmd.extend([
            "--founder-cap-min", str(founder_constraints["cap_min"]),
            "--founder-cap-max", str(founder_constraints["cap_max"]),
            "--founder-discount-min", str(founder_constraints["discount_min"]),
            "--founder-discount-max", str(founder_constraints["discount_max"]),
            "--founder-pro-rata-required", "true" if founder_constraints["pro_rata_required"] else "false",
            "--founder-mfn-required", "true" if founder_constraints["mfn_required"] else "false",
        ])
    if investor_constraints:
        cmd.extend([
            "--investor-cap-min", str(investor_constraints["cap_min"]),
            "--investor-cap-max", str(investor_constraints["cap_max"]),
            "--investor-discount-min", str(investor_constraints["discount_min"]),
            "--investor-discount-max", str(investor_constraints["discount_max"]),
            "--investor-pro-rata-required", "true" if investor_constraints["pro_rata_required"] else "false",
            "--investor-mfn-required", "true" if investor_constraints["mfn_required"] else "false",
        ])
    if role:
        cmd.extend(["--role", role])
    return cmd


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
            # Transient sshsign errors are tolerable. Log to stderr and retry.
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
    host = f_cfg.get("sshsign_host") or os.environ.get("SSHSIGN_HOST", "sshsign.dev")

    cmd = build_upstream_cmd(
        repo, f_cfg, i_cfg, args.role,
        founder_constraints=mint.get("founder_constraints"),
        investor_constraints=mint.get("investor_constraints"),
    )

    stop = threading.Event()
    get_history_fn = load_get_history(repo)
    streamer = None
    if get_history_fn is not None:
        streamer = threading.Thread(
            target=stream_offers,
            args=(get_history_fn, host, negotiation_id, stop, args.poll_interval),
            daemon=True,
        )
        streamer.start()

    try:
        result = subprocess.run(cmd, cwd=repo)
    finally:
        if streamer is not None:
            # Give the streamer one final poll before shutdown so we don't miss
            # the last offer logged just before the subprocess exited.
            time.sleep(args.poll_interval + 0.5)
            stop.set()
            streamer.join(timeout=5)

    # Final status marker so the skill host knows the run ended.
    sys.stdout.write(json.dumps({"type": "exit", "code": result.returncode}) + "\n")
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
