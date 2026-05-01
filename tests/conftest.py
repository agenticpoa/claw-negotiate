"""Shared fixtures and markers for the claw-negotiate test suite."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

# Make skill scripts importable as modules
REPO_ROOT = Path(__file__).parent.parent
SKILL_DIR = REPO_ROOT / "negotiate_safe"
sys.path.insert(0, str(SKILL_DIR))


def pytest_collection_modifyitems(config, items):
    """Skip integration tests unless RUN_INTEGRATION=1."""
    if os.environ.get("RUN_INTEGRATION") == "1":
        return
    skip = pytest.mark.skip(reason="integration tests disabled (set RUN_INTEGRATION=1)")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)




@pytest.fixture(autouse=True)
def _identity_configured(monkeypatch):
    """Default every test to 'identity already configured' so the first-run
    setup wizard only kicks in for tests that explicitly delete FOUNDER_NAME.
    """
    monkeypatch.setenv("FOUNDER_NAME", "Test User")


@pytest.fixture(autouse=True)
def _bot_role_either(monkeypatch):
    """Default every test to 'no role enforcement' so the bot-role gate
    in run_prepare doesn't interfere with tests that exercise the join
    branch (investor-shaped messages) or the post-parse role inference.
    Tests that specifically verify the gate explicitly override this
    via monkeypatch.setenv("NEGOTIATE_SAFE_BOT_ROLE", "founder"|"investor").
    """
    monkeypatch.setenv("NEGOTIATE_SAFE_BOT_ROLE", "either")


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path_factory, monkeypatch):
    """Point state_store at a per-test-session isolated dir so the
    single-active-negotiation gate doesn't read leftover pointers
    from prior test runs (or the developer's actual home dir).
    """
    isolated = tmp_path_factory.mktemp("oc-skill-state")
    monkeypatch.setenv("CLAW_NEGOTIATE_STATE_DIR", str(isolated))
    yield isolated


@pytest.fixture(autouse=True)
def _no_real_sshsign_session(monkeypatch):
    """Several code paths instantiate SshsignSession directly (rather
    than accepting an injected session_client) and call get_session
    or update_session_member. In tests we don't want any path to fire
    a real `ssh sshsign.dev …` subprocess. Patch the class globally
    to a MagicMock that raises SshsignSessionError by default — tests
    that need a specific session payload override the .get_session
    side_effect / return_value via their own MagicMock injection.
    """
    from unittest.mock import MagicMock
    from sshsign_session import SshsignSessionError

    def _factory(*a, **kw):
        m = MagicMock()
        m.get_session.side_effect = SshsignSessionError("not stubbed in this test")
        m.update_session_member.return_value = {"ok": True}
        m.update_session_member_text.return_value = {"ok": True}
        return m

    import run_safe as rs
    monkeypatch.setattr(rs, "SshsignSession", _factory)
    yield


@pytest.fixture(autouse=True)
def _no_real_investor_wait(monkeypatch, request):
    """The investor wait gate now runs unconditionally when role==investor
    (Path 1 supports investor-joins-before-founder-binds). Without this
    autouse mock, any test that drives run_negotiate down the investor
    path enters a poll loop that hits the SshsignSession mock — which
    raises by default — and loops forever waiting for streaming_at.
    Default to "streaming" return so tests proceed; tests that exercise
    the wait helper directly mark themselves with the real_wait marker
    or fixture to opt out.
    """
    if "real_wait" in request.keywords:
        return
    import run_safe as rs
    monkeypatch.setattr(
        rs, "_investor_wait_for_founder_streaming",
        lambda *a, **kw: "streaming",
    )


@pytest.fixture(autouse=True)
def _no_demo_session_pid():
    """Wipe any stale /tmp/safe_negotiate/.session.pid that an earlier
    test or live demo left behind. Must run before each test so the
    active-negotiation gate doesn't see a phantom PID."""
    from pathlib import Path
    pid = Path("/tmp/safe_negotiate/.session.pid")
    try:
        pid.unlink()
    except (OSError, FileNotFoundError):
        pass
    yield


@pytest.fixture
def sample_constraints() -> dict:
    return {
        "role": "founder",
        "mode": "demo",
        "session_code": None,
        "valuation_cap_min": 8_000_000,
        "valuation_cap_max": 12_000_000,
        "discount_min": 0.20,
        "discount_max": 0.20,
        "pro_rata": "required",
        "mfn": "preferred",
        "company_name": "Acme Corp",
        "founder_name": None,
        "founder_title": None,
        "investor_name": "Angel Ventures",
        "investor_firm": None,
        "investment_amount": 500_000.0,
        "investment_amount_min": None,
        "investment_amount_max": None,
    }


@pytest.fixture
def sample_offer() -> dict:
    return {
        "type": "offer",
        "round": 2,
        "party": "Founder",
        "rationale": "$6M is below our minimum. Counter at $10M with 20% discount.",
        "terms": {"valuation_cap": 10_000_000, "discount_rate": 0.20},
        "cap_min": 8_000_000,
        "cap_max": 12_000_000,
        "discount_min": 0.20,
        "immudb_tx": 48326,
    }


@pytest.fixture
def sample_founder_config(tmp_path) -> dict:
    return {
        "role": "founder",
        "negotiation_id": "neg_abc123",
        "session_id": "session_neg_abc123",
        "schema": "schemas/safe.json",
        "sshsign_host": "sshsign.dev",
        "investment_amount": 500_000.0,
        "company_name": "Acme Corp",
        "founder_signing_key_id": "key_founder_1",
        "investor_signing_key_id": "key_investor_1",
        "token": str(tmp_path / "tokens/founder.jwt"),
        "pubkey": str(tmp_path / "keys/founder_public.pem"),
        "signing_key_id": "key_founder_1",
        "name": "Jane Doe",
        "title": "CEO",
        "party_name": "Jane Doe",
        "investor_name": "Angel Ventures",
    }


@pytest.fixture
def sample_investor_config(tmp_path) -> dict:
    return {
        "role": "investor",
        "negotiation_id": "neg_abc123",
        "session_id": "session_neg_abc123",
        "schema": "schemas/safe.json",
        "sshsign_host": "sshsign.dev",
        "investment_amount": 500_000.0,
        "company_name": "Acme Corp",
        "founder_signing_key_id": "key_founder_1",
        "investor_signing_key_id": "key_investor_1",
        "token": str(tmp_path / "tokens/investor.jwt"),
        "pubkey": str(tmp_path / "keys/investor_public.pem"),
        "signing_key_id": "key_investor_1",
        "name": "Angel Ventures",
        "party_name": "Angel Ventures",
        "founder_name": "Jane Doe",
        "founder_title": "CEO",
    }


@pytest.fixture
def sample_mint_output(tmp_path, sample_founder_config, sample_investor_config) -> dict:
    f = tmp_path / "founder.json"
    i = tmp_path / "investor.json"
    f.write_text(json.dumps(sample_founder_config))
    i.write_text(json.dumps(sample_investor_config))
    return {
        "negotiation_id": "neg_abc123",
        "founder_config_path": str(f),
        "investor_config_path": str(i),
        "founder_token_path": str(tmp_path / "tokens/founder.jwt"),
        "investor_token_path": str(tmp_path / "tokens/investor.jwt"),
        "expires_at": "2026-04-16T15:00:00Z",
        "service": "safe:acme-corp:neg_abc123",
        "founder_constraints": {
            "cap_min": 8_000_000,
            "cap_max": 12_000_000,
            "discount_min": 0.20,
            "discount_max": 0.25,
            "pro_rata_required": True,
            "mfn_required": False,
        },
        "investor_constraints": {
            "cap_min": 6_000_000,
            "cap_max": 10_000_000,
            "discount_min": 0.15,
            "discount_max": 0.25,
            "pro_rata_required": False,
            "mfn_required": False,
        },
    }
