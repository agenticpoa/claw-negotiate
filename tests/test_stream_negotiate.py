"""Tests for the subprocess streaming helper config hydration."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import _stream_negotiate as sn


@dataclass
class FakeNegotiationConfig:
    negotiate_repo: Path
    negotiation_id: str
    founder_token_path: str
    investor_token_path: str
    founder_pubkey_path: str
    investor_pubkey_path: str
    company_name: str
    founder_name: str
    founder_title: str
    investor_name: str
    investor_firm: str
    investment_amount: float
    sshsign_host: str
    no_sshsign: bool
    output_dir: str
    signing_key_id: str
    founder_signing_key_id: str
    investor_signing_key_id: str
    json_events: bool
    poll: bool
    role: str = ""
    signer_role: str = ""


class FakeModule:
    __file__ = "/tmp/negotiate/negotiate.py"
    NegotiationConfig = FakeNegotiationConfig


def test_build_config_falls_back_to_constraints_for_missing_party_details(tmp_path):
    neg_dir = tmp_path / "negotiations" / "neg_1"
    neg_dir.mkdir(parents=True)
    founder_cfg = neg_dir / "founder.json"
    founder_cfg.write_text(json.dumps({
        "pubkey": str(neg_dir / "keys" / "founder_public.pem"),
        "company_name": "Acme",
        "name": "Juan Figuera",
        "title": "CEO",
        "founder_signing_key_id": "key_founder",
    }))
    (tmp_path / "mint.json").write_text(json.dumps({
        "negotiation_id": "neg_1",
        "mode": "two_party",
        "user_role": "founder",
        "founder_config_path": str(founder_cfg),
        "investor_config_path": str(neg_dir / "investor.json"),
        "founder_token_path": str(neg_dir / "tokens" / "founder.jwt"),
        "investor_token_path": str(neg_dir / "tokens" / "investor.jwt"),
    }))
    (tmp_path / "config.json").write_text(json.dumps({
        "constraints": {
            "investor_name": "Nora",
            "investor_firm": "Babes Fund",
            "investment_amount": 750000.0,
        },
    }))

    cfg = sn._build_config(FakeModule, tmp_path, "sshsign.test")

    assert cfg.role == "founder"
    assert cfg.signer_role == "founder"
    assert cfg.investor_name == "Nora"
    assert cfg.investor_firm == "Babes Fund"
    assert cfg.investment_amount == 750000.0
