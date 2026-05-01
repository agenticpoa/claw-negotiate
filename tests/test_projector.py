from __future__ import annotations

import json
import urllib.parse
from types import SimpleNamespace
from unittest.mock import MagicMock

import delivery_store
import projector


class FakeDeliveryClient:
    def __init__(self, created=True, deliveries=None):
        self.created = created
        self.deliveries = deliveries or {}
        self.calls = []

    def claim_delivery(self, session_id, key, target=""):
        self.calls.append({
            "session_id": session_id,
            "key": key,
            "target": target,
        })
        return {"created": self.created}

    def get_delivery(self, session_id, key):
        return self.deliveries.get(key, {})


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
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "AgenticPOA_bot")
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
    dm_message = dm_sender.call_args.kwargs["message"]
    assert "callback=" in dm_message
    assert urllib.parse.quote("https://t.me/AgenticPOA_bot") in dm_message
    sender.assert_called_once()
    payload = delivery_store.read_deliveries("session_neg_sign")
    assert payload["delivered"]["signing:pnd_123"]["target"] == "dm:123;group:-100"


def test_project_signing_can_suppress_group_placeholder(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAW_NEGOTIATE_DELIVERY_DIR", str(tmp_path))
    sender = MagicMock()
    dm_sender = MagicMock()

    projected = projector.project_event(
        session_id="session_neg_sign",
        event={
            "type": "signing",
            "pending_id": "pnd_123",
            "approval_url": "https://sshsign.dev/approve/pnd_123",
            "_suppress_group_placeholder": True,
        },
        constraints={"mode": "two_party", "role": "founder"},
        dm_chat_id="123",
        group_chat_id="-100",
        sender=sender,
        dm_sender=dm_sender,
    )

    assert projected is True
    dm_sender.assert_called_once()
    sender.assert_not_called()


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


def test_project_event_edits_matching_heartbeat_instead_of_sending_new_card(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAW_NEGOTIATE_DELIVERY_DIR", str(tmp_path))
    sender = MagicMock()
    editor = MagicMock(return_value=SimpleNamespace(ok=True))
    client = FakeDeliveryClient(
        created=True,
        deliveries={
            "turn_heartbeat:investor:1": {"message_id": "88", "target": "-100"},
        },
    )

    projected = projector.project_event(
        session_id="session_neg_1",
        event={
            "type": "counter",
            "party": "investor",
            "round": 1,
            "terms": {"valuation_cap": 30_000_000, "discount_rate": 0.10},
            "message": "Investor counter.",
        },
        constraints={"mode": "two_party", "role": "investor"},
        dm_chat_id="123",
        group_chat_id="-100",
        sender=sender,
        editor=editor,
        delivery_client=client,
    )

    assert projected is True
    sender.assert_not_called()
    editor.assert_called_once()
    assert editor.call_args.args[0] == "-100"
    assert editor.call_args.kwargs["message_id"] == "88"
    assert "Offer 2" in editor.call_args.kwargs["message"]


def test_project_event_sends_new_card_when_heartbeat_edit_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAW_NEGOTIATE_DELIVERY_DIR", str(tmp_path))
    sender = MagicMock()
    editor = MagicMock(return_value=SimpleNamespace(ok=False))
    client = FakeDeliveryClient(
        created=True,
        deliveries={
            "turn_heartbeat:investor:1": {"message_id": "88", "target": "-100"},
        },
    )

    projected = projector.project_event(
        session_id="session_neg_1",
        event={
            "type": "counter",
            "party": "investor",
            "round": 1,
            "terms": {"valuation_cap": 30_000_000, "discount_rate": 0.10},
            "message": "Investor counter.",
        },
        constraints={"mode": "two_party", "role": "investor"},
        dm_chat_id="123",
        group_chat_id="-100",
        sender=sender,
        editor=editor,
        delivery_client=client,
    )

    assert projected is True
    editor.assert_called_once()
    sender.assert_called_once()
