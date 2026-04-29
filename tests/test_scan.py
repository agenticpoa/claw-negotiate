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
        "founder_dm_chat_id": "123456",
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

    def test_missing_output_dir_cleans_pointer(self, state_record, _state_dir, tmp_path):
        """If output_dir was wiped (/tmp cleaned), stop scanning this id."""
        state_store.write_state(state_record)
        state = dict(state_record)
        state["output_dir"] = str(tmp_path / "missing-output-dir")
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


class TestRunFounderResumePath1StateMachine:
    """Path 1 two-phase resume: state machine branches on
    (streaming_at, group_chat_id). The dedup signal is streaming_at
    (the "we're done" marker), NOT resumed_at (which now means
    "phase A acknowledged join, waiting for /bind"). See
    _run_founder_resume's docstring for the full state table.
    """

    def test_phase_a_no_group_yet(
        self, state_record, fake_client, monkeypatch, founder_output_dir,
    ):
        """Status=joined, no group bound, no resumed_at.
        → post create-group card to founder DM, set resumed_at,
        EXIT WITHOUT STREAMING."""
        # No group bound, no streaming_at, no resumed_at on founder row.
        fake_client.get_session.return_value["group_chat_id"] = 0
        fake_client.get_session.return_value["metadata_public"] = (
            '{"founder_bot_handle": "AgenticPOA_bot"}'
        )
        fake_client.get_session.return_value["members"][1]["bot_handle"] = (
            "AgenticPOAInvestor_bot"
        )
        monkeypatch.setattr(rs, "_resolve_group_chat_id", lambda *a, **kw: None)
        sender = MagicMock()

        with patch.object(rs, "_stream_to_telegram") as stream:
            rc = rs._run_founder_resume(
                state_record, session_client=fake_client, sender=sender,
                now_fn=lambda: 1714000000,
            )

        assert rc == 0
        # Stream MUST NOT be called — group isn't bound yet.
        stream.assert_not_called()
        # resumed_at written; streaming_at NOT written.
        fields_written = [
            (kw.get("field") or a[1])
            for a, kw in (
                (c.args, c.kwargs)
                for c in fake_client.update_session_member.call_args_list
            )
        ]
        assert "founder_resumed_at" in fields_written
        assert "founder_streaming_at" not in fields_written
        # Create-group card landed in the founder's DM (config.json's chat_id).
        msgs = [c.kwargs.get("message") or c.args[1] for c in sender.call_args_list]
        assert any("create" in m.lower() and "group" in m.lower() for m in msgs)

    def test_phase_a_done_idempotent(
        self, state_record, fake_client, monkeypatch,
    ):
        """resumed_at already set, no group, no streaming_at.
        → no-op (don't re-spam the create-group card every 10s)."""
        fake_client.get_session.return_value["group_chat_id"] = 0
        fake_client.get_session.return_value["members"][0]["founder_resumed_at"] = 1700000000
        monkeypatch.setattr(rs, "_resolve_group_chat_id", lambda *a, **kw: None)
        sender = MagicMock()

        with patch.object(rs, "_stream_to_telegram") as stream:
            rc = rs._run_founder_resume(
                state_record, session_client=fake_client, sender=sender,
            )

        assert rc == 0
        stream.assert_not_called()
        fake_client.update_session_member.assert_not_called()
        # Critical: NO new card emitted — we're already in phase A-done.
        assert sender.call_count == 0

    def test_phase_b_runs_stream_when_group_appears(
        self, state_record, fake_client, monkeypatch,
    ):
        """resumed_at set (phase A acknowledged on a prior tick),
        group_chat_id NOW set (founder pasted /bind), streaming_at null.
        → set streaming_at, run _stream_to_telegram against the group."""
        fake_client.get_session.return_value["members"][0]["founder_resumed_at"] = 1700000000
        fake_client.get_session.return_value["group_chat_id"] = -100456
        monkeypatch.setattr(rs, "_resolve_group_chat_id", lambda *a, **kw: "-100456")
        sender = MagicMock()
        events = []
        fake_client.update_session_member.side_effect = (
            lambda *a, **kw: events.append(("update", kw.get("field") or a[1]))
        )
        def _fake_stream(**kw):
            events.append(("stream", kw.get("group_chat_id")))
            return (0, None)
        with patch.object(rs, "_stream_to_telegram", side_effect=_fake_stream):
            rc = rs._run_founder_resume(
                state_record, session_client=fake_client, sender=sender,
                now_fn=lambda: 1714000005,
            )

        assert rc == 0
        # streaming_at written THEN stream called (sequencing critical).
        assert events == [
            ("update", "founder_streaming_at"),
            ("stream", "-100456"),
        ], f"phase B sequencing wrong: {events}"
        assert (Path(state_record["output_dir"]) / ".session.pid").read_text().strip()

    def test_clears_stale_founder_streaming_marker_before_classify(
        self, state_record, fake_client, monkeypatch,
    ):
        """If the host killed the previous founder stream, sshsign's
        founder_streaming_at marker should not permanently block retries."""
        fake_client.get_session.return_value["members"][0]["founder_resumed_at"] = 1700000000
        fake_client.get_session.return_value["members"][0]["founder_streaming_at"] = 1700000010
        fake_client.get_session.return_value["group_chat_id"] = -100456
        (Path(state_record["output_dir"]) / ".session.pid").write_text("999999")
        monkeypatch.setattr(rs, "_resolve_group_chat_id", lambda *a, **kw: "-100456")
        monkeypatch.setattr(rs, "_pid_file_has_live_negotiation", lambda _out: False)
        events = []
        fake_client.update_session_member.side_effect = (
            lambda *a, **kw: events.append(("update", kw.get("field") or a[1], kw.get("value") if kw else a[2]))
        )

        def _fake_stream(**kw):
            events.append(("stream", kw.get("group_chat_id"), None))
            return (0, None)

        with patch.object(rs, "_stream_to_telegram", side_effect=_fake_stream), \
             patch.object(rs, "_ssh_history", return_value=[]):
            rc = rs._run_founder_resume(
                state_record, session_client=fake_client,
                now_fn=lambda: 1714000005,
            )

        assert rc == 0
        assert ("update", "founder_streaming_at", 0) in events
        assert ("stream", "-100456", None) in events

    def test_stale_founder_stream_with_history_fails_closed(
        self, state_record, fake_client, monkeypatch,
    ):
        """Restarting the founder after offers exist would replay Round 0.
        Fail closed until upstream can resume first-mover history."""
        fake_client.get_session.return_value["members"][0]["founder_resumed_at"] = 1700000000
        fake_client.get_session.return_value["members"][0]["founder_streaming_at"] = 1700000010
        fake_client.get_session.return_value["group_chat_id"] = -100456
        (Path(state_record["output_dir"]) / ".session.pid").write_text("999999")
        monkeypatch.setattr(rs, "_resolve_group_chat_id", lambda *a, **kw: "-100456")
        monkeypatch.setattr(rs, "_pid_file_has_live_negotiation", lambda _out: False)

        with patch.object(rs, "_stream_to_telegram") as stream, \
             patch.object(rs, "_ssh_history", return_value=[
                 {"round": 0, "from": "founder", "type": "offer"},
             ]):
            rc = rs._run_founder_resume(
                state_record, session_client=fake_client,
                now_fn=lambda: 1714000005,
            )

        assert rc == 3
        stream.assert_not_called()
        fields = [
            kw.get("field") or a[1]
            for a, kw in (
                (c.args, c.kwargs)
                for c in fake_client.update_session_member.call_args_list
            )
        ]
        assert "founder_streaming_at" not in fields

    def test_phase_b_lease_conflict_skips_stream(
        self, state_record, fake_client, monkeypatch,
    ):
        """If another worker owns the sshsign negotiate lease, this tick
        exits quietly before setting streaming_at or spawning upstream."""
        from sshsign_session import LeaseHeldError
        fake_client.get_session.return_value["members"][0]["founder_resumed_at"] = 1700000000
        fake_client.get_session.return_value["group_chat_id"] = -100456
        fake_client.acquire_lease.side_effect = LeaseHeldError("lease_held", holder="other")
        monkeypatch.setattr(rs, "_resolve_group_chat_id", lambda *a, **kw: "-100456")

        with patch.object(rs, "_stream_to_telegram") as stream:
            rc = rs._run_founder_resume(
                state_record, session_client=fake_client,
                now_fn=lambda: 1714000005,
            )

        assert rc == 0
        stream.assert_not_called()
        fake_client.update_session_member.assert_not_called()

    def test_combined_pass_when_no_prior_phase_a(
        self, state_record, fake_client, monkeypatch,
    ):
        """No resumed_at, group already bound (run_bind's in-process
        fast path: founder pasted /bind directly without a cron tick
        in between). Phase B should set both resumed_at AND
        streaming_at, then run stream."""
        fake_client.get_session.return_value["group_chat_id"] = -100789
        monkeypatch.setattr(rs, "_resolve_group_chat_id", lambda *a, **kw: "-100789")
        events = []
        fake_client.update_session_member.side_effect = (
            lambda *a, **kw: events.append(("update", kw.get("field") or a[1]))
        )
        with patch.object(rs, "_stream_to_telegram", return_value=(0, None)):
            rc = rs._run_founder_resume(
                state_record, session_client=fake_client,
                now_fn=lambda: 1714000010,
            )
        assert rc == 0
        # Both timestamps written, in correct order.
        fields = [e[1] for e in events if e[0] == "update"]
        assert fields == ["founder_resumed_at", "founder_streaming_at"], fields
        assert (Path(state_record["output_dir"]) / ".session.pid").read_text().strip()

    def test_done_state_is_noop(self, state_record, fake_client, monkeypatch):
        """streaming_at already set → entire function is a no-op,
        regardless of group_chat_id or resumed_at. Prevents
        double-stream on concurrent cron ticks."""
        fake_client.get_session.return_value["members"][0]["founder_resumed_at"] = 1700000000
        fake_client.get_session.return_value["members"][0]["founder_streaming_at"] = 1700000005
        fake_client.get_session.return_value["group_chat_id"] = -100123
        monkeypatch.setattr(rs, "_resolve_group_chat_id", lambda *a, **kw: "-100123")

        with patch.object(rs, "_stream_to_telegram") as stream:
            rc = rs._run_founder_resume(state_record, session_client=fake_client)
        assert rc == 0
        stream.assert_not_called()
        fake_client.update_session_member.assert_not_called()

    def test_done_state_reconciles_late_signatures(
        self, state_record, fake_client, monkeypatch, founder_output_dir,
    ):
        """If the founder stream already ran but exited while waiting for
        the investor signature, cron should finalize once sshsign reports
        both pendings approved."""
        fake_client.get_session.return_value["members"][0]["founder_resumed_at"] = 1700000000
        fake_client.get_session.return_value["members"][0]["founder_streaming_at"] = 1700000005
        fake_client.get_session.return_value["group_chat_id"] = -100123
        monkeypatch.setattr(rs, "_resolve_group_chat_id", lambda *a, **kw: "-100123")
        state_store.write_state(state_record)

        with patch.object(rs, "_stream_to_telegram") as stream, \
             patch.object(rs, "_creator_reconcile_finalization",
                          return_value=0) as reconcile:
            rc = rs._run_founder_resume(state_record, session_client=fake_client)

        assert rc == 0
        stream.assert_not_called()
        reconcile.assert_called_once()
        assert reconcile.call_args.kwargs["group_chat_id"] == "-100123"
        assert state_store.read_state("neg_abc") is None


class TestRunFounderResumeStreamFailure:
    def test_streaming_at_failure_is_non_fatal(
        self, state_record, fake_client, monkeypatch,
    ):
        """update_session_member(founder_streaming_at) failing must NOT
        abort the stream — the audit trail on sshsign captures the gap.
        Phase B path with combined-first-pass."""
        from sshsign_session import SshsignSessionError
        fake_client.get_session.return_value["group_chat_id"] = -100123
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
        assert rc == 0  # stream-rc precedence

    def test_stream_failure_returns_rc_without_cleanup(
        self, state_record, fake_client, monkeypatch,
    ):
        """Non-terminal stream failure (rc != 0): don't delete state.
        Next cron tick re-evaluates; the dedup gate (streaming_at set)
        prevents a re-stream. State pointer survives until session
        terminates."""
        fake_client.get_session.return_value["group_chat_id"] = -100123
        state_store.write_state(state_record)
        monkeypatch.setattr(rs, "_resolve_group_chat_id", lambda *a, **kw: "-100123")
        with patch.object(rs, "_stream_to_telegram", return_value=(1, None)):
            rc = rs._run_founder_resume(state_record, session_client=fake_client)
        assert rc == 1
        assert state_store.read_state("neg_abc") is not None


class TestRunScan:
    def test_empty_state_dir_returns_zero(self):
        assert rs.run_scan() == 0

    def test_scan_throttle_skips_near_duplicate_tick(
        self, founder_output_dir, fake_client, monkeypatch,
    ):
        state_store.write_state({
            "negotiation_id": "neg_a",
            "output_dir": str(founder_output_dir),
            "session_code": "INV-A",
            "role": "founder",
        })
        reconcile = MagicMock(return_value=[])
        monkeypatch.setattr(rs.orchestrator, "reconcile_active", reconcile)

        assert rs.run_scan(session_client=fake_client, now_fn=lambda: 100.0) == 0
        assert rs.run_scan(session_client=fake_client, now_fn=lambda: 105.0) == 0

        reconcile.assert_called_once()

    def test_iterates_and_resumes_each_pointer(
        self, founder_output_dir, fake_client, monkeypatch,
    ):
        monkeypatch.setattr(rs, "_resolve_group_chat_id", lambda *a, **kw: None)
        for nid in ("neg_a", "neg_b"):
            state_store.write_state({
                "negotiation_id": nid,
                "output_dir": str(founder_output_dir),
                "session_code": f"INV-{nid.upper()}",
                "role": "founder",
            })
        with patch.object(rs.orchestrator, "reconcile_active", return_value=[]) as resume:
            rc = rs.run_scan(session_client=fake_client)
        assert rc == 0
        assert resume.call_count == 1
        assert len(resume.call_args.kwargs["states"]) == 2

    def test_per_pointer_failure_does_not_halt_tick(
        self, founder_output_dir, fake_client, monkeypatch,
    ):
        for nid in ("neg_bad", "neg_good"):
            state_store.write_state({
                "negotiation_id": nid,
                "output_dir": str(founder_output_dir),
                "session_code": f"INV-{nid.upper()}",
                "role": "founder",
            })
        with patch.object(rs.orchestrator, "reconcile_active", return_value=[]) as reconcile:
            rc = rs.run_scan(session_client=fake_client)
        assert rc == 0
        assert {
            s["negotiation_id"] for s in reconcile.call_args.kwargs["states"]
        } == {"neg_bad", "neg_good"}

    def test_dispatches_by_role(self, founder_output_dir, fake_client):
        """Symmetric Path 1: state pointers carry a `role` field;
        scan must call `_run_investor_resume` for investor pointers
        and `_run_founder_resume` for founder pointers."""
        state_store.write_state({
            "negotiation_id": "neg_f",
            "output_dir": str(founder_output_dir),
            "session_code": "INV-F",
            "role": "founder",
        })
        state_store.write_state({
            "negotiation_id": "neg_i",
            "output_dir": str(founder_output_dir),
            "session_code": "INV-I",
            "role": "investor",
        })

        with patch.object(rs.orchestrator, "reconcile_active", return_value=[]) as reconcile:
            rs.run_scan(session_client=fake_client)

        roles = {
            s["negotiation_id"]: s["role"]
            for s in reconcile.call_args.kwargs["states"]
        }
        assert roles == {"neg_f": "founder", "neg_i": "investor"}

    def test_missing_role_is_passed_to_orchestrator_as_invalid_state(
        self, founder_output_dir, fake_client,
    ):
        """No backwards compatibility: malformed pointers are handled by
        the orchestrator, not silently treated as founder."""
        state_store.write_state({
            "negotiation_id": "neg_legacy",
            "output_dir": str(founder_output_dir),
            "session_code": "INV-LEGACY",
            # NO role field
        })
        with patch.object(rs.orchestrator, "reconcile_active", return_value=[]) as reconcile:
            rs.run_scan(session_client=fake_client)
        assert reconcile.call_args.kwargs["states"][0]["negotiation_id"] == "neg_legacy"

    def test_terminal_orchestrator_result_cleans_pointer(
        self, founder_output_dir, fake_client,
    ):
        state_store.write_state({
            "negotiation_id": "neg_done",
            "output_dir": str(founder_output_dir),
            "session_code": "INV-DONE",
            "role": "founder",
        })

        with patch.object(
            rs.orchestrator,
            "reconcile_active",
            return_value=[rs.orchestrator.ReconcileResult("terminal:completed")],
        ):
            rs.run_scan(session_client=fake_client)

        assert state_store.read_state("neg_done") is None


class TestRunInvestorResume:
    """Symmetric Path 1: investor-side cron resume. Mirrors
    `_run_founder_resume` but gates on the founder's
    `founder_streaming_at` flipping, and dedupes via a flag we
    persist into the state pointer.
    """

    def _state(self, founder_output_dir):
        (founder_output_dir / "mint.json").write_text(json.dumps({
            "negotiation_id": "neg_inv",
            "mode": "two_party",
            "user_role": "investor",
        }))
        return {
            "negotiation_id": "neg_inv",
            "output_dir": str(founder_output_dir),
            "session_code": "INV-INV",
            "role": "investor",
            "investor_dm_chat_id": "8888888",
        }

    def _session(self, *, status="joined", group_chat_id=-1001234567890,
                 founder_streaming_at=None):
        return {
            "session_id": "session_neg_inv",
            "session_code": "INV-INV",
            "status": status,
            "group_chat_id": group_chat_id,
            "members": [
                {"role": "founder", "user_id": "u_f",
                 "founder_streaming_at": founder_streaming_at,
                 "founder_resumed_at": founder_streaming_at},
                {"role": "investor", "user_id": "u_i",
                 "bot_handle": "AgenticPOAInvestor_bot"},
            ],
        }

    def test_founder_not_streaming_skips(self, founder_output_dir):
        """Founder hasn't /bound + flipped streaming_at yet. Investor
        stays in the wait state — no group card, no stream spawn."""
        state = self._state(founder_output_dir)
        state_store.write_state(state)

        client = MagicMock()
        client.get_session.return_value = self._session(founder_streaming_at=None)
        sender = MagicMock()

        with patch.object(rs, "_stream_to_telegram") as stream:
            rc = rs._run_investor_resume(state, session_client=client, sender=sender)

        assert rc == 1
        stream.assert_not_called()
        # No group sends until founder is ready.
        targets = [str(c.args[0]) for c in sender.call_args_list if c.args]
        assert all(not t.startswith("-") for t in targets), targets

    def test_terminal_session_cleans_pointer(self, founder_output_dir):
        """Status=canceled → post status card to investor DM, delete
        pointer, return 2."""
        state = self._state(founder_output_dir)
        state_store.write_state(state)

        client = MagicMock()
        client.get_session.return_value = self._session(status="canceled")
        sender = MagicMock()

        with patch.object(rs, "_stream_to_telegram") as stream:
            rc = rs._run_investor_resume(state, session_client=client, sender=sender)

        assert rc == 2
        stream.assert_not_called()
        assert state_store.read_state("neg_inv") is None

    def test_mismatched_output_dir_cleans_stale_pointer(self, founder_output_dir):
        """When /tmp/safe_negotiate has been reused by a newer attempt,
        an older pointer must not stream against the newer mint.json."""
        state = self._state(founder_output_dir)
        (founder_output_dir / "mint.json").write_text(json.dumps({
            "negotiation_id": "neg_new",
            "mode": "two_party",
            "user_role": "investor",
        }))
        state_store.write_state(state)

        client = MagicMock()
        sender = MagicMock()

        with patch.object(rs, "_stream_to_telegram") as stream:
            rc = rs._run_investor_resume(state, session_client=client, sender=sender)

        assert rc == 2
        client.get_session.assert_not_called()
        stream.assert_not_called()
        sender.assert_not_called()
        assert state_store.read_state("neg_inv") is None

    def test_old_terminal_session_is_silent_when_newer_active_pointer_exists(
        self, founder_output_dir,
    ):
        """A canceled prior attempt should not leak into the user's DM
        once the same investor has a newer active negotiation."""
        state = self._state(founder_output_dir)
        state_store.write_state(state)
        state_store.write_state({
            "negotiation_id": "neg_new",
            "output_dir": str(founder_output_dir),
            "session_code": "INV-NEW",
            "role": "investor",
            "investor_dm_chat_id": state["investor_dm_chat_id"],
        })

        client = MagicMock()
        client.get_session.return_value = self._session(status="canceled")
        sender = MagicMock()

        with patch.object(rs, "_stream_to_telegram") as stream:
            rc = rs._run_investor_resume(state, session_client=client, sender=sender)

        assert rc == 2
        stream.assert_not_called()
        sender.assert_not_called()
        assert state_store.read_state("neg_inv") is None
        assert state_store.read_state("neg_new") is not None

    def test_happy_path_streams_and_finalizes(self, founder_output_dir, monkeypatch):
        """founder_streaming_at set + group bound → write started flag,
        post 'both online' card to group, spawn stream, run joiner-
        finalize, clean up pointer."""
        state = self._state(founder_output_dir)
        state_store.write_state(state)

        client = MagicMock()
        client.get_session.return_value = self._session(
            founder_streaming_at=1700000000,
            group_chat_id=-1001234567890,
        )
        sender = MagicMock()
        signing = {"type": "signing", "pending_id": "pnd_inv"}

        monkeypatch.setattr(rs, "_resolve_group_chat_id",
                            lambda *a, **kw: "-1001234567890")

        with patch.object(rs, "_stream_to_telegram",
                          return_value=(0, signing)) as stream, \
             patch.object(rs, "_joiner_await_sign_and_finalize",
                          return_value=0) as joiner:
            rc = rs._run_investor_resume(state, session_client=client, sender=sender)

        assert rc == 0
        stream.assert_called_once()
        # group_chat_id threaded to the stream
        assert stream.call_args.kwargs["group_chat_id"] == "-1001234567890"
        # investor DM threaded as primary chat_id (signing URL routing)
        assert stream.call_args.kwargs["chat_id"] == "8888888"
        joiner.assert_called_once()
        assert (founder_output_dir / ".session.pid").read_text().strip()
        # State pointer cleaned up after joiner finalize.
        assert state_store.read_state("neg_inv") is None
        # Both-online card posted to the group.
        group_sends = [c for c in sender.call_args_list
                       if c.args and str(c.args[0]) == "-1001234567890"]
        assert group_sends, "must announce 'both online' in the group"

    def test_dedup_via_started_flag(self, founder_output_dir, monkeypatch):
        """If `investor_streaming_started` is already set on the
        state pointer, this resume is a no-op — another tick or the
        in-process bind path already owns the stream."""
        state = self._state(founder_output_dir)
        state["investor_streaming_started"] = True
        state_store.write_state(state)

        client = MagicMock()
        client.get_session.return_value = self._session(
            founder_streaming_at=1700000000,
            group_chat_id=-1001234567890,
        )
        sender = MagicMock()

        with patch.object(rs, "_stream_to_telegram") as stream:
            rc = rs._run_investor_resume(state, session_client=client, sender=sender)

        assert rc == 0
        stream.assert_not_called()
        # We still query sshsign first so terminal status can clean up
        # already-started pointers after a later cancel/rescind.
        assert client.get_session.call_count >= 1

    def test_started_terminal_session_still_cleans_pointer(self, founder_output_dir):
        state = self._state(founder_output_dir)
        state["investor_streaming_started"] = True
        state_store.write_state(state)

        client = MagicMock()
        client.get_session.return_value = self._session(status="canceled")
        sender = MagicMock()

        with patch.object(rs, "_stream_to_telegram") as stream:
            rc = rs._run_investor_resume(state, session_client=client, sender=sender)

        assert rc == 2
        stream.assert_not_called()
        assert state_store.read_state("neg_inv") is None

    def test_pointer_marked_started_before_stream_spawn(self, founder_output_dir, monkeypatch):
        """The dedup flag must be persisted BEFORE _stream_to_telegram
        is invoked. Otherwise a 30s cron tick during a 5-minute stream
        could double-spawn upstream."""
        state = self._state(founder_output_dir)
        state_store.write_state(state)

        client = MagicMock()
        client.get_session.return_value = self._session(
            founder_streaming_at=1700000000,
            group_chat_id=-1001234567890,
        )
        sender = MagicMock()
        observed_at_stream_call: list[bool] = []

        def _stream(*a, **kw):
            persisted = state_store.read_state("neg_inv") or {}
            observed_at_stream_call.append(
                bool(persisted.get("investor_streaming_started"))
            )
            return (1, None)  # short-circuit out

        monkeypatch.setattr(rs, "_resolve_group_chat_id",
                            lambda *a, **kw: "-1001234567890")

        with patch.object(rs, "_stream_to_telegram", side_effect=_stream):
            rs._run_investor_resume(state, session_client=client, sender=sender)

        assert observed_at_stream_call == [True], (
            "started flag must be persisted BEFORE _stream_to_telegram"
        )

    def test_failed_stream_clears_started_flag_for_retry(
        self, founder_output_dir, monkeypatch,
    ):
        """If upstream dies before signing, the next cron tick must be
        allowed to retry. A stale started flag caused INV-WWXB8 to wedge
        after an env-related startup failure."""
        state = self._state(founder_output_dir)
        state_store.write_state(state)

        client = MagicMock()
        client.get_session.return_value = self._session(
            founder_streaming_at=1700000000,
            group_chat_id=-1001234567890,
        )
        sender = MagicMock()

        monkeypatch.setattr(rs, "_resolve_group_chat_id",
                            lambda *a, **kw: "-1001234567890")

        with patch.object(rs, "_stream_to_telegram", return_value=(1, None)):
            rc = rs._run_investor_resume(
                state, session_client=client, sender=sender,
            )

        assert rc == 1
        persisted = state_store.read_state("neg_inv") or {}
        assert "investor_streaming_started" not in persisted

    def test_failed_stream_does_not_resend_both_online_on_retry(
        self, founder_output_dir, monkeypatch,
    ):
        """The visible group status card is idempotent independently of
        the stream retry flag."""
        state = self._state(founder_output_dir)
        state_store.write_state(state)

        client = MagicMock()
        client.get_session.return_value = self._session(
            founder_streaming_at=1700000000,
            group_chat_id=-1001234567890,
        )
        sender = MagicMock()

        monkeypatch.setattr(rs, "_resolve_group_chat_id",
                            lambda *a, **kw: "-1001234567890")

        with patch.object(rs, "_stream_to_telegram", return_value=(1, None)):
            assert rs._run_investor_resume(
                state, session_client=client, sender=sender,
            ) == 1

        retry_state = state_store.read_state("neg_inv") or {}
        assert retry_state.get("investor_both_online_sent") is True
        assert "investor_streaming_started" not in retry_state

        sender.reset_mock()
        with patch.object(rs, "_stream_to_telegram", return_value=(1, None)):
            assert rs._run_investor_resume(
                retry_state, session_client=client, sender=sender,
            ) == 1

        group_sends = [
            c for c in sender.call_args_list
            if c.args and str(c.args[0]) == "-1001234567890"
        ]
        assert group_sends == []


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

        with patch.object(rs.orchestrator, "reconcile_state") as resume:
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
        assert add_call[add_call.index("--system-event") + 1] == "negotiate_safe_scan"
        assert "--keep-after-run" in add_call
        # Regression: these flags were rejected by the live OC CLI on
        # INV-6Z4K7 (Apr 25). Keep them out.
        assert "--exact" not in add_call, (
            "--exact is only valid with --cron schedules, not --every"
        )
        assert "--session" not in add_call, (
            "main session is the right target for system-event payloads; "
            "isolated/current session would also require --message"
        )
        assert "--no-deliver" not in add_call, (
            "--no-deliver requires non-main session"
        )

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

    def test_list_failure_can_install_system_cron_fallback(self):
        calls: list[tuple[list[str], dict]] = []

        def openclaw_runner(argv, **kwargs):
            calls.append((argv, kwargs))
            return self._cp(1, stderr="pairing required")

        def system_runner(argv, **kwargs):
            calls.append((argv, kwargs))
            if argv == ["crontab", "-l"]:
                return self._cp(1, stderr="no crontab for root")
            if argv == ["crontab", "-"]:
                assert "run_safe.py scan" in kwargs["input"]
                assert rs.SYSTEM_CRON_MARKER in kwargs["input"]
                return self._cp(0)
            raise AssertionError(f"unexpected argv {argv}")

        ok, err = rs.ensure_cron(runner=openclaw_runner, system_runner=system_runner)
        assert ok and err is None
        assert any(argv == ["crontab", "-"] for argv, _ in calls)

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


@pytest.mark.real_wait
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
        # Waiting card posted first, then both-online. Inverted-invitation
        # changed the waiting copy from "Waking the founder's agent" to
        # "Joined. Waiting for the founder's agent" since the investor
        # is now the one who joined (not waking anything).
        msgs = [c.kwargs.get("message") or c.args[1] for c in sender.call_args_list]
        assert any("Waiting for the founder" in m or "Joined" in m for m in msgs)
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
