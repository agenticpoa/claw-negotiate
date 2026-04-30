"""Tests for Telegram routing policy."""
from __future__ import annotations

from io import StringIO
from unittest.mock import MagicMock

import pytest

import telegram as tg
from telegram_push import SigningUrlTargetError


class TestStreamTarget:
    def test_group_wins_when_present(self):
        assert tg.stream_target("123", "-100") == "-100"

    def test_dm_when_no_group(self):
        assert tg.stream_target("123", None) == "123"


class TestShouldPublish:
    def test_two_party_filters_other_role_rounds(self):
        assert tg.should_publish_stream_event(
            {"type": "offer", "party": "founder"},
            {"mode": "two_party", "role": "investor"},
            "-100",
        ) is False

    def test_two_party_keeps_own_role_rounds(self):
        assert tg.should_publish_stream_event(
            {"type": "counter", "party": "investor"},
            {"mode": "two_party", "role": "investor"},
            "-100",
        ) is True

    def test_demo_does_not_filter_rounds(self):
        assert tg.should_publish_stream_event(
            {"type": "offer", "party": "founder"},
            {"mode": "demo", "role": "investor"},
            "-100",
        ) is True

    def test_non_round_events_are_not_filtered(self):
        assert tg.should_publish_stream_event(
            {"type": "signing", "party": "founder"},
            {"mode": "two_party", "role": "investor"},
            "-100",
        ) is True


class TestRouteStreamMessage:
    def test_routes_round_to_group(self):
        sender = MagicMock()
        dm_sender = MagicMock()
        tg.route_stream_message(
            event={"type": "offer", "party": "founder"},
            message="round",
            chat_id="123",
            group_chat_id="-100",
            constraints={"mode": "two_party", "role": "founder"},
            sender=sender,
            dm_sender=dm_sender,
        )
        sender.assert_called_once_with("-100", message="round")
        dm_sender.assert_not_called()

    def test_suppresses_other_role_round(self):
        sender = MagicMock()
        tg.route_stream_message(
            event={"type": "offer", "party": "founder"},
            message="round",
            chat_id="123",
            group_chat_id="-100",
            constraints={"mode": "two_party", "role": "investor"},
            sender=sender,
            dm_sender=MagicMock(),
        )
        sender.assert_not_called()

    def test_signing_url_goes_to_dm_and_placeholder_to_group(self):
        sender = MagicMock()
        dm_sender = MagicMock()
        tg.route_stream_message(
            event={"type": "signing"},
            message="https://sshsign.dev/approve/pnd_1",
            chat_id="123",
            group_chat_id="-100",
            constraints=None,
            sender=sender,
            dm_sender=dm_sender,
        )
        dm_sender.assert_called_once_with(
            "123", message="https://sshsign.dev/approve/pnd_1",
        )
        sender.assert_called_once_with("-100", message=tg.SIGNING_GROUP_PLACEHOLDER)

    def test_signing_dm_rejection_logs_and_still_posts_placeholder(self):
        sender = MagicMock()
        stderr = StringIO()

        def reject(*args, **kwargs):
            raise SigningUrlTargetError("nope")

        tg.route_stream_message(
            event={"type": "signing"},
            message="url",
            chat_id="-1",
            group_chat_id="-100",
            constraints=None,
            sender=sender,
            dm_sender=reject,
            stderr=stderr,
        )
        assert "refusing to send signing URL" in stderr.getvalue()
        sender.assert_called_once_with("-100", message=tg.SIGNING_GROUP_PLACEHOLDER)

    def test_signing_placeholder_can_be_suppressed(self):
        sender = MagicMock()
        dm_sender = MagicMock()
        tg.route_stream_message(
            event={"type": "signing", "_suppress_group_placeholder": True},
            message="https://sshsign.dev/approve/pnd_1",
            chat_id="123",
            group_chat_id="-100",
            constraints=None,
            sender=sender,
            dm_sender=dm_sender,
        )
        dm_sender.assert_called_once()
        sender.assert_not_called()

    def test_empty_message_noops(self):
        sender = MagicMock()
        tg.route_stream_message(
            event={"type": "offer"},
            message="",
            chat_id="123",
            group_chat_id=None,
            constraints=None,
            sender=sender,
            dm_sender=MagicMock(),
        )
        sender.assert_not_called()
