from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import orchestrator
from sshsign_session import LeaseExpiredError


def _state(tmp_path: Path, role: str = "founder") -> dict:
    out = tmp_path / "out"
    out.mkdir()
    neg_dir = tmp_path / "neg"
    neg_output = neg_dir / "output"
    neg_output.mkdir(parents=True)
    founder_cfg_path = neg_dir / "founder.json"
    investor_cfg_path = neg_dir / "investor.json"
    founder_cfg_path.write_text(json.dumps({"signing_key_id": "key_founder"}))
    investor_cfg_path.write_text(json.dumps({"signing_key_id": "key_investor"}))
    (out / "config.json").write_text(json.dumps({
        "constraints": {"role": role},
    }))
    (out / "mint.json").write_text(json.dumps({
        "negotiation_id": "neg_1",
        "mode": "two_party",
        "user_role": role,
        "negotiate_repo_path": "/repo",
        "founder_config_path": str(founder_cfg_path),
        "investor_config_path": str(investor_cfg_path),
    }))
    return {
        "negotiation_id": "neg_1",
        "output_dir": str(out),
        "session_code": "INV-1",
        "role": role,
        f"{role}_dm_chat_id": "123",
    }


def _client():
    client = MagicMock()
    client.get_session.return_value = {
        "session_id": "session_neg_1",
        "status": "joined",
        "group_chat_id": -100,
    }
    client.acquire_lease.return_value = {"generation": 1}
    return client


def test_reconcile_runs_due_local_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAW_NEGOTIATE_DELIVERY_DIR", str(tmp_path / "deliveries"))
    client = _client()
    sender = MagicMock()

    turn_result = MagicMock(
        returncode=0,
        stdout=json.dumps({
            "type": "offer",
            "party": "founder",
            "round": 0,
            "terms": {"valuation_cap": 40_000_000, "discount_rate": 0.1},
            "message": "Opening.",
        }) + "\n",
        stderr="",
    )
    runner = MagicMock(return_value=turn_result)

    result = orchestrator.reconcile_state(
        _state(tmp_path, "founder"),
        session_client=client,
        sender=sender,
        history_fn=lambda *a, **k: [],
        turn_runner=runner,
    )

    assert result.status == "turn_ran"
    assert result.turn_ran is True
    assert sender.call_count == 1
    client.acquire_lease.assert_called_once()
    client.release_lease.assert_called_once()


def test_reconcile_waits_when_counterparty_due(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAW_NEGOTIATE_DELIVERY_DIR", str(tmp_path / "deliveries"))
    client = _client()
    sender = MagicMock()
    history = [{
        "round": 0,
        "from": "founder",
        "type": "offer",
        "metadata": json.dumps({"valuation_cap": 40_000_000}),
    }]

    result = orchestrator.reconcile_state(
        _state(tmp_path, "founder"),
        session_client=client,
        sender=sender,
        history_fn=lambda *a, **k: history,
        turn_runner=MagicMock(),
    )

    assert result.status == "waiting_for_counterparty"
    client.acquire_lease.assert_not_called()


def test_reconcile_prompts_founder_to_bind_group_after_investor_joins(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAW_NEGOTIATE_DELIVERY_DIR", str(tmp_path / "deliveries"))
    client = _client()
    client.get_session.return_value = {
        "session_id": "session_neg_1",
        "session_code": "INV-1",
        "status": "joined",
        "group_chat_id": 0,
        "metadata_public": json.dumps({
            "founder_bot_handle": "AgenticPOA_bot",
            "investor_name": "Nora Vassileva",
            "investor_firm": "SD Fund",
        }),
        "members": [{"role": "investor", "bot_handle": "@AgenticPOAInvestor_bot"}],
    }
    sender = MagicMock()

    result = orchestrator.reconcile_state(
        _state(tmp_path, "founder"),
        session_client=client,
        sender=sender,
        history_fn=lambda *a, **k: [],
        turn_runner=MagicMock(),
    )

    assert result.status == "waiting_for_group"
    message = sender.call_args.kwargs["message"]
    assert "Set up the negotiation room" in message
    assert "Nora Vassileva at SD Fund" in message
    assert sender.call_args.kwargs["reply_markup"]["inline_keyboard"][0][0] == {
        "text": "Add founder AI agent",
        "url": "https://t.me/AgenticPOA_bot?startgroup=INV-1",
    }


def test_reconcile_does_not_repeat_group_bind_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAW_NEGOTIATE_DELIVERY_DIR", str(tmp_path / "deliveries"))
    client = _client()
    client.get_session.return_value = {
        "session_id": "session_neg_1",
        "session_code": "INV-1",
        "status": "joined",
        "group_chat_id": 0,
    }
    state = _state(tmp_path, "founder")
    out = Path(state["output_dir"])
    (out / ".group_prompted_neg_1").write_text("1")
    sender = MagicMock()

    result = orchestrator.reconcile_state(
        state,
        session_client=client,
        sender=sender,
        history_fn=lambda *a, **k: [],
        turn_runner=MagicMock(),
    )

    assert result.status == "waiting_for_group"
    sender.assert_not_called()


def test_reconcile_requests_local_signature_after_accept(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAW_NEGOTIATE_DELIVERY_DIR", str(tmp_path / "deliveries"))
    client = _client()
    sender = MagicMock()
    dm_sender = MagicMock()
    history = [
        {"round": 0, "from": "founder", "type": "offer", "metadata": "{}"},
        {"round": 1, "from": "investor", "type": "accept", "metadata": "{}"},
    ]
    turn_result = MagicMock(
        returncode=0,
        stdout=json.dumps({
            "type": "signing",
            "pending_id": "pnd_founder",
            "approval_url": "https://sshsign.dev/approve/pnd_founder",
        }) + "\n",
        stderr="",
    )

    result = orchestrator.reconcile_state(
        _state(tmp_path, "founder"),
        session_client=client,
        sender=sender,
        dm_sender=dm_sender,
        history_fn=lambda *a, **k: history,
        turn_runner=MagicMock(return_value=turn_result),
    )

    assert result.status == "signing_requested"
    assert result.signing_event["pending_id"] == "pnd_founder"
    dm_sender.assert_called_once()


def test_reconcile_founder_finalizes_after_both_signatures(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAW_NEGOTIATE_DELIVERY_DIR", str(tmp_path / "deliveries"))
    client = _client()
    sender = MagicMock()
    state = _state(tmp_path, "founder")
    mint = json.loads((Path(state["output_dir"]) / "mint.json").read_text())
    pending_dir = Path(mint["founder_config_path"]).parent / "output"
    (pending_dir / "neg_1_founder_pending.txt").write_text("pnd_founder")
    history = [
        {"round": 0, "from": "founder", "type": "offer", "metadata": "{}"},
        {"round": 1, "from": "investor", "type": "accept", "metadata": "{}"},
    ]

    monkeypatch.setattr(orchestrator, "_session_signature_status", lambda *a, **k: {
        "status": "complete",
        "signers": [
            {"pending_id": "pnd_founder", "key_id": "key_founder"},
            {"pending_id": "pnd_investor", "key_id": "key_investor"},
        ],
    })
    pdf = tmp_path / "executed.pdf"
    pdf.write_text("pdf")
    monkeypatch.setattr(orchestrator, "finalize_executed_pdf", lambda *a, **k: pdf)

    result = orchestrator.reconcile_state(
        state,
        session_client=client,
        sender=sender,
        history_fn=lambda *a, **k: history,
        turn_runner=MagicMock(),
    )

    assert result.status == "finalized"
    assert (pending_dir / "neg_1_investor_pending.txt").read_text() == "pnd_investor"
    assert any(c.kwargs.get("media_path") == str(pdf) for c in sender.call_args_list)
    client.complete_session.assert_called_once()
    assert client.complete_session.call_args.kwargs["lease_holder"]
    assert client.complete_session.call_args.kwargs["lease_generation"] == 1
    assert client.check_lease.call_count == 2


def test_reconcile_does_not_finalize_with_only_founder_signature(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAW_NEGOTIATE_DELIVERY_DIR", str(tmp_path / "deliveries"))
    client = _client()
    sender = MagicMock()
    state = _state(tmp_path, "founder")
    mint = json.loads((Path(state["output_dir"]) / "mint.json").read_text())
    pending_dir = Path(mint["founder_config_path"]).parent / "output"
    (pending_dir / "neg_1_founder_pending.txt").write_text("pnd_founder")
    history = [
        {"round": 0, "from": "founder", "type": "offer", "metadata": "{}"},
        {"round": 1, "from": "investor", "type": "accept", "metadata": "{}"},
    ]

    monkeypatch.setattr(orchestrator, "_session_signature_status", lambda *a, **k: {
        "status": "complete",
        "signers": [
            {"pending_id": "pnd_founder", "key_id": "key_founder"},
        ],
    })
    finalize = MagicMock()
    monkeypatch.setattr(orchestrator, "finalize_executed_pdf", finalize)

    result = orchestrator.reconcile_state(
        state,
        session_client=client,
        sender=sender,
        history_fn=lambda *a, **k: history,
        turn_runner=MagicMock(),
    )

    assert result.status == "awaiting_both_signatures"
    finalize.assert_not_called()
    client.complete_session.assert_not_called()
    assert not any(c.kwargs.get("media_path") for c in sender.call_args_list)


def test_reconcile_stops_finalization_when_finalize_lease_is_stale(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAW_NEGOTIATE_DELIVERY_DIR", str(tmp_path / "deliveries"))
    client = _client()
    client.check_lease.side_effect = LeaseExpiredError("lease_expired")
    sender = MagicMock()
    state = _state(tmp_path, "founder")
    mint = json.loads((Path(state["output_dir"]) / "mint.json").read_text())
    pending_dir = Path(mint["founder_config_path"]).parent / "output"
    (pending_dir / "neg_1_founder_pending.txt").write_text("pnd_founder")
    history = [
        {"round": 0, "from": "founder", "type": "offer", "metadata": "{}"},
        {"round": 1, "from": "investor", "type": "accept", "metadata": "{}"},
    ]

    monkeypatch.setattr(orchestrator, "_session_signature_status", lambda *a, **k: {
        "status": "complete",
        "signers": [
            {"pending_id": "pnd_founder", "key_id": "key_founder"},
            {"pending_id": "pnd_investor", "key_id": "key_investor"},
        ],
    })
    pdf = tmp_path / "executed.pdf"
    pdf.write_text("pdf")
    monkeypatch.setattr(orchestrator, "finalize_executed_pdf", lambda *a, **k: pdf)

    result = orchestrator.reconcile_state(
        state,
        session_client=client,
        sender=sender,
        history_fn=lambda *a, **k: history,
        turn_runner=MagicMock(),
    )

    assert result.status == "finalize_lease_lost"
    client.complete_session.assert_not_called()
    assert not any(c.kwargs.get("media_path") == str(pdf) for c in sender.call_args_list)


def test_reconcile_maps_unknown_second_signer_to_counterparty(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAW_NEGOTIATE_DELIVERY_DIR", str(tmp_path / "deliveries"))
    client = _client()
    sender = MagicMock()
    state = _state(tmp_path, "founder")
    mint = json.loads((Path(state["output_dir"]) / "mint.json").read_text())
    pending_dir = Path(mint["founder_config_path"]).parent / "output"
    (pending_dir / "neg_1_founder_pending.txt").write_text("pnd_founder")
    history = [
        {"round": 0, "from": "founder", "type": "offer", "metadata": "{}"},
        {"round": 1, "from": "investor", "type": "accept", "metadata": "{}"},
    ]

    monkeypatch.setattr(orchestrator, "_session_signature_status", lambda *a, **k: {
        "status": "complete",
        "signers": [
            {"pending_id": "pnd_investor", "key_id": "real_joiner_key"},
            {"pending_id": "pnd_founder", "key_id": "key_founder"},
        ],
    })
    pdf = tmp_path / "executed.pdf"
    pdf.write_text("pdf")
    monkeypatch.setattr(orchestrator, "finalize_executed_pdf", lambda *a, **k: pdf)

    result = orchestrator.reconcile_state(
        state,
        session_client=client,
        sender=sender,
        history_fn=lambda *a, **k: history,
        turn_runner=MagicMock(),
    )

    assert result.status == "finalized"
    assert (pending_dir / "neg_1_investor_pending.txt").read_text() == "pnd_investor"
