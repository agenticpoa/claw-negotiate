"""Tests for typing_loop.

Mock everything touching the network. The loop runs in a real background
thread so we can verify its start/stop semantics, but the send function is
always injected.
"""
from __future__ import annotations

import json
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

import typing_loop as tl


# ─── get_bot_token ─────────────────────────────────────────────────


class TestGetBotToken:
    def setup_method(self):
        tl._reset_token_cache_for_tests()

    def teardown_method(self):
        tl._reset_token_cache_for_tests()

    def test_reads_token_from_config(self, tmp_path):
        config = tmp_path / "openclaw.json"
        config.write_text(json.dumps({
            "channels": {"telegram": {"botToken": "SECRET_TOKEN"}},
        }))
        assert tl.get_bot_token(config_path=config) == "SECRET_TOKEN"

    def test_caches_between_calls(self, tmp_path):
        config = tmp_path / "openclaw.json"
        config.write_text(json.dumps({
            "channels": {"telegram": {"botToken": "A"}},
        }))
        first = tl.get_bot_token(config_path=config)
        # Change on disk; cached result should not see it
        config.write_text(json.dumps({
            "channels": {"telegram": {"botToken": "B"}},
        }))
        second = tl.get_bot_token(config_path=config)
        assert first == second == "A"

    def test_force_reload_bypasses_cache(self, tmp_path):
        config = tmp_path / "openclaw.json"
        config.write_text(json.dumps({
            "channels": {"telegram": {"botToken": "A"}},
        }))
        tl.get_bot_token(config_path=config)
        config.write_text(json.dumps({
            "channels": {"telegram": {"botToken": "B"}},
        }))
        assert tl.get_bot_token(config_path=config, force_reload=True) == "B"

    def test_missing_file_returns_none(self, tmp_path):
        assert tl.get_bot_token(config_path=tmp_path / "nope.json") is None

    def test_malformed_json_returns_none(self, tmp_path):
        config = tmp_path / "openclaw.json"
        config.write_text("{not json")
        assert tl.get_bot_token(config_path=config) is None

    def test_missing_botToken_returns_none(self, tmp_path):
        config = tmp_path / "openclaw.json"
        config.write_text(json.dumps({"channels": {"telegram": {}}}))
        assert tl.get_bot_token(config_path=config) is None


# ─── send_typing_once ──────────────────────────────────────────────


class TestSendTypingOnce:
    def test_posts_correct_url_and_payload(self):
        captured = {}

        class FakeResp:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): pass

        def fake_opener(req, timeout):
            captured["url"] = req.full_url
            captured["body"] = req.data
            captured["method"] = req.get_method()
            return FakeResp()

        ok = tl.send_typing_once("12345", "MY_TOKEN", opener=fake_opener)
        assert ok is True
        assert captured["url"] == "https://api.telegram.org/botMY_TOKEN/sendChatAction"
        assert captured["method"] == "POST"
        payload = json.loads(captured["body"])
        assert payload == {"chat_id": "12345", "action": "typing"}

    def test_non_2xx_returns_false(self):
        class BadResp:
            status = 400
            def __enter__(self): return self
            def __exit__(self, *a): pass

        ok = tl.send_typing_once("1", "tok", opener=lambda req, timeout: BadResp())
        assert ok is False

    def test_exception_returns_false(self):
        def raiser(*args, **kwargs):
            raise ConnectionError("boom")
        ok = tl.send_typing_once("1", "tok", opener=raiser)
        assert ok is False


# ─── TypingLoop ────────────────────────────────────────────────────


class TestTypingLoop:
    def test_start_stop_clean(self):
        calls = []
        loop = tl.TypingLoop(
            chat_id="12345",
            bot_token="tok",
            interval=0.05,
            send_fn=lambda cid, tk: calls.append((cid, tk)) or True,
        )
        loop.start()
        # Allow enough time for at least 2 renewals.
        time.sleep(0.18)
        loop.stop()

        assert loop.is_active is False
        assert len(calls) >= 2
        assert all(c == ("12345", "tok") for c in calls)

    def test_stop_is_idempotent(self):
        loop = tl.TypingLoop(chat_id="1", bot_token="t", interval=0.05, send_fn=lambda *a: True)
        loop.start()
        loop.stop()
        loop.stop()  # must not raise

    def test_start_is_noop_when_already_active(self):
        loop = tl.TypingLoop(chat_id="1", bot_token="t", interval=0.05, send_fn=lambda *a: True)
        loop.start()
        first_thread = loop._thread
        loop.start()  # double-start
        assert loop._thread is first_thread
        loop.stop()

    def test_disabled_when_no_chat_id(self):
        calls = []
        loop = tl.TypingLoop(chat_id="", bot_token="t", interval=0.05,
                             send_fn=lambda *a: calls.append(a) or True)
        loop.start()
        time.sleep(0.1)
        loop.stop()
        assert loop.is_active is False
        assert calls == []

    def test_disabled_when_no_bot_token(self):
        calls = []
        loop = tl.TypingLoop(chat_id="1", bot_token=None, interval=0.05,
                             send_fn=lambda *a: calls.append(a) or True)
        loop.start()
        time.sleep(0.1)
        loop.stop()
        assert calls == []

    def test_stops_quickly_when_stopped_mid_wait(self):
        """A long interval must not delay stop() — _stop_event.wait handles it."""
        loop = tl.TypingLoop(chat_id="1", bot_token="t", interval=10.0,
                             send_fn=lambda *a: True)
        loop.start()
        time.sleep(0.05)  # let the first send fire
        t0 = time.time()
        loop.stop()
        # Should be quick — well under the 10s interval
        assert time.time() - t0 < 0.5

    def test_context_manager(self):
        calls = []
        with tl.TypingLoop(chat_id="1", bot_token="t", interval=0.05,
                           send_fn=lambda *a: calls.append(a) or True):
            time.sleep(0.1)
        assert len(calls) >= 1

    def test_send_failures_do_not_stop_loop(self):
        """A failing Telegram API must not break the renewal loop."""
        count = 0
        def flaky(*a):
            nonlocal count
            count += 1
            return count != 2  # fail on 2nd call only

        loop = tl.TypingLoop(chat_id="1", bot_token="t", interval=0.03, send_fn=flaky)
        loop.start()
        time.sleep(0.15)
        loop.stop()
        assert count >= 3  # kept going after the failure

    def test_chat_id_coerced_to_str(self):
        calls = []
        loop = tl.TypingLoop(chat_id=12345, bot_token="t", interval=0.05,
                             send_fn=lambda cid, tk: calls.append(cid) or True)
        loop.start()
        time.sleep(0.1)
        loop.stop()
        assert calls and all(isinstance(c, str) for c in calls)
