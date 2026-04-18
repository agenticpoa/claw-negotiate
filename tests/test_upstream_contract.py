"""Contract tests for upstream agenticpoa/negotiate.

Asserts that the CLI flags and function signatures our wrapper depends on
still exist in the upstream repo. Catches renames/removals before they
break us silently. Requires NEGOTIATE_REPO_PATH to be set.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

NEGOTIATE_REPO = os.environ.get("NEGOTIATE_REPO_PATH", "")


@pytest.fixture
def upstream_negotiate_source():
    if not NEGOTIATE_REPO or not Path(NEGOTIATE_REPO).exists():
        pytest.skip("NEGOTIATE_REPO_PATH not set or invalid")
    return (Path(NEGOTIATE_REPO) / "negotiate.py").read_text()


@pytest.fixture
def upstream_create_tokens_source():
    if not NEGOTIATE_REPO or not Path(NEGOTIATE_REPO).exists():
        pytest.skip("NEGOTIATE_REPO_PATH not set or invalid")
    return (Path(NEGOTIATE_REPO) / "create_tokens.py").read_text()


class TestNegotiateFlags:
    """Flags our wrapper depends on in upstream negotiate.py."""

    REQUIRED_FLAGS = [
        "--schema",
        "--role",
        "--founder-token",
        "--investor-token",
        "--founder-pubkey",
        "--investor-pubkey",
        "--founder-cap-min",
        "--founder-cap-max",
        "--founder-discount-min",
        "--founder-discount-max",
        "--founder-pro-rata-required",
        "--founder-mfn-required",
        "--investor-cap-min",
        "--investor-cap-max",
        "--sshsign-host",
        "--negotiation-id",
        "--session-id",
        "--founder-signing-key-id",
        "--investor-signing-key-id",
        "--investment-amount",
        "--company-name",
        "--founder-name",
        "--founder-title",
        "--investor-name",
        "--output-dir",
        "--no-sshsign",
        "--poll",
    ]

    @pytest.mark.parametrize("flag", REQUIRED_FLAGS)
    def test_flag_exists(self, upstream_negotiate_source, flag):
        assert f'"{flag}"' in upstream_negotiate_source, (
            f"Upstream negotiate.py missing flag {flag}. "
            f"Our wrapper depends on it via build_namespace()."
        )

    def test_run_local_exists(self, upstream_negotiate_source):
        assert "async def run_local(" in upstream_negotiate_source

    def test_run_local_takes_args(self, upstream_negotiate_source):
        match = re.search(r"async def run_local\((\w+)", upstream_negotiate_source)
        assert match, "run_local signature not found"
        assert match.group(1) == "args", "run_local should take 'args' as first param"


class TestCreateTokensFlags:
    """Flags our mint_token.py depends on in upstream create_tokens.py."""

    REQUIRED_FLAGS = [
        "--negotiation-id",
        "--principal-id",
        "--expires",
        "--company-name",
        "--founder-name",
        "--founder-title",
        "--investor-name",
        "--investment-amount",
        "--founder-cap-min",
        "--founder-cap-max",
        "--founder-discount-min",
        "--founder-discount-max",
        "--founder-pro-rata-required",
        "--founder-mfn-required",
        "--keys-dir",
        "--tokens-dir",
        "--config-dir",
        "--create-keys",
    ]

    @pytest.mark.parametrize("flag", REQUIRED_FLAGS)
    def test_flag_exists(self, upstream_create_tokens_source, flag):
        assert f'"{flag}"' in upstream_create_tokens_source, (
            f"Upstream create_tokens.py missing flag {flag}. "
            f"Our mint_token.py depends on it."
        )
