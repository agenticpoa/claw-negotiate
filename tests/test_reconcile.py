from __future__ import annotations

import json
from unittest.mock import MagicMock

import reconcile


def test_status_helpers_normalize_and_detect_terminal_states():
    assert reconcile.normalize_status(" Completed ") == "completed"
    assert reconcile.is_terminal_status("rescinded_after_sign") is True
    assert reconcile.is_terminal_status("joined") is False
    assert reconcile.is_terminal_status(None) is False


def test_latest_signing_pending_id_returns_last_signing_event(tmp_path):
    (tmp_path / "events.ndjson").write_text(
        "\n".join([
            json.dumps({"type": "offer", "round": 0}),
            json.dumps({"type": "signing", "pending_id": "pnd_old"}),
            "not json",
            json.dumps({"type": "signing", "pending_id": "pnd_new"}),
        ])
        + "\n"
    )

    assert reconcile.latest_signing_pending_id(tmp_path) == "pnd_new"


def test_latest_signing_pending_id_missing_file_is_empty(tmp_path):
    assert reconcile.latest_signing_pending_id(tmp_path) == ""


def test_executed_delivered_marker_is_idempotent(tmp_path):
    assert reconcile.has_executed_delivered(tmp_path) is False
    reconcile.mark_executed_delivered(tmp_path)
    reconcile.mark_executed_delivered(tmp_path)
    assert reconcile.has_executed_delivered(tmp_path) is True


def test_executed_delivered_marker_can_be_scoped_to_negotiation(tmp_path):
    reconcile.mark_executed_delivered(tmp_path, "neg_old")

    assert reconcile.has_executed_delivered(tmp_path, "neg_old") is True
    assert reconcile.has_executed_delivered(tmp_path, "neg_new") is False


class TestResumeClassifiers:
    def test_member_for_role_finds_matching_member(self):
        session = {"members": [
            {"role": "investor", "id": "i"},
            {"role": "founder", "id": "f"},
        ]}
        assert reconcile.member_for_role(session, "founder")["id"] == "f"
        assert reconcile.member_for_role(session, "moderator") is None

    def test_classify_founder_resume_phases(self):
        assert reconcile.classify_founder_resume(
            {"status": "open"}, group_chat_id=None,
        )[0] == reconcile.FOUNDER_WAIT_COUNTERPARTY

        assert reconcile.classify_founder_resume(
            {"status": "joined", "members": []}, group_chat_id=None,
        )[0] == reconcile.FOUNDER_STALE_NO_MEMBER

        session = {
            "status": "joined",
            "members": [{"role": "founder", "founder_streaming_at": 1}],
        }
        assert reconcile.classify_founder_resume(
            session, group_chat_id="-100",
        )[0] == reconcile.FOUNDER_ALREADY_STREAMING

        session = {
            "status": "joined",
            "members": [{"role": "founder", "founder_resumed_at": 1}],
        }
        assert reconcile.classify_founder_resume(
            session, group_chat_id=None,
        )[0] == reconcile.FOUNDER_WAIT_GROUP_ALREADY_PROMPTED

        session = {"status": "joined", "members": [{"role": "founder"}]}
        assert reconcile.classify_founder_resume(
            session, group_chat_id=None,
        )[0] == reconcile.FOUNDER_PROMPT_GROUP
        assert reconcile.classify_founder_resume(
            session, group_chat_id="-100",
        )[0] == reconcile.FOUNDER_START_STREAM

    def test_classify_investor_resume_phases(self):
        session = {"members": [{"role": "founder"}]}
        assert reconcile.classify_investor_resume(
            {"investor_streaming_started": True},
            session,
            group_chat_id="-100",
        )[0] == reconcile.INVESTOR_ALREADY_STREAMING

        assert reconcile.classify_investor_resume(
            {}, {"members": []}, group_chat_id="-100",
        )[0] == reconcile.INVESTOR_STALE_NO_FOUNDER

        assert reconcile.classify_investor_resume(
            {}, session, group_chat_id="-100",
        )[0] == reconcile.INVESTOR_WAIT_FOUNDER_STREAM

        session = {"members": [{"role": "founder", "founder_streaming_at": 1}]}
        assert reconcile.classify_investor_resume(
            {}, session, group_chat_id=None,
        )[0] == reconcile.INVESTOR_WAIT_GROUP_BIND
        assert reconcile.classify_investor_resume(
            {}, session, group_chat_id="-100",
        )[0] == reconcile.INVESTOR_START_STREAM


class TestReconcileSession:
    def test_defaults_to_founder_runner(self):
        founder = MagicMock(return_value=7)
        investor = MagicMock(return_value=9)
        client = MagicMock()
        sender = MagicMock()

        rc = reconcile.reconcile_session(
            {"negotiation_id": "neg_1"},
            founder_runner=founder,
            investor_runner=investor,
            session_client=client,
            sender=sender,
            now_fn=lambda: 123,
        )

        assert rc == 7
        founder.assert_called_once()
        investor.assert_not_called()
        assert founder.call_args.kwargs["session_client"] is client
        assert founder.call_args.kwargs["sender"] is sender

    def test_dispatches_investor_runner(self):
        founder = MagicMock(return_value=7)
        investor = MagicMock(return_value=9)

        rc = reconcile.reconcile_session(
            {"negotiation_id": "neg_1", "role": "investor"},
            founder_runner=founder,
            investor_runner=investor,
        )

        assert rc == 9
        founder.assert_not_called()
        investor.assert_called_once()


class TestReconcileStateByNegotiationId:
    def test_missing_pointer_returns_retry_code(self):
        rc = reconcile.reconcile_state_by_negotiation_id(
            "neg_missing",
            read_state=MagicMock(return_value=None),
            founder_runner=MagicMock(),
            investor_runner=MagicMock(),
        )
        assert rc == 1

    def test_reads_pointer_and_dispatches(self):
        founder = MagicMock(return_value=0)
        investor = MagicMock(return_value=4)
        client = MagicMock()
        sender = MagicMock()

        rc = reconcile.reconcile_state_by_negotiation_id(
            "neg_i",
            read_state=MagicMock(return_value={
                "negotiation_id": "neg_i",
                "role": "investor",
            }),
            founder_runner=founder,
            investor_runner=investor,
            session_client=client,
            sender=sender,
            now_fn=lambda: 456,
        )

        assert rc == 4
        founder.assert_not_called()
        investor.assert_called_once()
        assert investor.call_args.kwargs["session_client"] is client
        assert investor.call_args.kwargs["sender"] is sender


class TestReconcileActiveSessions:
    def test_iterates_all_pointers_and_dispatches_by_role(self):
        founder = MagicMock(return_value=0)
        investor = MagicMock(return_value=0)
        pointers = [
            {"negotiation_id": "neg_f", "role": "founder"},
            {"negotiation_id": "neg_i", "role": "investor"},
        ]

        rc = reconcile.reconcile_active_sessions(
            list_active=lambda: pointers,
            founder_runner=founder,
            investor_runner=investor,
        )

        assert rc == 0
        assert founder.call_args.args[0]["negotiation_id"] == "neg_f"
        assert investor.call_args.args[0]["negotiation_id"] == "neg_i"

    def test_list_active_failure_is_contained(self):
        stderr = MagicMock()

        rc = reconcile.reconcile_active_sessions(
            list_active=MagicMock(side_effect=RuntimeError("boom")),
            founder_runner=MagicMock(),
            investor_runner=MagicMock(),
            stderr=stderr,
        )

        assert rc == 0
        assert "list_active failed" in stderr.write.call_args.args[0]

    def test_pointer_failure_does_not_stop_later_pointer(self):
        founder = MagicMock(side_effect=[RuntimeError("bad"), 0])
        stderr = MagicMock()
        pointers = [
            {"negotiation_id": "neg_bad", "role": "founder"},
            {"negotiation_id": "neg_good", "role": "founder"},
        ]

        rc = reconcile.reconcile_active_sessions(
            list_active=lambda: pointers,
            founder_runner=founder,
            investor_runner=MagicMock(),
            stderr=stderr,
        )

        assert rc == 0
        assert founder.call_count == 2
        assert "neg_bad" in stderr.write.call_args.args[0]
