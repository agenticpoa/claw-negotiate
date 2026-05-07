"""Structural tests for the installable skill manifest."""
from __future__ import annotations

import json
from pathlib import Path


REPO_ROOT = Path(__file__).parent.parent
SKILL_DIR = REPO_ROOT / "negotiate_safe"
MANIFEST = SKILL_DIR / "skill_manifest.json"


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text())


def test_manifest_json_parses():
    data = _manifest()
    assert data["name"] == "negotiate_safe"
    assert data["entrypoint"] == "run_safe.py"
    assert data["skill_file"] == "SKILL.md"


def test_manifest_files_exist():
    data = _manifest()
    for rel in data["files"]:
        assert (SKILL_DIR / rel).exists(), f"manifest file missing: {rel}"


def test_manifest_includes_all_skill_python_files():
    data = _manifest()
    listed = set(data["files"])
    actual = {p.name for p in SKILL_DIR.glob("*.py")}
    assert actual <= listed


def test_manifest_requirements_match_skill_frontmatter():
    data = _manifest()
    skill = (SKILL_DIR / "SKILL.md").read_text()
    frontmatter = skill.split("---", 2)[1]
    metadata_line = next(
        line for line in frontmatter.splitlines()
        if line.startswith("metadata:")
    )
    metadata = json.loads(metadata_line.split("metadata:", 1)[1].strip())
    required = metadata["openclaw"]["requires"]

    assert set(required["bins"]) <= set(data["required_bins"])
    assert set(required["env"]) <= set(data["required_env"])


def test_readme_points_to_manifest_and_doctor():
    readme = (REPO_ROOT / "README.md").read_text()
    assert "skill_manifest.json" in readme
    assert "smoke_install.py" in readme
    assert "demo_checklist.py" in readme
    assert "run_safe.py doctor" in readme
    assert "operator-setup" in readme
    assert "docs/" not in readme


def test_public_markdown_allowlist():
    markdown_files = sorted(
        p.relative_to(REPO_ROOT).as_posix()
        for p in REPO_ROOT.rglob("*.md")
        if not {".git", ".pytest_cache", ".venv"}.intersection(p.parts)
    )
    assert markdown_files == [
        "README.md",
        "SKILL.md",
        "hooks/telegram-typing/HOOK.md",
        "negotiate_safe/SKILL.md",
    ]
