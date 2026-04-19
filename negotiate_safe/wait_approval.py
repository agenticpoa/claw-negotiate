#!/usr/bin/env python3
"""Block until the sshsign signature is approved, then print confirmation.

Reads the pending ID from results.md in the output dir, polls
sshsign get-envelope every 10 seconds until approved or timeout.
"""
import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path


def extract_pending_id(output_dir: str) -> str | None:
    results = Path(output_dir) / "results.md"
    if not results.exists():
        return None
    content = results.read_text()
    match = re.search(r"pnd_[a-f0-9]+", content)
    return match.group(0) if match else None


def check_envelope(pending_id: str) -> dict | None:
    try:
        result = subprocess.run(
            ["ssh", "sshsign.dev", "get-envelope", "--id", pending_id],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout)
    except Exception:
        pass
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Wait for signature approval")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--poll-interval", type=int, default=10)
    parser.add_argument("--max-wait", type=int, default=300)
    args = parser.parse_args()

    pending_id = extract_pending_id(args.output_dir)
    if not pending_id:
        sys.stderr.write("No pending ID found in results.md\n")
        return 2

    sys.stderr.write(f"Waiting for signature on {pending_id}...\n")
    elapsed = 0

    while elapsed < args.max_wait:
        envelope = check_envelope(pending_id)
        if envelope and envelope.get("status") == "approved":
            sys.stdout.write(json.dumps({
                "type": "approved",
                "pending_id": pending_id,
                "status": "approved",
            }, indent=2) + "\n")
            return 0
        time.sleep(args.poll_interval)
        elapsed += args.poll_interval

    sys.stderr.write(f"Signature not approved after {args.max_wait}s\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
