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
    "SKILL.md",
    ".env.example",
    "requirements.txt",
]

SCRIPT_FILES = [
    "scripts/smoke_install.py",
    "scripts/demo_checklist.py",
]


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

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    for rel in manifest.get("files", []):
        _copy_file(RUNTIME_DIR / rel, out_dir / "negotiate_safe" / rel)

    package_manifest = {
        "source": "https://github.com/agenticpoa/claw-negotiate",
        "purpose": "Lean ClawHub package generated from the public repository.",
        "included": {
            "root_files": ROOT_FILES,
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
