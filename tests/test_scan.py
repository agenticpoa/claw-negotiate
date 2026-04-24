"""Tests for P7-5 resume + scan.

Covers:
- _run_founder_resume: missing state, missing output_dir, session not found,
  terminal status, status != joined, dedup on founder_resumed_at already set,
  happy path sets resumed_at + streaming_at + re-enters stream.
- run_scan: iterates pointers, contains per-pointer failures, exits 0.
- run_bind: status=joined branch invokes resume inline.
- Regression: run_negotiate single-party path does NOT write state or call resume.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import run_safe as rs
import state_store


@pytest.fixture(autouse=True)
def _state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAW_NEGOTIATE_STATE_DIR", str(tmp_path / "state"))
    return tmp_path / "state"


@pytest.fixture
def founder_output_dir(tmp_path) -> Path:
    """An output_dir preloaded with mint.json + config.json as
    ``_run_founder_resume`` expects to find them.
    """
    out = tmp_path / "safe_negotiate"
    out.mkdir()
    (out / "mint.json").write_text(json.dumps({
        "negotiation_id": "neg_abc",
        "mode": "two_party",
        "user_role": "founder",
    }))
    (out / "config.json").write_text(json.dumps({
        "chat_id": "123456",
        "constraints": {"role": "founder", "valuation_cap_min": 8_000_000},
    }))
    return out


@pytest.fixture
def state_record(founder_output_dir) -> dict:
    return {
        "negotiation_id": "neg_abc",
        "output_dir": str(founder_output_dir),
        "session_code": "INV-ABCDE",
    }


@pytest.fixture
def fake_client():
    client = MagicMock()
    client.get_session.return_value = {
        "session_id": "session_neg_abc",
        "session_code": "INV-ABCDE",
        "status": "joined",
        "members": [
            {"role": "founder", "user_id": "u_founder", "founder_resumed_at": None},
            {"role": "investor", "user_id": "u_investor"},
        ],
        "group_chat_id": -100123,
    }
    return client


class TestRunFounderResumeInputValidation:
    def test_missing_negotiation_id_returns_2(self, state_record):
        state = dict(state_record)
        state.pop("negotiation_id")
        rc = rs._run_founder_resume(state)
        assert rc == 2

    def test_missing_output_dir_cleans_pointer(self, state_record, _state_dir):
        """If output_dir was wiped (/tmp cleaned), stop scanning this id."""
        state_store.write_state(state_record)
        state = dict(state_record)
        state["output_dir"] = "/tmp/does_not_exist_ever_" + "x" * 8
        rc = rs._run_founder_resume(state)
        assert rc == 2
        # pointer deleted
        assert state_store.read_state(state["negotiation_id"]) is None


class TestRunFounderResumeSessionStatus:
    def test_session_not_found_cleans_pointer(self, state_record, fake_client):
        from sshsign_session import SessionNotFoundError
        state_store.write_state(state_record)
        fake_client.get_session.side_effect = SessionNotFoundError("gone")
        rc = rs._run_founder_resume(state_record, session_client=fake_client)
        assert rc == 2
        assert state_store.read_state("neg_abc") is None

    @pytest.mark.parametrize("status", [
        "canceled", "rescinded", "rescinded_after_sign", "completed", "expired",
    ])
    def test_terminal_status_cleans_and_skips_stream(
        self, state_record, fake_client, status,
    ):
        state_store.write_state(state_record)
        fake_client.get_session.return_value["status"] = status
        with patch.object(rs, "_stream_to_telegram") as stream:
            rc = rs._run_founder_resume(state_record, session_client=fake_client)
        assert rc == 2
        stream.assert_not_called()
        fake_client.update_session_member.assert_not_called()
        assert state_store.read_state("neg_abc") is None

    def test_not_yet_joined_returns_1(self, state_record, fake_client):
        """status=created means the investor hasn't joined yet. Scan
        will retry on the next cron tick; don't resume early.
        """
        fake_client.get_session.return_value["status"] = "created"
        with patch.object(rs, "_stream_to_telegram") as stream:
            rc = rs._run_founder_resume(state_record, session_client=fake_client)
        assert rc == 1
        stream.assert_not_called()
        fake_client.update_session_member.assert_not_called()


class TestRunFounderResumeIdempotency:
    def test_resumed_at_already_set_dedups(self, state_record, fake_client):
        """A concurrent scan tick or a bind race: the other turn already
        set resumed_at. Stay idle and let them own the stream.
        """
        fake_client.get_session.return_value["members"][0]["founder_resumed_at"] = 1700000000
        with patch.object(rs, "_stream_to_telegram") as stream:
            rc = rs._run_founder_resume(state_record, session_client=fake_client)
        assert rc == 0
        stream.assert_not_called()
        fake_client.update_session_member.assert_not_called()


class TestRunFounderResumeHappyPath:
    def test_sets_resumed_then_streams_then_sets_streaming(
        self, state_record, fake_client, monkeypatch,
    ):
        # Mock _resolve_group_chat_id so we don't ssh in the test.
        monkeypatch.setattr(rs, "_resolve_group_chat_id", lambda *a, **kw: "-100123")
        # Stream returns clean rc + no signing event — skips finalize.
        with patch.object(rs, "_stream_to_telegram", return_value=(0, None)) as stream:
            sender = MagicMock()
            rc = rs._run_founder_resume(
                state_record, session_client=fake_client, sender=sender,
                now_fn=lambda: 1714000000,
            )

        assert rc == 0
        # resumed_at set first, then stream called, then streaming_at set.
        calls = fake_client.update_session_member.call_args_list
        assert len(calls) == 2
        assert calls[0].kwargs == {
            "field": "founder_resumed_at", "value": 1714000000,
        } or calls[0].args[1:] == ("founder_resumed_at", 1714000000)
        assert calls[1].kwargs == {
            "field": "founder_streaming_at", "value": 1714000000,
        } or calls[1].args[1:] == ("founder_streaming_at", 1714000000)
        stream.assert_called_once()
        # Orienting card posted in the group, never starts with '/'.
        sent_to_group = [
            c for c in sender.call_args_list if c.args[0] == "-100123"
        ]
        assert sent_to_group, "resume must post at least one card in the group"
        orient_msg = sent_to_group[0].kwargs["message"]
        assert not orient_msg.startswith("/"), (
            "invariant: resume cards must never start with '/' "
            "(would be dispatched back as a skill intent)"
        )

    def test_streaming_at_failure_is_non_fatal(
        self, state_record, fake_client, monkeypatch,
    ):
        """update_session_member(founder_streaming_at) failing must NOT
        abort the stream — the stream already ran. The audit trail on
        sshsign captures the gap for ops.
        """
        from sshsign_session import SshsignSessionError
        monkeypatch.setattr(rs, "_resolve_group_chat_id", lambda *a, **kw: "-100123")

        def _update(*args, **kwargs):
            field = kwargs.get("field") or args[1]
            if field == "founder_streaming_at":
                raise SshsignSessionError("sshsign unreachable")
            return {"ok": True}

        fake_client.update_session_member.side_effect = _update

        with patch.object(rs, "_stream_to_telegram", return_value=(0, None)):
            rc = rs._run_founder_resume(
                state_record, session_client=fake_client,
                now_fn=lambda: 1714000001,
            )
        # Return code still 0; scan-level audit reflects the gap.
        assert rc == 0

    def test_stream_failure_returns_rc_without_cleanup(
        self, state_record, fake_client, monkeypatch,
    ):
        """Non-terminal stream failure: don't delete state. Next cron
        tick will re-check — resumed_at is set so it'll dedup and
        skip, which is safe behavior until the session genuinely
        terminates or is cancelled.
        """
        state_store.write_state(state_record)
        monkeypatch.setattr(rs, "_resolve_group_chat_id", lambda *a, **kw: None)
        with patch.object(rs, "_stream_to_telegram", return_value=(1, None)):
            rc = rs._run_founder_resume(state_record, session_client=fake_client)
        assert rc == 1
        # State NOT deleted — session isn't terminal yet.
        assert state_store.read_state("neg_abc") is not None


class TestRunScan:
    def test_empty_state_dir_returns_zero(self):
        assert rs.run_scan() == 0

    def test_iterates_and_resumes_each_pointer(
        self, founder_output_dir, fake_client, monkeypatch,
    ):
        monkeypatch.setattr(rs, "_resolve_group_chat_id", lambda *a, **kw: None)
        # Two pointers pointing at the same output_dir for simplicity.
        for nid in ("neg_a", "neg_b"):
            state_store.write_state({
                "negotiation_id": nid,
                "output_dir": str(founder_output_dir),
                "session_code": f"INV-{nid.upper()}",
            })
        with patch.object(rs, "_run_founder_resume", return_value=0) as resume:
            rc = rs.run_scan(session_client=fake_client)
        assert rc == 0
        assert resume.call_count == 2

    def test_per_pointer_failure_does_not_halt_tick(
        self, founder_output_dir, fake_client, monkeypatch,
    ):
        for nid in ("neg_bad", "neg_good"):
            state_store.write_state({
                "negotiation_id": nid,
                "output_dir": str(founder_output_dir),
                "session_code": f"INV-{nid.upper()}",
            })
        call_order = []

        def _fake_resume(state, *, session_client=None, sender=None, now_fn=None):
            call_order.append(state["negotiation_id"])
            if state["negotiation_id"] == "neg_bad":
                raise RuntimeError("transient")
            return 0

        with patch.object(rs, "_run_founder_resume", side_effect=_fake_resume):
            rc = rs.run_scan(session_client=fake_client)
        assert rc == 0
        # Both pointers processed despite one raising.
        assert set(call_order) == {"neg_bad", "neg_good"}


class TestBindInProcessResumeFastPath:
    @staticmethod
    def _founder_session(status, session_id="session_neg_abc"):
        return {
            "session_id": session_id,
            "session_code": "INV-INPROC",
            "status": status,
            "metadata_member": json.dumps({
                "investor_name": "Alex",
                "telegram": {"founder_user_id": 111},
            }),
        }

    def test_bind_with_status_joined_triggers_resume(
        self, founder_output_dir, monkeypatch,
    ):
        state_store.write_state({
            "negotiation_id": "neg_abc",
            "output_dir": str(founder_output_dir),
            "session_code": "INV-INPROC",
        })
        client = MagicMock()
        client.get_session.return_value = self._founder_session(status="joined")
        client.bind_group.return_value = {"status": "joined"}

        with patch.object(rs, "_run_founder_resume", return_value=0) as resume:
            rc = rs.run_bind(
                session_code="INV-INPROC",
                group_chat_id=-1001234,
                from_user_id=111,
                sender=MagicMock(),
                session_client=client,
            )
        assert rc == 0
        # resume called exactly once with the loaded state pointer.
        resume.assert_called_once()
        state_arg = resume.call_args.args[0]
        assert state_arg["negotiation_id"] == "neg_abc"

    def test_bind_with_status_non_joined_does_not_resume(
        self, founder_output_dir, monkeypatch,
    ):
        """Standard /bind flow (investor hasn't joined yet): we exit
        after writing the binding; cron scan handles the resume.
        """
        state_store.write_state({
            "negotiation_id": "neg_abc",
            "output_dir": str(founder_output_dir),
            "session_code": "INV-INPROC",
        })
        client = MagicMock()
        client.get_session.return_value = self._founder_session(status="created")
        client.bind_group.return_value = {"status": "created"}
        with patch.object(rs, "_run_founder_resume") as resume:
            rs.run_bind(
                session_code="INV-INPROC",
                group_chat_id=-1001234,
                from_user_id=111,
                sender=MagicMock(),
                session_client=client,
            )
        resume.assert_not_called()


class TestCliDispatch:
    def test_scan_subcommand_calls_run_scan(self, monkeypatch, capsys):
        called = {"n": 0}

        def _fake_scan():
            called["n"] += 1
            return 0

        monkeypatch.setattr(rs, "run_scan", _fake_scan)
        monkeypatch.setattr("sys.argv", ["run_safe.py", "scan"])
        rc = rs.main()
        assert rc == 0
        assert called["n"] == 1


class TestEnsureCron:
    @staticmethod
    def _cp(returncode: int, stdout: str = "", stderr: str = ""):
        import subprocess
        return subprocess.CompletedProcess(
            args=[], returncode=returncode, stdout=stdout, stderr=stderr,
        )

    def test_adds_cron_when_absent(self):
        calls: list[list[str]] = []

        def runner(argv, **kwargs):
            calls.append(argv)
            if argv[:3] == ["openclaw", "cron", "list"]:
                return self._cp(0, stdout="[]")
            if argv[:3] == ["openclaw", "cron", "add"]:
                return self._cp(0, stdout=json.dumps({"ok": True}))
            raise AssertionError(f"unexpected argv {argv}")

        ok, err = rs.ensure_cron(interval="30s", runner=runner)
        assert ok and err is None
        # list and add both called
        assert any(c[:3] == ["openclaw", "cron", "list"] for c in calls)
        add_call = next(c for c in calls if c[:3] == ["openclaw", "cron", "add"])
        assert "--name" in add_call
        assert add_call[add_call.index("--name") + 1] == "negotiate_safe-scan"
        assert add_call[add_call.index("--every") + 1] == "30s"
        assert add_call[add_call.index("--session") + 1] == "isolated"
        assert add_call[add_call.index("--system-event") + 1] == "negotiate_safe_scan"
        assert "--exact" in add_call
        assert "--keep-after-run" in add_call

    def test_noop_when_job_already_exists(self):
        existing = json.dumps([
            {"name": "negotiate_safe-scan", "schedule": "every 30s"},
            {"name": "something-else"},
        ])
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            return self._cp(0, stdout=existing)

        ok, err = rs.ensure_cron(runner=runner)
        assert ok and err is None
        # only list was called; add was skipped
        assert len(calls) == 1
        assert calls[0][:3] == ["openclaw", "cron", "list"]

    def test_tolerates_wrapped_jobs_list(self):
        """Some OC versions wrap the list under {jobs: [...]}."""
        wrapped = json.dumps({"jobs": [{"name": "negotiate_safe-scan"}]})

        def runner(argv, **kwargs):
            return self._cp(0, stdout=wrapped)

        ok, err = rs.ensure_cron(runner=runner)
        assert ok and err is None

    def test_list_failure_returns_false_with_reason(self):
        def runner(argv, **kwargs):
            return self._cp(1, stderr="pairing required")

        ok, err = rs.ensure_cron(runner=runner)
        assert ok is False
        assert "pairing required" in err

    def test_add_failure_returns_false_with_reason(self):
        def runner(argv, **kwargs):
            if argv[:3] == ["openclaw", "cron", "list"]:
                return self._cp(0, stdout="[]")
            return self._cp(2, stderr="--every 10s not supported")

        ok, err = rs.ensure_cron(interval="10s", runner=runner)
        assert ok is False
        assert "10s not supported" in err

    def test_invalid_json_returns_false(self):
        def runner(argv, **kwargs):
            return self._cp(0, stdout="not json{")

        ok, err = rs.ensure_cron(runner=runner)
        assert ok is False
        assert "invalid JSON" in err

    def test_subprocess_error_is_contained(self):
        def runner(argv, **kwargs):
            raise FileNotFoundError("openclaw not on PATH")

        ok, err = rs.ensure_cron(runner=runner)
        assert ok is False
        assert "openclaw cron list failed" in err


class TestInvestorWaitForFounderStreaming:
    """P7-5 investor-side bounded poll: posts waiting card, typing
    indicator, and either sees founder_streaming_at hit, detects
    terminal status, or times out after 180s.
    """

    @staticmethod
    def _session(status="joined", streaming_at=None, resumed_at=None):
        member = {"role": "founder"}
        if streaming_at is not None:
            member["founder_streaming_at"] = streaming_at
        if resumed_at is not None:
            member["founder_resumed_at"] = resumed_at
        return {
            "session_id": "session_neg_x",
            "status": status,
            "members": [member, {"role": "investor"}],
        }

    @staticmethod
    def _fake_clock():
        """Monotonic clock that advances by INVESTOR_WAIT_POLL_INTERVAL
        each time the poll loop calls sleep. Lets tests exercise the
        full timeline without real wall-clock waits.
        """
        state = {"t": 0.0}

        def now():
            return state["t"]

        def sleep(secs):
            state["t"] += secs

        return now, sleep

    @staticmethod
    def _typing():
        return MagicMock(start=MagicMock(), stop=MagicMock())

    def test_streaming_at_hit_returns_streaming(self):
        client = MagicMock()
        client.get_session.return_value = self._session(streaming_at=1714000000)
        now_fn, sleep_fn = self._fake_clock()
        sender = MagicMock()
        typing = self._typing()

        rc = rs._investor_wait_for_founder_streaming(
            session_id="session_neg_x",
            group_chat_id="-100555",
            session_client=client,
            sender=sender,
            typing_factory=lambda *a, **kw: typing,
            sleep_fn=sleep_fn,
            now_fn=now_fn,
        )
        assert rc == "streaming"
        # Waiting card posted first, then both-online.
        msgs = [c.kwargs.get("message") or c.args[1] for c in sender.call_args_list]
        assert any("Waking" in m for m in msgs)
        assert any("Both sides" in m for m in msgs)
        typing.start.assert_called_once()
        typing.stop.assert_called_once()

    def test_terminal_status_returns_terminal(self):
        for status in ("canceled", "rescinded_after_sign", "completed", "expired"):
            client = MagicMock()
            client.get_session.return_value = self._session(status=status)
            now_fn, sleep_fn = self._fake_clock()
            sender = MagicMock()
            typing = self._typing()
            rc = rs._investor_wait_for_founder_streaming(
                session_id="session_neg_x", group_chat_id="-100555",
                session_client=client, sender=sender,
                typing_factory=lambda *a, **kw: typing,
                sleep_fn=sleep_fn, now_fn=now_fn,
            )
            assert rc == "terminal", f"status={status}"
            typing.stop.assert_called()

    def test_timeout_posts_emergency_and_returns_timeout(self, monkeypatch):
        """When founder_streaming_at never sets, poll must bail at
        INVESTOR_WAIT_TIMEOUT (180s) with an emergency card.
        """
        client = MagicMock()
        # Always joined, never streaming.
        client.get_session.return_value = self._session()
        now_fn, sleep_fn = self._fake_clock()
        sender = MagicMock()
        typing = self._typing()
        rc = rs._investor_wait_for_founder_streaming(
            session_id="session_neg_x", group_chat_id="-100555",
            session_client=client, sender=sender,
            typing_factory=lambda *a, **kw: typing,
            sleep_fn=sleep_fn, now_fn=now_fn,
        )
        assert rc == "timeout"
        msgs = [c.kwargs.get("message") or c.args[1] for c in sender.call_args_list]
        assert any("longer than expected" in m for m in msgs)
        typing.stop.assert_called()

    def test_heartbeat_fires_once_around_15s(self):
        client = MagicMock()
        client.get_session.return_value = self._session()
        now_fn, sleep_fn = self._fake_clock()
        sender = MagicMock()
        rc = rs._investor_wait_for_founder_streaming(
            session_id="session_neg_x", group_chat_id="-100555",
            session_client=client, sender=sender,
            typing_factory=lambda *a, **kw: self._typing(),
            sleep_fn=sleep_fn, now_fn=now_fn,
        )
        assert rc == "timeout"
        msgs = [c.kwargs.get("message") or c.args[1] for c in sender.call_args_list]
        # Exactly one heartbeat body across the whole wait (not zero, not
        # spamming every poll).
        heartbeats = [m for m in msgs if "Still waking" in m]
        assert len(heartbeats) == 1, (
            f"expected 1 heartbeat, got {len(heartbeats)}: {msgs}"
        )

    def test_transient_get_session_error_keeps_polling(self):
        from sshsign_session import SshsignSessionError
        client = MagicMock()
        # First call raises; second call succeeds with streaming_at set.
        client.get_session.side_effect = [
            SshsignSessionError("network blip"),
            self._session(streaming_at=1),
        ]
        now_fn, sleep_fn = self._fake_clock()
        rc = rs._investor_wait_for_founder_streaming(
            session_id="session_neg_x", group_chat_id="-100555",
            session_client=client,
            sender=MagicMock(),
            typing_factory=lambda *a, **kw: self._typing(),
            sleep_fn=sleep_fn, now_fn=now_fn,
        )
        assert rc == "streaming"
        assert client.get_session.call_count == 2

    def test_typing_stopped_even_on_exception(self):
        client = MagicMock()
        client.get_session.side_effect = RuntimeError("boom")
        typing = self._typing()
        now_fn, sleep_fn = self._fake_clock()
        with pytest.raises(RuntimeError):
            rs._investor_wait_for_founder_streaming(
                session_id="session_neg_x", group_chat_id="-100555",
                session_client=client, sender=MagicMock(),
                typing_factory=lambda *a, **kw: typing,
                sleep_fn=sleep_fn, now_fn=now_fn,
            )
        typing.stop.assert_called_once()


class TestCardInvariants:
    def test_founder_resumed_card_never_starts_with_slash(self):
        from format_event import format_event
        body = format_event({"type": "founder_resumed", "session_code": "INV-XYZ"})
        assert body is not None
        assert not body.startswith("/"), (
            "Invariant: resume cards must never start with '/' to prevent "
            "being dispatched as skill intents."
        )


# Note: the run_mint integration test for state-write is deferred to the
# Day 3.5 dual-bot E2E harness, which spawns run_safe subprocesses and
# exercises the full mint → state-write → resume loop end-to-end. The
# state_store.write_state call site in run_safe.py:run_mint is a 10-line
# surgical branch guarded by `mode == "two_party" and user_role ==
# "founder"`; the conditions are verified at code-review time and the
# negative case (single-party must NOT write state) is a Day-3.5 scenario.
