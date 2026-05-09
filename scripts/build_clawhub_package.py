#!/usr/bin/env python3
"""Build the lean ClawHub package for claw-negotiate.

The GitHub repo includes tests, demo helpers, optional hooks, and media assets.
The ClawHub package should be narrower: just the OpenClaw skill instructions,
runtime, runtime requirements, setup example, and a smoke-check helper.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = ROOT / "negotiate_safe"
MANIFEST_PATH = RUNTIME_DIR / "skill_manifest.json"
DEFAULT_OUT = ROOT / "dist" / "claw-negotiate-clawhub"

ROOT_FILES = [
    "README.md",
    ".env.example",
    "requirements.txt",
]

SCRIPT_FILES = [
    "scripts/smoke_install.py",
    "scripts/demo_checklist.py",
]

PUBLIC_SKILL = """---
name: negotiate_safe
description: Negotiate a SAFE for a founder or investor using APOA-bounded authority, OpenClaw, Telegram, and sshsign.
user-invocable: true
metadata: {"openclaw":{"emoji":"🤝","requires":{"bins":["python3","ssh","openclaw"],"env":["USER_DID","NEGOTIATE_SAFE_BOT_ROLE","TELEGRAM_BOT_USERNAME"]},"homepage":"https://github.com/agenticpoa/claw-negotiate"}}
---

Use this skill for SAFE negotiation requests, exact `GO` confirmations after a review card, profile setup messages, `/bind ...` in Telegram groups, and cancellation requests.

Run the matching command with `{baseDir}`:

- `/bind ...`: `python3 {baseDir}/negotiate_safe/run_safe.py bind --message "<message text>" --chat-id <chat.id> --from-id <from.id>`
- `/cancel`, `cancel`, `stop`, or `abort`: `python3 {baseDir}/negotiate_safe/run_safe.py cancel --output-dir /tmp/claw-negotiate/<chat.id> --chat-id <chat.id>`
- exact `GO`: `python3 {baseDir}/negotiate_safe/run_safe.py negotiate --output-dir /tmp/claw-negotiate/<chat.id> --chat-id <chat.id>`
- profile lookup: `python3 {baseDir}/negotiate_safe/run_safe.py profile`
- profile update or first-run identity setup: write the message to `/tmp/claw-negotiate/<chat.id>/identity.txt`, then run `python3 {baseDir}/negotiate_safe/run_safe.py setup --message-file /tmp/claw-negotiate/<chat.id>/identity.txt`
- new negotiation request or correction: write the message to `/tmp/claw-negotiate/<chat.id>/request.txt`, then run `python3 {baseDir}/negotiate_safe/run_safe.py prepare --message-file /tmp/claw-negotiate/<chat.id>/request.txt --output-dir /tmp/claw-negotiate/<chat.id>`

For `negotiate`, use a long timeout. The runtime posts Telegram cards, signing links, status updates, and the executed PDF itself, so let the runtime handle follow-up Telegram updates after the command starts.

Operational notes:
- Use a separate private work directory for each chat, such as `/tmp/claw-negotiate/<chat.id>`, with permissions limited to the local OpenClaw user.
- User bounds are stored in the per-negotiation APOA authorization and enforced by the runtime before offers are displayed.
- Signing links are private DM-only. The agent can request signing, but the SAFE is not executed unless each human signer personally approves in sshsign.
- If the user changes bounds, cancel and start a new negotiation.
- Run `python3 {baseDir}/negotiate_safe/run_safe.py doctor` to check host setup.
"""


def _copy_file(src: Path, dst: Path) -> None:
    if not src.is_file():
        raise SystemExit(f"missing package source file: {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def build_package(out_dir: Path) -> None:
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    for rel in ROOT_FILES + SCRIPT_FILES:
        _copy_file(ROOT / rel, out_dir / rel)
    (out_dir / "SKILL.md").write_text(PUBLIC_SKILL, encoding="utf-8")

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    manifest["files"] = [rel for rel in manifest.get("files", []) if rel != "SKILL.md"]
    for rel in manifest["files"]:
        _copy_file(RUNTIME_DIR / rel, out_dir / "negotiate_safe" / rel)
    (out_dir / "negotiate_safe" / "skill_manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )

    package_manifest = {
        "source": "https://github.com/agenticpoa/claw-negotiate",
        "purpose": "Lean ClawHub package generated from the public repository.",
        "included": {
            "root_files": ROOT_FILES,
            "generated_root_files": ["SKILL.md"],
            "scripts": SCRIPT_FILES,
            "runtime_manifest": "negotiate_safe/skill_manifest.json",
        },
        "excluded": [
            "tests/",
            "hooks/",
            "docs/",
            "assets/",
            "development caches",
        ],
    }
    (out_dir / "CLAWHUB_PACKAGE.json").write_text(
        json.dumps(package_manifest, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Build lean ClawHub package")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    out_dir = args.out.resolve()
    build_package(out_dir)
    print(out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
