#!/usr/bin/env python3
"""Block until results.md appears in the output dir, then print it.

Used as a second exec call after run_safe.py negotiate auto-backgrounds.
Simple python3 script.py command — no shell constructs needed.
"""
import argparse
import sys
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Wait for negotiation results")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--poll-interval", type=int, default=10)
    parser.add_argument("--max-wait", type=int, default=300)
    args = parser.parse_args()

    results_path = Path(args.output_dir) / "results.md"
    elapsed = 0

    while elapsed < args.max_wait:
        if results_path.exists():
            content = results_path.read_text()
            if content.strip():
                sys.stdout.write(content)
                return 0
        time.sleep(args.poll_interval)
        elapsed += args.poll_interval

    sys.stderr.write(f"results.md not found after {args.max_wait}s\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
