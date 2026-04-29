from __future__ import annotations

import json
from unittest.mock import MagicMock

import delivery_store
import projector


def test_project_history_sends_local_role_once(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAW_NEGOTIATE_DELIVERY_DIR", str(tmp_path))
    sender = MagicMock()
    rows = [
        {
            "round": 0,
            "from": "founder",
            "type": "offer",
            "metadata": json.dumps({
                "valuation_cap": 40_000_000,
                "discount_rate": 0.10,
                "_message": "Founder opening.",
            }),
        },
        {
            "round": 1,
            "from": "investor",
            "type": "counter",
            "metadata": json.dumps({
                "valuation_cap": 20_000_000,
                "discount_rate": 0.10,
                "_message": "Investor counter.",
            }),
        },
    ]

    count = projector.project_history(
        session_id="session_neg_1",
        history_rows=rows,
        constraints={"mode": "two_party", "role": "investor"},
        dm_chat_id="123",
        group_chat_id="-100",
        sender=sender,
    )
    again = projector.project_history(
        session_id="session_neg_1",
        history_rows=rows,
        constraints={"mode": "two_party", "role": "investor"},
        dm_chat_id="123",
        group_chat_id="-100",
        sender=sender,
    )

    assert count == 1
    assert again == 0
    assert sender.call_count == 1
    assert sender.call_args.args[0] == "-100"
    assert delivery_store.has_delivery(
        "session_neg_1", "offer:investor:1:counter",
    )


def test_project_signing_records_dm_and_group_targets(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAW_NEGOTIATE_DELIVERY_DIR", str(tmp_path))
    sender = MagicMock()
    dm_sender = MagicMock()

    projected = projector.project_event(
        session_id="session_neg_sign",
        event={
            "type": "signing",
            "pending_id": "pnd_123",
            "approval_url": "https://sshsign.dev/approve/pnd_123",
        },
        constraints={"mode": "two_party", "role": "founder"},
        dm_chat_id="123",
        group_chat_id="-100",
        sender=sender,
        dm_sender=dm_sender,
    )

    assert projected is True
    dm_sender.assert_called_once()
    sender.assert_called_once()
    payload = delivery_store.read_deliveries("session_neg_sign")
    assert payload["delivered"]["signing:pnd_123"]["target"] == "dm:123;group:-100"
