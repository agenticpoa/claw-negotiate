from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

import identity


def test_build_env_updates_for_founder():
    updates = identity.build_env_updates({
        "role": "founder",
        "name": "Juan Figuera",
        "title": "CEO",
        "company": "APOA Inc",
        "firm": None,
    })

    assert updates == {
        "FOUNDER_NAME": "Juan Figuera",
        "FOUNDER_TITLE": "CEO",
        "COMPANY_NAME": "APOA Inc",
    }


def test_build_env_updates_for_investor():
    updates = identity.build_env_updates({
        "role": "investor",
        "name": "Nora",
        "title": "Partner",
        "company": None,
        "firm": "Babes Fund",
    })

    assert updates == {
        "INVESTOR_NAME": "Nora",
        "INVESTOR_FIRM": "Babes Fund",
    }


def test_profile_from_env_mapping():
    profile = identity.profile_from_env({
        "FOUNDER_NAME": "Juan",
        "FOUNDER_TITLE": "CEO",
        "COMPANY_NAME": "APOA",
        "INVESTOR_NAME": "Nora",
        "INVESTOR_FIRM": "Babes Fund",
    })

    assert profile["founder_name"] == "Juan"
    assert profile["investor_firm"] == "Babes Fund"


def test_persist_env_updates_calls_openclaw_config_set():
    runner = MagicMock(return_value=MagicMock(returncode=0, stdout="", stderr=""))
    failures = identity.persist_env_updates({"FOUNDER_NAME": "Juan"}, runner=runner)

    assert failures == []
    assert runner.call_args.args[0] == [
        "openclaw",
        "config",
        "set",
        "skills.entries.negotiate_safe.env.FOUNDER_NAME",
        "Juan",
    ]


def test_persist_env_updates_reports_timeout():
    def runner(*args, **kwargs):
        raise subprocess.TimeoutExpired("openclaw", 10)

    failures = identity.persist_env_updates({"FOUNDER_NAME": "Juan"}, runner=runner)

    assert failures == ["FOUNDER_NAME"]


def test_persist_env_updates_accepts_written_value_despite_nonzero(tmp_path, monkeypatch):
    cfg = tmp_path / "openclaw.json"
    cfg.write_text(
        '{"skills":{"entries":{"negotiate_safe":{"env":{"INVESTOR_NAME":"Nora"}}}}}'
    )
    monkeypatch.setattr(identity, "OPENCLAW_CONFIG_PATH", cfg)
    runner = MagicMock(return_value=MagicMock(returncode=1, stdout="", stderr="pairing required"))

    failures = identity.persist_env_updates({"INVESTOR_NAME": "Nora"}, runner=runner)

    assert failures == []


def test_persist_env_updates_accepts_written_value_after_timeout(tmp_path, monkeypatch):
    cfg = tmp_path / "openclaw.json"
    cfg.write_text(
        '{"skills":{"entries":{"negotiate_safe":{"env":{"INVESTOR_NAME":"Nora"}}}}}'
    )
    monkeypatch.setattr(identity, "OPENCLAW_CONFIG_PATH", cfg)

    def runner(*args, **kwargs):
        raise subprocess.TimeoutExpired("openclaw", kwargs.get("timeout"))

    failures = identity.persist_env_updates({"INVESTOR_NAME": "Nora"}, runner=runner)

    assert failures == []
