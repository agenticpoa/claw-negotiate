"""Tests for sshsign_session.py — the SSH client for signing_sessions."""
from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock

import pytest

import sshsign_session as ss


def _cp(returncode: int, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr,
    )


class TestCreateSession:
    def test_builds_correct_argv(self):
        runner = MagicMock(return_value=_cp(0, json.dumps({
            "session_id": "neg_1", "session_code": "INV-7K3X9", "status": "open",
        })))
        client = ss.SshsignSession(host="sshsign.dev", runner=runner)

        result = client.create_session(
            session_id="neg_1",
            role="founder",
            apoa_pubkey_pem="PEM",
            party_did="did:apoa:juan",
            metadata_public={"use_case": "safe"},
            metadata_member={"company_name": "Acme"},
            ttl_seconds=86400,
        )

        assert result["session_code"] == "INV-7K3X9"
        argv = runner.call_args[0][0]
        assert argv[:3] == ["ssh", "sshsign.dev", "create-session"]
        assert "--session-id" in argv
        assert argv[argv.index("--session-id") + 1] == "neg_1"
        assert "--role" in argv
        assert argv[argv.index("--role") + 1] == "founder"
        assert "--apoa-pubkey" in argv
        assert argv[argv.index("--apoa-pubkey") + 1] == "PEM"
        assert "--party-did" in argv
        # P8-2: metadata goes over the wire base64-encoded so string
        # values survive SSH argv (no inner-quote stripping, no
        # fixBareJSONKeys repair needed).
        import base64
        assert "--metadata-public-b64" in argv
        encoded = argv[argv.index("--metadata-public-b64") + 1]
        mp = json.loads(base64.urlsafe_b64decode(encoded))
        assert mp == {"use_case": "safe"}
        assert "--metadata-member-b64" in argv
        encoded = argv[argv.index("--metadata-member-b64") + 1]
        mm = json.loads(base64.urlsafe_b64decode(encoded))
        assert mm == {"company_name": "Acme"}
        # The plain flags must NOT be sent (conflict check server-side).
        assert "--metadata-public" not in argv
        assert "--metadata-member" not in argv
        assert argv[argv.index("--ttl") + 1] == "86400"

    def test_omits_optional_flags_when_not_given(self):
        runner = MagicMock(return_value=_cp(0, json.dumps({
            "session_code": "INV-X", "status": "open",
        })))
        client = ss.SshsignSession(runner=runner)
        client.create_session(session_id="n", role="founder", apoa_pubkey_pem="K")
        argv = runner.call_args[0][0]
        assert "--party-did" not in argv
        assert "--metadata-public" not in argv
        assert "--metadata-public-b64" not in argv
        assert "--metadata-member" not in argv
        assert "--metadata-member-b64" not in argv
        assert "--ttl" not in argv


class TestJoinSession:
    def test_builds_correct_argv(self):
        runner = MagicMock(return_value=_cp(0, json.dumps({
            "session_code": "INV-7K3X9", "status": "joined",
        })))
        client = ss.SshsignSession(runner=runner)
        client.join_session(
            session_code="INV-7K3X9", role="investor", apoa_pubkey_pem="PEM",
            party_did="did:apoa:bob",
        )
        argv = runner.call_args[0][0]
        assert argv[:3] == ["ssh", "sshsign.dev", "join-session"]
        assert argv[argv.index("--session-code") + 1] == "INV-7K3X9"


class TestGetSession:
    def test_by_code(self):
        runner = MagicMock(return_value=_cp(0, json.dumps({
            "session_code": "INV-X", "status": "open",
        })))
        client = ss.SshsignSession(runner=runner)
        client.get_session(session_code="INV-X")
        argv = runner.call_args[0][0]
        assert "--session-code" in argv

    def test_by_id(self):
        runner = MagicMock(return_value=_cp(0, json.dumps({
            "session_id": "neg_x", "status": "open",
        })))
        client = ss.SshsignSession(runner=runner)
        client.get_session(session_id="neg_x")
        argv = runner.call_args[0][0]
        assert "--session-id" in argv

    def test_requires_one_of(self):
        client = ss.SshsignSession(runner=MagicMock())
        with pytest.raises(ValueError):
            client.get_session()


class TestCancelAndComplete:
    def test_cancel_plain(self):
        runner = MagicMock(return_value=_cp(0, json.dumps({"status": "canceled"})))
        client = ss.SshsignSession(runner=runner)
        client.cancel_session("neg_1")
        argv = runner.call_args[0][0]
        assert argv[:3] == ["ssh", "sshsign.dev", "cancel-session"]
        assert "--rescind" not in argv

    def test_cancel_with_rescind(self):
        runner = MagicMock(return_value=_cp(0, json.dumps({"status": "rescinded_after_sign"})))
        client = ss.SshsignSession(runner=runner)
        client.cancel_session("neg_1", rescind=True)
        assert "--rescind" in runner.call_args[0][0]

    def test_complete(self):
        runner = MagicMock(return_value=_cp(0, json.dumps({
            "status": "completed", "view_token": "v_x",
        })))
        client = ss.SshsignSession(runner=runner)
        result = client.complete_session("neg_1", "sshsign://artifact/final.pdf")
        assert result["view_token"] == "v_x"
        argv = runner.call_args[0][0]
        assert argv[argv.index("--executed-artifact") + 1] == "sshsign://artifact/final.pdf"


class TestErrorMapping:
    def test_not_found(self):
        runner = MagicMock(return_value=_cp(0, json.dumps({"error": "session not found"})))
        client = ss.SshsignSession(runner=runner)
        with pytest.raises(ss.SessionNotFoundError):
            client.get_session(session_code="INV-NOPE")

    def test_role_conflict(self):
        runner = MagicMock(return_value=_cp(0, json.dumps({
            "error": "session already has a member in that role: founder",
        })))
        client = ss.SshsignSession(runner=runner)
        with pytest.raises(ss.SessionRoleConflictError):
            client.join_session(session_code="INV-X", role="founder", apoa_pubkey_pem="K")

    def test_terminal(self):
        runner = MagicMock(return_value=_cp(0, json.dumps({"error": "session is in a terminal state"})))
        client = ss.SshsignSession(runner=runner)
        with pytest.raises(ss.SessionTerminalError):
            client.cancel_session("neg_1")

    def test_not_creator(self):
        runner = MagicMock(return_value=_cp(0, json.dumps({
            "error": "only the session creator may perform this action",
        })))
        client = ss.SshsignSession(runner=runner)
        with pytest.raises(ss.SessionNotCreatorError):
            client.complete_session("neg_1", "artifact://x")

    def test_not_member(self):
        runner = MagicMock(return_value=_cp(0, json.dumps({"error": "not a member of this session"})))
        client = ss.SshsignSession(runner=runner)
        with pytest.raises(ss.SessionNotMemberError):
            client.audit_session("neg_1")

    def test_expired(self):
        runner = MagicMock(return_value=_cp(0, json.dumps({"error": "session has expired"})))
        client = ss.SshsignSession(runner=runner)
        with pytest.raises(ss.SessionExpiredError):
            client.join_session(session_code="INV-X", role="investor", apoa_pubkey_pem="K")

    def test_generic_error(self):
        runner = MagicMock(return_value=_cp(0, json.dumps({"error": "some unknown thing"})))
        client = ss.SshsignSession(runner=runner)
        with pytest.raises(ss.SshsignSessionError):
            client.get_session(session_id="x")


class TestTransportErrors:
    def test_non_zero_exit(self):
        runner = MagicMock(return_value=_cp(255, "", "Permission denied"))
        client = ss.SshsignSession(runner=runner)
        with pytest.raises(ss.SshsignSessionError, match="ssh exited 255"):
            client.get_session(session_id="x")

    def test_timeout(self):
        def raiser(*a, **kw):
            raise subprocess.TimeoutExpired(cmd=["ssh"], timeout=20)
        client = ss.SshsignSession(runner=raiser)
        with pytest.raises(ss.SshsignSessionError, match="timed out"):
            client.get_session(session_id="x")

    def test_ssh_binary_missing(self):
        def raiser(*a, **kw):
            raise FileNotFoundError("no ssh binary")
        client = ss.SshsignSession(runner=raiser)
        with pytest.raises(ss.SshsignSessionError, match="ssh not found"):
            client.get_session(session_id="x")

    def test_empty_response(self):
        runner = MagicMock(return_value=_cp(0, "  \n  "))
        client = ss.SshsignSession(runner=runner)
        with pytest.raises(ss.SshsignSessionError, match="empty"):
            client.get_session(session_id="x")

    def test_non_json_response(self):
        runner = MagicMock(return_value=_cp(0, "this is not json"))
        client = ss.SshsignSession(runner=runner)
        with pytest.raises(ss.SshsignSessionError, match="non-JSON"):
            client.get_session(session_id="x")


class TestBindGroup:
    def test_builds_correct_argv(self):
        runner = MagicMock(return_value=_cp(0, json.dumps({
            "session_id": "neg_1", "status": "joined", "group_chat_id": -1001234567890,
        })))
        client = ss.SshsignSession(runner=runner)

        result = client.bind_group("neg_1", -1001234567890)

        assert result["group_chat_id"] == -1001234567890
        argv = runner.call_args[0][0]
        assert argv[:3] == ["ssh", "sshsign.dev", "bind-group"]
        assert argv[argv.index("--session-id") + 1] == "neg_1"
        assert argv[argv.index("--group-chat-id") + 1] == "-1001234567890"

    def test_coerces_int(self):
        runner = MagicMock(return_value=_cp(0, json.dumps({"status": "joined"})))
        client = ss.SshsignSession(runner=runner)
        # Passing a string-ish number should still work via int() coercion.
        client.bind_group("neg_1", int("-42"))
        argv = runner.call_args[0][0]
        assert argv[argv.index("--group-chat-id") + 1] == "-42"

    def test_raises_group_already_bound(self):
        runner = MagicMock(return_value=_cp(0, json.dumps({"error": "group_already_bound"})))
        client = ss.SshsignSession(runner=runner)
        with pytest.raises(ss.GroupAlreadyBoundError):
            client.bind_group("neg_1", -9999)

    def test_idempotent_same_value_returns_session(self):
        # Server returns the current session row (no error) on same-value rebind.
        runner = MagicMock(return_value=_cp(0, json.dumps({
            "session_id": "neg_1", "status": "joined", "group_chat_id": -1001,
        })))
        client = ss.SshsignSession(runner=runner)
        result = client.bind_group("neg_1", -1001)
        assert result["group_chat_id"] == -1001

    def test_not_member_raises_typed(self):
        runner = MagicMock(return_value=_cp(0, json.dumps({
            "error": "caller is not a member of this session",
        })))
        client = ss.SshsignSession(runner=runner)
        with pytest.raises(ss.SessionNotMemberError):
            client.bind_group("neg_1", -1001)


class TestAuditList:
    def test_returns_list(self):
        events = [
            {"event_type": "created", "actor_id": "alice"},
            {"event_type": "joined", "actor_id": "bob"},
        ]
        runner = MagicMock(return_value=_cp(0, json.dumps(events)))
        client = ss.SshsignSession(runner=runner)
        result = client.audit_session("neg_1")
        assert result == events

    def test_rejects_non_list_response(self):
        runner = MagicMock(return_value=_cp(0, json.dumps({"not": "a list"})))
        client = ss.SshsignSession(runner=runner)
        with pytest.raises(ss.SshsignSessionError, match="expected audit list"):
            client.audit_session("neg_1")


class TestUpdateSessionMember:
    def test_builds_correct_argv_for_resumed_at(self):
        runner = MagicMock(return_value=_cp(0, json.dumps({"ok": True})))
        client = ss.SshsignSession(runner=runner)
        client.update_session_member("neg_1", field="founder_resumed_at", value=1714000000)
        argv = runner.call_args[0][0]
        assert argv[:3] == ["ssh", "sshsign.dev", "update-session-member"]
        assert argv[argv.index("--session-id") + 1] == "neg_1"
        assert argv[argv.index("--field") + 1] == "founder_resumed_at"
        assert argv[argv.index("--value") + 1] == "1714000000"

    def test_builds_correct_argv_for_streaming_at(self):
        runner = MagicMock(return_value=_cp(0, json.dumps({"ok": True})))
        client = ss.SshsignSession(runner=runner)
        client.update_session_member("neg_1", field="founder_streaming_at", value=1714000001)
        argv = runner.call_args[0][0]
        assert argv[argv.index("--field") + 1] == "founder_streaming_at"

    def test_client_side_whitelist_rejects_unknown_field(self):
        runner = MagicMock()
        client = ss.SshsignSession(runner=runner)
        with pytest.raises(ss.SshsignSessionError, match="not writable"):
            client.update_session_member("neg_1", field="created_by", value=42)
        # Must short-circuit before calling ssh.
        runner.assert_not_called()

    def test_server_rejects_non_creator(self):
        runner = MagicMock(return_value=_cp(0, json.dumps({
            "error": "only the session creator can update members",
        })))
        client = ss.SshsignSession(runner=runner)
        with pytest.raises(ss.SessionNotCreatorError):
            client.update_session_member(
                "neg_1", field="founder_resumed_at", value=1,
            )

    def test_server_rejects_non_whitelisted(self):
        runner = MagicMock(return_value=_cp(0, json.dumps({
            "error": "field not writable via update-session-member",
        })))
        client = ss.SshsignSession(runner=runner)
        # Client whitelist bypassed here to exercise the server error path
        # (real server will reject even when the client lets it through).
        client._UPDATABLE_MEMBER_FIELDS = frozenset({"some_future_field"})
        with pytest.raises(ss.SshsignSessionError):
            client.update_session_member(
                "neg_1", field="some_future_field", value=1,
            )

    def test_coerces_int_value(self):
        runner = MagicMock(return_value=_cp(0, json.dumps({"ok": True})))
        client = ss.SshsignSession(runner=runner)
        client.update_session_member("neg_1", field="founder_resumed_at", value=17140000.7)
        argv = runner.call_args[0][0]
        # int() coercion drops the fractional part.
        assert argv[argv.index("--value") + 1] == "17140000"


class TestUpdateSessionMemberText:
    def test_builds_correct_argv_for_bot_handle(self):
        runner = MagicMock(return_value=_cp(0, json.dumps({"ok": True})))
        client = ss.SshsignSession(runner=runner)
        client.update_session_member_text(
            "neg_1", field="bot_handle", text_value="@alice_bot",
        )
        argv = runner.call_args[0][0]
        assert argv[:3] == ["ssh", "sshsign.dev", "update-session-member"]
        assert argv[argv.index("--field") + 1] == "bot_handle"
        assert argv[argv.index("--text-value") + 1] == "@alice_bot"
        # No --value (int) flag
        assert "--value" not in argv

    def test_client_side_whitelist_rejects_unknown_text_field(self):
        runner = MagicMock()
        client = ss.SshsignSession(runner=runner)
        with pytest.raises(ss.SshsignSessionError, match="not writable"):
            client.update_session_member_text("neg_1", field="role", text_value="founder")
        runner.assert_not_called()

    def test_empty_string_value_allowed(self):
        runner = MagicMock(return_value=_cp(0, json.dumps({"ok": True})))
        client = ss.SshsignSession(runner=runner)
        client.update_session_member_text("neg_1", field="bot_handle", text_value="")
        argv = runner.call_args[0][0]
        assert argv[argv.index("--text-value") + 1] == ""

    def test_server_rejects_non_member(self):
        runner = MagicMock(return_value=_cp(0, json.dumps({
            "error": "caller is not a member of this session",
        })))
        client = ss.SshsignSession(runner=runner)
        with pytest.raises(ss.SessionNotMemberError):
            client.update_session_member_text(
                "neg_1", field="bot_handle", text_value="@bot",
            )
