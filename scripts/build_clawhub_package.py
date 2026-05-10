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
version: 0.1.0
description: APOA-constrained SAFE negotiation on OpenClaw. Two OpenClaws negotiate within user-approved bounds, stream offers in Telegram, and finalize with human-approved sshsign signatures.
author: agenticpoa
homepage: https://github.com/agenticpoa/claw-negotiate
user-invocable: true
metadata:
  openclaw:
    emoji: "🤝"
    tags:
      - negotiation
      - apoa
      - safe
      - telegram
      - sshsign
      - human-in-the-loop
    requires:
      bins:
        - python3
        - ssh
        - openclaw
      env:
        - USER_DID
        - NEGOTIATE_SAFE_BOT_ROLE
        - TELEGRAM_BOT_USERNAME
---

# claw-negotiate

APOA-constrained SAFE negotiation for OpenClaw.

For the demo, a founder and an investor each use their own OpenClaw. Each person privately authorizes negotiation bounds, the two OpenClaws negotiate in a Telegram room, and the final SAFE is signed only after each human approves their own sshsign link.

The point is not that agents can chat. The point is that agents can act creatively while staying inside authority their users explicitly granted.

## Demo

[Watch the demo](https://www.youtube.com/watch?v=T2Y2Tr__g_k)

## What This Shows

- Bounded AI-agent delegation through APOA authorizations.
- Two independent OpenClaws negotiating on behalf of two humans.
- Private negotiation bounds with public offer-by-offer visibility.
- Human approval before any signature is completed.
- Executed SAFE PDF with signatures and an sshsign audit trail.

## Typical Flow

1. The founder sends their OpenClaw a SAFE negotiation request with cap, check size, discount, and pro-rata limits.
2. The founder reviews the authorization card and replies `GO`.
3. The investor joins from their own OpenClaw with their own limits.
4. The parties create a Telegram negotiation room and bind it with `/bind INV-XXXXX`.
5. Both OpenClaws post offers in the group while APOA blocks out-of-bounds terms privately.
6. If a deal is reached, each human receives a private sshsign approval link.
7. After both humans sign, the executed SAFE is shared with the audit trail.

## Example Founder Request

```text
Live negotiation for Series Seed SAFE with Nora Vassileva at SD Capital.

Cap: $20M-$30M post.
Check: $500k-$1M.
Pro rata: required.
Discount: 0%
```

## Runtime Commands

- `profile` shows the saved founder or investor profile.
- `prepare` reads a negotiation request and renders an authorization card.
- `negotiate` mints the APOA authorization and runs the workflow after `GO`.
- `bind` connects an `INV-XXXXX` negotiation to a Telegram group.
- `cancel` revokes an in-progress negotiation.
- `doctor` checks local OpenClaw, Telegram, sshsign, and env setup.

## Security Model

- User bounds are stored in a per-negotiation APOA authorization.
- Offers are validated before they are displayed.
- Signing links are private DM-only.
- The SAFE is not executed unless each human signer personally approves in sshsign.
- Use dedicated Telegram bots and sshsign keys for public or production testing.
- Use a private per-chat work directory, such as `/tmp/claw-negotiate/<chat.id>`.

## OpenClaw Instructions

Use this skill for SAFE negotiation requests, exact `GO` confirmations after a review card, profile setup messages, `/bind ...` in Telegram groups, and cancellation requests.

Write user-supplied Telegram or negotiation text to a per-chat file before invoking the runtime. Do not pass freeform Telegram text as an inline command argument.

Dispatch:

```text
/bind ...
  write message to /tmp/claw-negotiate/<chat.id>/bind.txt
  python3 {baseDir}/negotiate_safe/run_safe.py bind --message-file /tmp/claw-negotiate/<chat.id>/bind.txt --chat-id <chat.id> --from-id <from.id>

/cancel, cancel, stop, abort
  python3 {baseDir}/negotiate_safe/run_safe.py cancel --output-dir /tmp/claw-negotiate/<chat.id> --chat-id <chat.id>

GO
  python3 {baseDir}/negotiate_safe/run_safe.py negotiate --output-dir /tmp/claw-negotiate/<chat.id> --chat-id <chat.id>

profile lookup
  python3 {baseDir}/negotiate_safe/run_safe.py profile

profile update or first-run identity setup
  write message to /tmp/claw-negotiate/<chat.id>/identity.txt
  python3 {baseDir}/negotiate_safe/run_safe.py setup --message-file /tmp/claw-negotiate/<chat.id>/identity.txt

new negotiation request or correction
  write message to /tmp/claw-negotiate/<chat.id>/request.txt
  python3 {baseDir}/negotiate_safe/run_safe.py prepare --message-file /tmp/claw-negotiate/<chat.id>/request.txt --output-dir /tmp/claw-negotiate/<chat.id>
```

For `negotiate`, use a long timeout. The runtime posts Telegram cards, signing links, status updates, and the executed PDF itself.
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
