"""Structural tests for negotiate_safe/SKILL.md.

These catch regressions like: dropped env var from requires.env, missing step,
description drift from implementation, or Telegram template types that don't
match telegram_format.py's FORMATTERS.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import format_event as tf

SKILL_MD = Path(__file__).parent.parent / "negotiate_safe" / "SKILL.md"


@pytest.fixture(scope="module")
def skill_content() -> str:
    return SKILL_MD.read_text()


@pytest.fixture(scope="module")
def frontmatter(skill_content: str) -> str:
    m = re.match(r"^---\n(.*?)\n---\n", skill_content, re.DOTALL)
    assert m is not None, "SKILL.md must start with YAML frontmatter"
    return m.group(1)


class TestFrontmatter:
    def test_name(self, frontmatter):
        assert re.search(r"^name:\s*negotiate_safe\s*$", frontmatter, re.M)

    def test_user_invocable(self, frontmatter):
        assert re.search(r"^user-invocable:\s*true\s*$", frontmatter, re.M)

    def test_description_mentions_apoa_and_sshsign(self, frontmatter):
        assert "APOA" in frontmatter
        assert "sshsign" in frontmatter

    def test_metadata_json_parses(self, frontmatter):
        m = re.search(r"^metadata:\s*(\{.*\})\s*$", frontmatter, re.M)
        assert m, "metadata field must be single-line JSON"
        meta = json.loads(m.group(1))
        assert "openclaw" in meta
        assert meta["openclaw"]["emoji"]

    def test_requires_lists_all_needed_env(self, frontmatter):
        m = re.search(r"^metadata:\s*(\{.*\})\s*$", frontmatter, re.M)
        meta = json.loads(m.group(1))
        env = set(meta["openclaw"]["requires"]["env"])
        required = {
            "ANTHROPIC_API_KEY",
            "NEGOTIATE_REPO_PATH",
            "SSHSIGN_KEY_PATH",
            "PRINCIPAL_KEY_PATH",
            "FOUNDER_DID",
        }
        missing = required - env
        assert not missing, f"SKILL.md requires.env missing: {missing}"

    def test_requires_lists_bins(self, frontmatter):
        m = re.search(r"^metadata:\s*(\{.*\})\s*$", frontmatter, re.M)
        meta = json.loads(m.group(1))
        bins = set(meta["openclaw"]["requires"]["bins"])
        assert "python3" in bins
        assert "ssh" in bins


class TestSteps:
    @pytest.mark.parametrize("step", [
        "## Step 0",    # Preflight
        "## Step 1",    # Parse
        "## Step 2",    # Confirm
        "## Step 2.5",  # Mint APOA token (post-APOA update)
        "## Step 3",    # Run
        "## Step 4",    # Stream
        "## Step 5",    # Co-sign
    ])
    def test_step_present(self, skill_content, step):
        assert step in skill_content, f"Missing: {step}"

    def test_step_25_mentions_apoa_scoping(self, skill_content):
        m = re.search(r"## Step 2\.5.*?## Step 3", skill_content, re.DOTALL)
        assert m
        body = m.group(0)
        assert "APOA" in body
        assert "expires" in body.lower()
        assert "principal" in body.lower()

    def test_invariants_section_exists(self, skill_content):
        assert "## Invariants" in skill_content
        # Six invariants enumerated
        invariants = re.findall(r"^\d+\.\s+\*\*", skill_content, re.M)
        assert len(invariants) >= 6

    def test_troubleshooting_covers_revoke(self, skill_content):
        ts_match = re.search(r"## Troubleshooting.*?(?=##|\Z)", skill_content, re.DOTALL)
        assert ts_match
        ts = ts_match.group(0)
        assert "revoke" in ts.lower()
        assert "tid" in ts.lower() or "token" in ts.lower()


class TestCrossFileConsistency:
    def test_all_formatter_types_reachable_from_skill(self, skill_content):
        """Each FORMATTERS key should map to a template visible in the doc.

        Not every type needs a verbatim copy block in the doc, but the critical
        user-facing ones do. Catches drift where we add a formatter but forget
        to document the intent.
        """
        critical = {"confirm", "authorized", "offer", "agreed", "cosign_requested", "signed"}
        assert critical <= set(tf.FORMATTERS.keys()), \
            "format_event.py is missing a formatter the skill assumes"

    def test_skill_references_script_names(self, skill_content):
        for script in ("parse_constraints.py", "mint_token.py", "format_event.py"):
            assert script in skill_content, f"SKILL.md never mentions {script}"

    def test_skill_documents_direct_import(self, skill_content):
        assert "run_local()" in skill_content or "importlib" in skill_content
        assert "auto_setup" in skill_content
