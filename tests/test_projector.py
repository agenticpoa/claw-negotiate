from __future__ import annotations

import json
from unittest.mock import MagicMock

import delivery_store
import projector


class FakeDeliveryClient:
    def __init__(self, created=True):
        self.created = created
        self.calls = []

    def claim_delivery(self, session_id, key, target=""):
        self.calls.append({
            "session_id": session_id,
            "key": key,
            "target": target,
        })
        return {"created": self.created}


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


def test_project_history_does_not_repost_counterparty_offer(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAW_NEGOTIATE_DELIVERY_DIR", str(tmp_path))
    sender = MagicMock()
    rows = [{
        "round": 0,
        "from": "founder",
        "type": "offer",
        "metadata": json.dumps({
            "valuation_cap": 40_000_000,
            "discount_rate": 0.10,
            "_message": "Founder opening.",
        }),
    }]

    count = projector.project_history(
        session_id="session_neg_1",
        history_rows=rows,
        constraints={"mode": "two_party", "role": "investor"},
        dm_chat_id="123",
        group_chat_id="-100",
        sender=sender,
    )

    assert count == 0
    sender.assert_not_called()
    assert not delivery_store.has_delivery(
        "session_neg_1", "offer:founder:0:offer",
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


def test_project_event_uses_server_delivery_claim_before_send(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAW_NEGOTIATE_DELIVERY_DIR", str(tmp_path))
    sender = MagicMock()
    client = FakeDeliveryClient(created=True)

    projected = projector.project_event(
        session_id="session_neg_1",
        event={
            "type": "counter",
            "party": "investor",
            "round": 1,
            "valuation_cap": 30_000_000,
            "discount_rate": 0.10,
            "message": "Investor counter.",
        },
        constraints={"mode": "two_party", "role": "investor"},
        dm_chat_id="123",
        group_chat_id="-100",
        sender=sender,
        delivery_client=client,
    )

    assert projected is True
    sender.assert_called_once()
    assert client.calls == [{
        "session_id": "session_neg_1",
        "key": "offer:investor:1:counter",
        "target": "-100",
    }]
    assert not delivery_store.has_delivery(
        "session_neg_1", "offer:investor:1:counter",
    )


def test_project_event_suppresses_when_server_delivery_already_claimed(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAW_NEGOTIATE_DELIVERY_DIR", str(tmp_path))
    sender = MagicMock()
    client = FakeDeliveryClient(created=False)

    projected = projector.project_event(
        session_id="session_neg_1",
        event={
            "type": "counter",
            "party": "investor",
            "round": 1,
            "valuation_cap": 30_000_000,
            "discount_rate": 0.10,
            "message": "Investor counter.",
        },
        constraints={"mode": "two_party", "role": "investor"},
        dm_chat_id="123",
        group_chat_id="-100",
        sender=sender,
        delivery_client=client,
    )

    assert projected is False
    sender.assert_not_called()
    assert client.calls[0]["key"] == "offer:investor:1:counter"
