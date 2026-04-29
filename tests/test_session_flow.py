from pathlib import Path
import json
import subprocess
from unittest.mock import MagicMock

from sshsign_session import LeaseHeldError, SshsignSession, SshsignSessionError

from negotiate_safe.session_flow import (
    join_signing_session,
    register_signing_session,
    role_pubkey_path,
    sshsign_session_id,
)


def _neg_dir(tmp_path, role: str) -> Path:
    neg_dir = tmp_path / "neg"
    (neg_dir / "keys").mkdir(parents=True)
    role_pubkey_path(neg_dir, role).write_text(
        f"-----BEGIN APOA-----\n{role.upper()}_FAKE_KEY\n-----END APOA-----\n"
    )
    return neg_dir


def _constraints(**overrides):
    data = {
        "company_name": "Acme",
        "founder_name": "Jane",
        "founder_title": "CEO",
        "investor_name": "Mark",
        "investor_firm": "Bay",
        "investment_amount": 500_000.0,
    }
    data.update(overrides)
    return data


def test_sshsign_session_id_prefixes_raw_negotiation_ids():
    assert sshsign_session_id("") == ""
    assert sshsign_session_id("neg_1") == "session_neg_1"
    assert sshsign_session_id("session_neg_1") == "session_neg_1"


def test_sshsign_session_acquire_lease_command_shape():
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv, 0,
            stdout=json.dumps({
                "session_id": "session_neg_1",
                "role": "founder",
                "action": "negotiate",
                "holder": "h1",
                "generation": 1,
            }),
            stderr="",
        )

    client = SshsignSession(host="sshsign.test", runner=runner)
    payload = client.acquire_lease(
        "session_neg_1", role="founder", action="negotiate",
        holder="h1", ttl_seconds=120,
    )

    assert payload["generation"] == 1
    assert calls == [[
        "ssh", "sshsign.test", "acquire-lease",
        "--session-id", "session_neg_1",
        "--role", "founder",
        "--action", "negotiate",
        "--holder", "h1",
        "--ttl", "120",
    ]]


def test_sshsign_session_maps_lease_conflict_payload():
    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 0,
            stdout=json.dumps({
                "error": "lease_held",
                "holder": "other",
                "expires_at": "2026-04-27T10:02:00Z",
            }),
            stderr="",
        )

    client = SshsignSession(host="sshsign.test", runner=runner)
    try:
        client.acquire_lease(
            "session_neg_1", role="founder", action="negotiate", holder="h1",
        )
    except LeaseHeldError as e:
        assert e.holder == "other"
        assert e.expires_at == "2026-04-27T10:02:00Z"
    else:
        raise AssertionError("expected LeaseHeldError")


def test_register_signing_session_publishes_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("USER_DID", "did:apoa:founder")
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "@FounderBot")
    neg_dir = _neg_dir(tmp_path, "founder")
    client = MagicMock()
    client.create_session.return_value = {
        "session_code": "INV-7K3X9",
        "created_at": "created",
        "expires_at": "expires",
        "status": "open",
    }

    result = register_signing_session(
        {"negotiation_id": "neg_1"},
        _constraints(),
        "founder",
        neg_dir,
        session_client=client,
        telegram_user_id=123,
    )

    assert result == {
        "session_code": "INV-7K3X9",
        "session_created_at": "created",
        "session_expires_at": "expires",
        "session_status": "open",
    }
    call = client.create_session.call_args
    assert call.kwargs["session_id"] == "session_neg_1"
    assert call.kwargs["role"] == "founder"
    assert "FOUNDER_FAKE_KEY" in call.kwargs["apoa_pubkey_pem"]
    assert call.kwargs["party_did"] == "did:apoa:founder"
    assert call.kwargs["metadata_public"] == {
        "use_case": "safe",
        "version": 1,
        "company_name": "Acme",
        "founder_name": "Jane",
        "founder_title": "CEO",
        "investor_name": "Mark",
        "investor_firm": "Bay",
        "founder_bot_handle": "@FounderBot",
    }
    assert call.kwargs["metadata_member"]["telegram"] == {"founder_user_id": 123}


def test_register_signing_session_missing_pubkey_returns_none(tmp_path):
    client = MagicMock()
    assert register_signing_session(
        {"negotiation_id": "neg_1"},
        _constraints(),
        "founder",
        tmp_path / "neg",
        session_client=client,
    ) is None
    client.create_session.assert_not_called()


def test_register_signing_session_error_returns_none(tmp_path):
    neg_dir = _neg_dir(tmp_path, "founder")
    client = MagicMock()
    client.create_session.side_effect = SshsignSessionError("boom")
    assert register_signing_session(
        {"negotiation_id": "neg_1"},
        _constraints(),
        "founder",
        neg_dir,
        session_client=client,
    ) is None


def test_join_signing_session_fetches_and_writes_counterparty_pubkey(tmp_path, monkeypatch):
    monkeypatch.setenv("USER_DID", "did:apoa:investor")
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "@InvestorBot")
    neg_dir = _neg_dir(tmp_path, "investor")
    client = MagicMock()
    client.join_session.return_value = {
        "session_id": "session_neg_1",
        "session_code": "INV-7K3X9",
        "status": "joined",
    }
    client.get_session.return_value = {
        "session_code": "INV-7K3X9",
        "status": "joined",
        "created_at": "created",
        "expires_at": "expires",
        "members": [
            {"role": "founder", "apoa_pubkey_pem": "FOUNDER_PEM"},
            {"role": "investor", "apoa_pubkey_pem": "INVESTOR_PEM"},
        ],
    }

    result = join_signing_session(
        {"negotiation_id": "neg_1"},
        {"session_code": "INV-7K3X9"},
        "investor",
        neg_dir,
        session_client=client,
    )

    assert result is not None
    assert result["session_status"] == "joined"
    assert result["counterparty_pubkey_path"].endswith("founder_public.pem")
    assert role_pubkey_path(neg_dir, "founder").read_text() == "FOUNDER_PEM"
    join_call = client.join_session.call_args
    assert join_call.kwargs["party_did"] == "did:apoa:investor"
    assert "INVESTOR_FAKE_KEY" in join_call.kwargs["apoa_pubkey_pem"]
    client.update_session_member_text.assert_called_once_with(
        "session_neg_1", field="bot_handle", text_value="@InvestorBot",
    )


def test_join_signing_session_missing_session_code_returns_none(tmp_path):
    neg_dir = _neg_dir(tmp_path, "investor")
    client = MagicMock()
    assert join_signing_session(
        {"negotiation_id": "neg_1"},
        {},
        "investor",
        neg_dir,
        session_client=client,
    ) is None
    client.join_session.assert_not_called()


def test_join_signing_session_post_join_fetch_failure_is_non_fatal(tmp_path):
    neg_dir = _neg_dir(tmp_path, "investor")
    client = MagicMock()
    client.join_session.return_value = {"session_code": "INV-X", "status": "joined"}
    client.get_session.side_effect = SshsignSessionError("transient")

    result = join_signing_session(
        {"negotiation_id": "neg_1"},
        {"session_code": "INV-X"},
        "investor",
        neg_dir,
        session_client=client,
    )

    assert result is not None
    assert result["session_code"] == "INV-X"
    assert result["counterparty_pubkey_path"] == ""
