"""Structural tests for negotiate_safe/SKILL.md.

Validates the two-command architecture (prepare + negotiate via run_safe.py).
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

    def test_requires_essential_env(self, frontmatter):
        m = re.search(r"^metadata:\s*(\{.*\})\s*$", frontmatter, re.M)
        meta = json.loads(m.group(1))
        env = set(meta["openclaw"]["requires"]["env"])
        # Core secrets + the installed user's APOA DID. Party-identity env
        # vars (FOUNDER_NAME, INVESTOR_NAME, COMPANY_NAME, etc) come from
        # upstream's .env.example convention; they're optional because the
        # user can also supply them in the NL, so they're not in `requires`.
        assert {"ANTHROPIC_API_KEY", "NEGOTIATE_REPO_PATH", "USER_DID"} <= env

    def test_requires_lists_bins(self, frontmatter):
        m = re.search(r"^metadata:\s*(\{.*\})\s*$", frontmatter, re.M)
        meta = json.loads(m.group(1))
        bins = set(meta["openclaw"]["requires"]["bins"])
        assert "python3" in bins
        assert "ssh" in bins


class TestSteps:
    @pytest.mark.parametrize("step", [
        "## Step 1",
        "## Step 2",
        "## Step 3",
    ])
    def test_step_present(self, skill_content, step):
        assert step in skill_content, f"Missing: {step}"

    def test_invariants_section_exists(self, skill_content):
        assert "## Invariants" in skill_content
        invariants = re.findall(r"^\d+\.\s+\*\*", skill_content, re.M)
        assert len(invariants) >= 4

    def test_troubleshooting_exists(self, skill_content):
        assert "## Troubleshooting" in skill_content


class TestTwoCommandArchitecture:
    def test_uses_run_safe_py(self, skill_content):
        assert "run_safe.py" in skill_content

    def test_has_prepare_command(self, skill_content):
        assert "run_safe.py prepare" in skill_content

    def test_has_negotiate_command(self, skill_content):
        assert "run_safe.py negotiate" in skill_content

    def test_mentions_background_exec(self, skill_content):
        assert "background" in skill_content.lower()
        assert "timeout" in skill_content.lower()

    def test_no_pipe_instructions(self, skill_content):
        assert "NEVER use pipes" in skill_content or "NEVER use pipe" in skill_content

    def test_mentions_output_dir(self, skill_content):
        assert "--output-dir" in skill_content

    def test_has_bind_shortcut(self, skill_content):
        """Phase 8: /bind must be listed as an intent shortcut so the model
        routes it straight to run_safe.py bind instead of the prepare path."""
        assert "/bind" in skill_content
        assert "run_safe.py bind" in skill_content
        # Must pass through the envelope flags so the skill can do the ACL check.
        assert "--chat-id" in skill_content
        assert "--from-id" in skill_content


class TestCrossFileConsistency:
    def test_all_formatter_types_reachable_from_skill(self, skill_content):
        critical = {
            "confirm", "authorized",
            "offer", "counter", "accept",
            "outcome", "signing", "signed",
        }
        assert critical <= set(tf.FORMATTERS.keys()), \
            "format_event.py is missing a formatter the skill assumes"
