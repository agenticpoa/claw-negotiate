from __future__ import annotations

import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).parent.parent
CHECKLIST = REPO_ROOT / "docs" / "demo_checklist.md"
SCRIPT = REPO_ROOT / "scripts" / "demo_checklist.py"


def test_demo_checklist_contains_core_proof_points():
    text = CHECKLIST.read_text()
    required = [
        "APOA",
        "bounded",
        "audit",
        "human",
        "signing links",
        "executed SAFE",
        "INV-XXXXX",
        "/bind",
        "Round 0",
        "Nora Vassileva",
        "SD Capital",
        "Avocado",
        "APOA blocked",
    ]
    for term in required:
        assert term in text


def test_demo_checklist_has_expected_sections():
    text = CHECKLIST.read_text()
    for heading in [
        "## Preflight",
        "## Founder Starts",
        "## Investor Joins",
        "## Group Bind",
        "## APOA Proof Points",
        "## Recording Reminders",
    ]:
        assert heading in text


def test_demo_checklist_script_prints_markdown():
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert result.stdout.startswith("# APOA SAFE Negotiation Demo Checklist")
    assert "APOA Proof Points" in result.stdout


def test_demo_checklist_script_quick_mode_prints_pasteable_steps():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--quick"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert result.stdout.startswith("# APOA SAFE Demo Quick Script")
    assert "Live negotiation for Series Seed SAFE with Nora Vassileva at SD Capital" in result.stdout
    assert "/bind INV-XXXXX" in result.stdout
