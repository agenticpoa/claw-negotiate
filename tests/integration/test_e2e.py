"""Integration tests. Opt-in via RUN_INTEGRATION=1.

These hit real external services and/or require the local negotiate repo to
exist. Run selectively:

    RUN_INTEGRATION=1 pytest tests/integration/test_e2e.py -v

Env assumed:
  - ANTHROPIC_API_KEY         (real key)
  - NEGOTIATE_REPO_PATH       (path to agenticpoa/negotiate checkout)
  - PRINCIPAL_KEY_PATH        (optional, for mint)
  - SSHSIGN_HOST              (optional, defaults to sshsign.dev)

These tests are slower, cost real API credits, and may hit network flakes.
Keep them narrow: happy-path smoke tests only.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).parent.parent.parent / "negotiate-safe"


pytestmark = pytest.mark.integration


def test_parse_constraints_real_api():
    """Sanity check that parse_constraints emits valid JSON for a simple request."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY not set")

    msg = "Negotiate my SAFE for Acme Corp with Angel Ventures. Cap $8M to $12M, discount at least 20%, pro-rata required. $500k."
    result = subprocess.run(
        [sys.executable, str(SKILL_DIR / "parse_constraints.py")],
        input=msg,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)

    assert parsed["valuation_cap_min"] == 8_000_000
    assert parsed["valuation_cap_max"] == 12_000_000
    assert parsed["discount_min"] == 0.20
    assert parsed["pro_rata"] == "required"
    assert parsed["company_name"] in ("Acme Corp", "Acme")
    assert parsed["investor_name"] in ("Angel Ventures", "Angel")
    assert parsed["investment_amount"] == 500_000


def test_mint_token_with_real_repo():
    """Mint a real token pair against a local negotiate repo checkout.

    Writes into {repo}/negotiations/{id}/ so it's isolated per-run.
    """
    repo = os.environ.get("NEGOTIATE_REPO_PATH")
    if not repo or not Path(repo).exists():
        pytest.skip("NEGOTIATE_REPO_PATH not set or invalid")

    constraints = {
        "valuation_cap_min": 8_000_000,
        "valuation_cap_max": 12_000_000,
        "discount_min": 0.20,
        "pro_rata": "required",
        "mfn": "preferred",
        "company_name": "Test Co",
        "investor_name": "Test Ventures",
        "investment_amount": 100_000.0,
    }
    result = subprocess.run(
        [
            sys.executable, str(SKILL_DIR / "mint_token.py"),
            "--constraints-json", json.dumps(constraints),
            "--company-name", "Test Co",
            "--founder-name", "Test Founder",
            "--investor-name", "Test Ventures",
            "--investment-amount", "100000",
            "--skip-sshsign-keys",  # don't hit sshsign in tests
            "--ttl-seconds", "300",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr

    out = json.loads(result.stdout)
    for key in ("negotiation_id", "founder_config_path", "investor_config_path",
                "founder_token_path", "investor_token_path", "expires_at"):
        assert key in out, f"Missing key: {key}"

    assert Path(out["founder_config_path"]).exists()
    assert Path(out["investor_config_path"]).exists()
    assert Path(out["founder_token_path"]).exists()
    assert Path(out["investor_token_path"]).exists()

    # Clean up
    import shutil
    nego_dir = Path(out["founder_config_path"]).parent
    shutil.rmtree(nego_dir, ignore_errors=True)
