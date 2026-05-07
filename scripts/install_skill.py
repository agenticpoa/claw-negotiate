#!/usr/bin/env python3
"""Install the negotiate_safe OpenClaw skill from this checkout.

The installer copies only files listed in negotiate_safe/skill_manifest.json so
local caches, scratch files, and development-only artifacts are not installed.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL_SRC = ROOT / "negotiate_safe"
MANIFEST = SKILL_SRC / "skill_manifest.json"
DEFAULT_TARGET = Path.home() / ".agents" / "skills" / "negotiate_safe"


def load_manifest() -> dict:
    try:
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - CLI should report a plain error
        raise SystemExit(f"Could not read {MANIFEST}: {exc}") from exc


def install_skill(target: Path, *, force: bool) -> None:
    manifest = load_manifest()
    files = manifest.get("files", [])
    if not isinstance(files, list) or not files:
        raise SystemExit("Manifest does not contain an installable files list.")

    target.mkdir(parents=True, exist_ok=True)

    for rel in files:
        src = SKILL_SRC / rel
        dst = target / rel
        if not src.is_file():
            raise SystemExit(f"Manifest file is missing: {src}")
        if dst.exists() and not force:
            raise SystemExit(
                f"{dst} already exists. Re-run with --force to overwrite skill files."
            )
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    print(f"Installed {manifest.get('name', 'negotiate_safe')} to {target}")
    print(f"Next: python3 {target / 'run_safe.py'} operator-setup --env-file .env")
    print(f"Then: python3 {target / 'run_safe.py'} doctor")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install claw-negotiate into OpenClaw.")
    parser.add_argument(
        "--target",
        type=Path,
        default=DEFAULT_TARGET,
        help=f"Skill install directory. Default: {DEFAULT_TARGET}",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing installed skill files.",
    )
    args = parser.parse_args(argv)

    install_skill(args.target.expanduser(), force=args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
