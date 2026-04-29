"""Tests for telegram_push — chat_id resolution and openclaw CLI invocation."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import telegram_push as tp


# ---- resolve_chat_id ----


class TestResolveChatId:
    def test_flag_takes_priority(self, tmp_path):
        sessions = tmp_path / "sessions.json"
        sessions.write_text(json.dumps({
            "agent:main:telegram:direct:999": {"updatedAt": 1_000_000},
        }))
        assert tp.resolve_chat_id("12345", sessions_path=sessions) == "12345"

    def test_flag_stripped_and_coerced_to_str(self, tmp_path):
        assert tp.resolve_chat_id("  6413315062  ", sessions_path=tmp_path / "missing.json") == "6413315062"

    def test_empty_flag_falls_through_to_fallback(self, tmp_path):
        sessions = tmp_path / "sessions.json"
        sessions.write_text(json.dumps({
            "agent:main:telegram:direct:777": {"updatedAt": 1_000_000},
        }))
        assert tp.resolve_chat_id("", sessions_path=sessions) == "777"

    def test_none_flag_falls_through_to_fallback(self, tmp_path):
        sessions = tmp_path / "sessions.json"
        sessions.write_text(json.dumps({
            "agent:main:telegram:direct:8888": {"updatedAt": 42},
        }))
        assert tp.resolve_chat_id(None, sessions_path=sessions) == "8888"

    def test_picks_most_recently_updated_direct_session(self, tmp_path):
        sessions = tmp_path / "sessions.json"
        sessions.write_text(json.dumps({
            "agent:main:telegram:direct:111": {"updatedAt": 100},
            "agent:main:telegram:direct:222": {"updatedAt": 500},
            "agent:main:telegram:direct:333": {"updatedAt": 250},
        }))
        assert tp.resolve_chat_id(None, sessions_path=sessions) == "222"

    def test_ignores_slash_and_non_direct_entries(self, tmp_path):
        sessions = tmp_path / "sessions.json"
        sessions.write_text(json.dumps({
            "agent:main:telegram:slash:111": {"updatedAt": 9_999_999},
            "agent:main:slack:direct:222": {"updatedAt": 8_888_888},
            "agent:main:telegram:direct:333": {"updatedAt": 1},
        }))
        assert tp.resolve_chat_id(None, sessions_path=sessions) == "333"

    def test_missing_sessions_file_returns_none(self, tmp_path):
        assert tp.resolve_chat_id(None, sessions_path=tmp_path / "nope.json") is None

    def test_malformed_sessions_json_returns_none(self, tmp_path):
        sessions = tmp_path / "sessions.json"
        sessions.write_text("{not valid json")
        assert tp.resolve_chat_id(None, sessions_path=sessions) is None

    def test_non_object_root_returns_none(self, tmp_path):
        sessions = tmp_path / "sessions.json"
        sessions.write_text("[]")
        assert tp.resolve_chat_id(None, sessions_path=sessions) is None

    def test_no_direct_entries_returns_none(self, tmp_path):
        sessions = tmp_path / "sessions.json"
        sessions.write_text(json.dumps({"agent:main:telegram:slash:111": {"updatedAt": 1}}))
        assert tp.resolve_chat_id(None, sessions_path=sessions) is None

    def test_entry_missing_updatedAt_is_skipped(self, tmp_path):
        sessions = tmp_path / "sessions.json"
        sessions.write_text(json.dumps({
            "agent:main:telegram:direct:111": {},
            "agent:main:telegram:direct:222": {"updatedAt": 99},
        }))
        assert tp.resolve_chat_id(None, sessions_path=sessions) == "222"


# ---- send_telegram ----


def _ok_stdout(message_id: int = 195) -> str:
    return json.dumps({
        "action": "send",
        "channel": "telegram",
        "payload": {"ok": True, "messageId": str(message_id), "chatId": "6413315062"},
    })


def _ok_result(stdout: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


class _FakeHttpResponse:
    status = 200

    def __init__(self, body: dict):
        self._body = json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None


class TestSendTelegram:
    def test_requires_message_or_media(self):
        with pytest.raises(ValueError):
            tp.send_telegram("123")

    def test_bot_token_prefers_direct_bot_api(self, monkeypatch):
        runner = MagicMock()
        monkeypatch.setattr(tp, "get_bot_token", lambda: "TOKEN")

        def opener(req, timeout):
            assert req.full_url == "https://api.telegram.org/botTOKEN/sendMessage"
            assert json.loads(req.data.decode()) == {"chat_id": "12345", "text": "hello"}
            return _FakeHttpResponse({"ok": True, "result": {"message_id": 77}})

        result = tp.send_telegram("12345", message="hello", runner=runner, opener=opener)

        assert result.ok is True
        assert result.message_id == "77"
        runner.assert_not_called()

    def test_bot_api_sends_reply_markup(self, monkeypatch):
        runner = MagicMock()
        monkeypatch.setattr(tp, "get_bot_token", lambda: "TOKEN")
        markup = {
            "inline_keyboard": [[
                {"text": "Copy bind command", "copy_text": {"text": "/bind INV-1"}},
            ]],
        }

        def opener(req, timeout):
            assert req.full_url == "https://api.telegram.org/botTOKEN/sendMessage"
            assert json.loads(req.data.decode()) == {
                "chat_id": "12345",
                "text": "hello",
                "reply_markup": markup,
            }
            return _FakeHttpResponse({"ok": True, "result": {"message_id": 77}})

        result = tp.send_telegram(
            "12345",
            message="hello",
            reply_markup=markup,
            runner=runner,
            opener=opener,
        )

        assert result.ok is True
        assert result.message_id == "77"
        runner.assert_not_called()

    def test_text_only_builds_correct_cmd(self):
        runner = MagicMock(return_value=_ok_result(_ok_stdout(195)))
        result = tp.send_telegram("12345", message="hello", runner=runner)

        assert result.ok is True
        assert result.message_id == "195"
        assert result.error is None

        cmd = runner.call_args[0][0]
        assert cmd[:3] == ["openclaw", "message", "send"]
        assert "--channel" in cmd and cmd[cmd.index("--channel") + 1] == "telegram"
        assert "--target" in cmd and cmd[cmd.index("--target") + 1] == "12345"
        assert "--message" in cmd and cmd[cmd.index("--message") + 1] == "hello"
        assert "--json" in cmd
        assert "--media" not in cmd
        assert "--force-document" not in cmd

    def test_group_target_prefix_is_normalized(self):
        runner = MagicMock(return_value=_ok_result(_ok_stdout(195)))
        tp.send_telegram("group:-5261090007", message="hello", runner=runner)

        cmd = runner.call_args[0][0]
        assert cmd[cmd.index("--target") + 1] == "-5261090007"

    def test_telegram_target_prefix_is_normalized(self):
        runner = MagicMock(return_value=_ok_result(_ok_stdout(195)))
        tp.send_telegram("telegram:6413315062", message="hello", runner=runner)

        cmd = runner.call_args[0][0]
        assert cmd[cmd.index("--target") + 1] == "6413315062"

    def test_media_only_builds_correct_cmd(self, tmp_path):
        pdf = tmp_path / "doc.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        runner = MagicMock(return_value=_ok_result(_ok_stdout(196)))

        result = tp.send_telegram("12345", media_path=pdf, runner=runner)

        assert result.ok is True
        assert result.message_id == "196"
        cmd = runner.call_args[0][0]
        assert "--media" in cmd and cmd[cmd.index("--media") + 1] == str(pdf)
        assert "--message" not in cmd

    def test_message_and_media_together(self, tmp_path):
        pdf = tmp_path / "doc.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        runner = MagicMock(return_value=_ok_result(_ok_stdout(197)))

        tp.send_telegram("12345", message="here is the file", media_path=pdf, runner=runner)

        cmd = runner.call_args[0][0]
        assert "--message" in cmd
        assert "--media" in cmd

    def test_force_document_flag(self):
        runner = MagicMock(return_value=_ok_result(_ok_stdout()))
        tp.send_telegram("12345", message="x", force_document=True, runner=runner)
        cmd = runner.call_args[0][0]
        assert "--force-document" in cmd

    def test_custom_openclaw_bin(self):
        runner = MagicMock(return_value=_ok_result(_ok_stdout()))
        tp.send_telegram("12345", message="x", openclaw_bin="/usr/local/bin/openclaw", runner=runner)
        cmd = runner.call_args[0][0]
        assert cmd[0] == "/usr/local/bin/openclaw"

    def test_non_zero_exit_returns_error(self):
        runner = MagicMock(return_value=subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="boom",
        ))
        result = tp.send_telegram("12345", message="x", runner=runner)
        assert result.ok is False
        assert result.message_id is None
        assert "boom" in result.error

    def test_non_zero_exit_falls_back_to_bot_api(self, monkeypatch):
        runner = MagicMock(return_value=subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="pairing required",
        ))
        monkeypatch.setattr(tp, "get_bot_token", lambda: "TOKEN")

        def opener(req, timeout):
            assert req.full_url == "https://api.telegram.org/botTOKEN/sendMessage"
            assert json.loads(req.data.decode()) == {"chat_id": "-5261090007", "text": "x"}
            return _FakeHttpResponse({"ok": True, "result": {"message_id": 77}})

        result = tp.send_telegram("group:-5261090007", message="x", runner=runner, opener=opener)
        assert result.ok is True
        assert result.message_id == "77"

    def test_non_zero_exit_falls_back_to_send_document(self, tmp_path, monkeypatch):
        runner = MagicMock(return_value=subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="pairing required",
        ))
        monkeypatch.setattr(tp, "get_bot_token", lambda: "TOKEN")
        pdf = tmp_path / "safe.pdf"
        pdf.write_bytes(b"%PDF")

        def opener(req, timeout):
            assert req.full_url == "https://api.telegram.org/botTOKEN/sendDocument"
            assert b'name="chat_id"\r\n\r\n12345' in req.data
            assert b'name="caption"\r\n\r\nexecuted' in req.data
            assert b'name="document"; filename="safe.pdf"' in req.data
            assert b"%PDF" in req.data
            return _FakeHttpResponse({"ok": True, "result": {"message_id": 78}})

        result = tp.send_telegram("12345", message="executed", media_path=pdf, runner=runner, opener=opener)
        assert result.ok is True
        assert result.message_id == "78"

    def test_non_json_stdout_returns_error(self):
        runner = MagicMock(return_value=_ok_result("not json"))
        result = tp.send_telegram("12345", message="x", runner=runner)
        assert result.ok is False
        assert "non-JSON" in result.error

    def test_payload_ok_false_returns_error(self):
        body = json.dumps({"payload": {"ok": False, "error": "chat not found"}})
        runner = MagicMock(return_value=_ok_result(body))
        result = tp.send_telegram("12345", message="x", runner=runner)
        assert result.ok is False
        assert "chat not found" in result.error

    def test_timeout_returns_error(self):
        def raiser(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="openclaw", timeout=30)
        result = tp.send_telegram("12345", message="x", runner=raiser)
        assert result.ok is False
        assert "timeout" in result.error

    def test_openclaw_binary_missing_returns_error(self):
        def raiser(*args, **kwargs):
            raise FileNotFoundError("no such file")
        result = tp.send_telegram("12345", message="x", runner=raiser)
        assert result.ok is False
        assert "not found on PATH" in result.error

    def test_message_id_missing_still_ok(self):
        body = json.dumps({"payload": {"ok": True, "chatId": "123"}})
        runner = MagicMock(return_value=_ok_result(body))
        result = tp.send_telegram("12345", message="x", runner=runner)
        assert result.ok is True
        assert result.message_id is None


# ---- send_signing_url_to_dm ----


class TestSendSigningUrlToDm:
    """Structural privacy: the signing URL primitive rejects non-DM
    targets BEFORE any subprocess call. Replaces the runtime
    substring-guard design from the earlier K4 plan."""

    def test_happy_path_positive_int_delivers(self):
        runner = MagicMock(return_value=_ok_result(_ok_stdout(42)))
        result = tp.send_signing_url_to_dm(
            123456789,
            message="Sign here: https://sshsign.dev/approve/pnd_X",
            runner=runner,
        )
        assert result.ok is True
        cmd = runner.call_args[0][0]
        assert cmd[cmd.index("--target") + 1] == "123456789"
        assert cmd[cmd.index("--message") + 1].startswith("Sign here:")

    def test_string_positive_int_accepted(self):
        runner = MagicMock(return_value=_ok_result(_ok_stdout(42)))
        result = tp.send_signing_url_to_dm("123", message="x", runner=runner)
        assert result.ok is True

    def test_negative_target_raises_before_send(self):
        runner = MagicMock()
        with pytest.raises(tp.SigningUrlTargetError, match="positive"):
            tp.send_signing_url_to_dm(-1001234567890, message="x", runner=runner)
        runner.assert_not_called()

    def test_group_prefixed_target_raises_before_send(self):
        runner = MagicMock()
        with pytest.raises(tp.SigningUrlTargetError, match="integer"):
            tp.send_signing_url_to_dm("group:-5261090007", message="x", runner=runner)
        runner.assert_not_called()

    def test_zero_target_raises(self):
        runner = MagicMock()
        with pytest.raises(tp.SigningUrlTargetError, match="positive"):
            tp.send_signing_url_to_dm(0, message="x", runner=runner)
        runner.assert_not_called()

    def test_non_integer_target_raises(self):
        runner = MagicMock()
        with pytest.raises(tp.SigningUrlTargetError, match="integer"):
            tp.send_signing_url_to_dm("not-a-number", message="x", runner=runner)
        runner.assert_not_called()

    def test_none_target_raises(self):
        runner = MagicMock()
        with pytest.raises(tp.SigningUrlTargetError):
            tp.send_signing_url_to_dm(None, message="x", runner=runner)
        runner.assert_not_called()

    def test_whitespace_stripped(self):
        runner = MagicMock(return_value=_ok_result(_ok_stdout(42)))
        result = tp.send_signing_url_to_dm("  123456  ", message="x", runner=runner)
        assert result.ok is True
