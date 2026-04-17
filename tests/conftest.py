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




@pytest.fixture
def sample_constraints() -> dict:
    return {
        "valuation_cap_min": 8_000_000,
        "valuation_cap_max": 12_000_000,
        "discount_min": 0.20,
        "pro_rata": "required",
        "mfn": "preferred",
        "company_name": "Acme Corp",
        "investor_name": "Angel Ventures",
        "investment_amount": 500_000.0,
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
        "intended_service": "safe:acme-corp:neg_abc123",
        "actual_service_in_token": "safe-agreement",
    }
