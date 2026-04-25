"""Tests for the bot-role enforcement + single-active-negotiation gates
added to run_prepare to prevent:

1. The user accidentally getting an INVESTOR confirm card on the
   FOUNDER bot (or vice versa) — leaks each party's constraints to
   the other.
2. The user starting a fresh negotiation while a prior one is still
   in flight, leading to mixed state and the agent not knowing which
   to operate on.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import run_safe as rs
import state_store


@pytest.fixture
def _clean_role_env(monkeypatch):
    """Override for tests that specifically test 'no enforcement'
    semantics — strip both inference signals and the explicit knob.
    Conftest's autouse ``_bot_role_either`` sets
    ``NEGOTIATE_SAFE_BOT_ROLE=either`` for the rest of the suite.
    """
    monkeypatch.delenv("NEGOTIATE_SAFE_BOT_ROLE", raising=False)
    monkeypatch.delenv("FOUNDER_NAME", raising=False)
    monkeypatch.delenv("INVESTOR_NAME", raising=False)
    yield


@pytest.fixture
def _state_dir(monkeypatch, tmp_path):
    """Local handle to the per-test state dir (autouse'd via conftest's
    ``_isolated_state_dir``). Tests that need to write state files
    accept this fixture for the path."""
    import os
    return Path(os.environ["CLAW_NEGOTIATE_STATE_DIR"])


# ─── _classify_bot_role ────────────────────────────────────────────────────

class TestClassifyBotRole:
    def test_explicit_founder_wins(self, monkeypatch):
        monkeypatch.setenv("NEGOTIATE_SAFE_BOT_ROLE", "founder")
        monkeypatch.setenv("INVESTOR_NAME", "Should Be Ignored")
        assert rs._classify_bot_role() == "founder"

    def test_explicit_investor_wins(self, monkeypatch):
        monkeypatch.setenv("NEGOTIATE_SAFE_BOT_ROLE", "investor")
        monkeypatch.setenv("FOUNDER_NAME", "Should Be Ignored")
        assert rs._classify_bot_role() == "investor"

    def test_either_means_no_enforcement(self, monkeypatch):
        monkeypatch.setenv("NEGOTIATE_SAFE_BOT_ROLE", "either")
        assert rs._classify_bot_role() is None

    def test_inferred_founder_from_env(self, monkeypatch):
        # Inference path only fires when no explicit knob is set.
        monkeypatch.delenv("NEGOTIATE_SAFE_BOT_ROLE", raising=False)
        monkeypatch.setenv("FOUNDER_NAME", "Juan")
        # INVESTOR_NAME unset
        assert rs._classify_bot_role() == "founder"

    def test_inferred_investor_from_env(self, monkeypatch):
        monkeypatch.delenv("NEGOTIATE_SAFE_BOT_ROLE", raising=False)
        monkeypatch.delenv("FOUNDER_NAME", raising=False)
        monkeypatch.setenv("INVESTOR_NAME", "Nora")
        assert rs._classify_bot_role() == "investor"

    def test_both_set_no_enforcement(self, monkeypatch):
        monkeypatch.delenv("NEGOTIATE_SAFE_BOT_ROLE", raising=False)
        monkeypatch.setenv("FOUNDER_NAME", "Juan")
        monkeypatch.setenv("INVESTOR_NAME", "Nora")
        assert rs._classify_bot_role() is None

    def test_neither_set_no_enforcement(self, monkeypatch):
        monkeypatch.delenv("NEGOTIATE_SAFE_BOT_ROLE", raising=False)
        monkeypatch.delenv("FOUNDER_NAME", raising=False)
        monkeypatch.delenv("INVESTOR_NAME", raising=False)
        assert rs._classify_bot_role() is None


# ─── pre-parse role guard ──────────────────────────────────────────────────

class TestEnforceBotRolePreParse:
    def test_no_role_set_returns_none(self):
        assert rs._enforce_bot_role_pre_parse("Join INV-7K3X9 as investor") is None

    def test_investor_msg_to_founder_bot_blocked(self, monkeypatch):
        monkeypatch.setenv("NEGOTIATE_SAFE_BOT_ROLE", "founder")
        monkeypatch.setenv("NEGOTIATE_SAFE_COUNTERPARTY_BOT", "InvBot")
        msg = "Join negotiation INV-7K3X9 as investor, $40M cap"
        err = rs._enforce_bot_role_pre_parse(msg)
        assert err is not None
        assert "FOUNDER" in err
        assert "@InvBot" in err

    def test_founder_msg_to_investor_bot_blocked(self, monkeypatch):
        monkeypatch.setenv("NEGOTIATE_SAFE_BOT_ROLE", "investor")
        monkeypatch.setenv("NEGOTIATE_SAFE_COUNTERPARTY_BOT", "@FounderBot")
        msg = "Negotiate a SAFE with Nora at Babes Fund, $40M cap"
        err = rs._enforce_bot_role_pre_parse(msg)
        assert err is not None
        assert "INVESTOR" in err
        assert "@FounderBot" in err

    def test_no_counterparty_handle_falls_back_to_generic(self, monkeypatch):
        """When operator hasn't configured NEGOTIATE_SAFE_COUNTERPARTY_BOT
        we must NOT hardcode a handle that may not exist — fall back to
        a generic phrase that hints at the right destination without
        making a wrong claim."""
        monkeypatch.setenv("NEGOTIATE_SAFE_BOT_ROLE", "founder")
        monkeypatch.delenv("NEGOTIATE_SAFE_COUNTERPARTY_BOT", raising=False)
        err = rs._enforce_bot_role_pre_parse("Join INV-X as investor")
        assert err is not None
        # No invented handle leaks into the message.
        assert "@" not in err.split("\n", 1)[1].split(".")[0], err
        assert "investor bot" in err.lower()

    def test_correct_role_passes(self, monkeypatch):
        monkeypatch.setenv("NEGOTIATE_SAFE_BOT_ROLE", "founder")
        assert rs._enforce_bot_role_pre_parse(
            "Negotiate a SAFE with Nora, $40M cap"
        ) is None

    def test_inv_code_alone_classifies_investor(self, monkeypatch):
        monkeypatch.setenv("NEGOTIATE_SAFE_BOT_ROLE", "founder")
        # Even without "investor" keyword, the INV-XXXXX code is a
        # strong signal this is a join.
        err = rs._enforce_bot_role_pre_parse("Joining INV-7K3X9, $40M cap")
        assert err is not None

    def test_pre_parse_classifies_join_phrasing_as_investor(self, monkeypatch):
        monkeypatch.setenv("NEGOTIATE_SAFE_BOT_ROLE", "founder")
        err = rs._enforce_bot_role_pre_parse("joining as investor")
        assert err is not None


# ─── post-parse role guard ─────────────────────────────────────────────────

class TestEnforceBotRolePostParse:
    def test_matching_role_passes(self, monkeypatch):
        monkeypatch.setenv("NEGOTIATE_SAFE_BOT_ROLE", "founder")
        assert rs._enforce_bot_role_post_parse("founder") is None

    def test_mismatched_role_blocked(self, monkeypatch):
        monkeypatch.setenv("NEGOTIATE_SAFE_BOT_ROLE", "founder")
        err = rs._enforce_bot_role_post_parse("investor")
        assert err is not None
        assert "FOUNDER" in err

    def test_no_enforcement_when_unset(self):
        assert rs._enforce_bot_role_post_parse("investor") is None

    def test_empty_parsed_role_does_not_trigger(self, monkeypatch):
        monkeypatch.setenv("NEGOTIATE_SAFE_BOT_ROLE", "founder")
        # If the parser couldn't determine a role, don't reject — let
        # the existing missing-fields check handle it.
        assert rs._enforce_bot_role_post_parse("") is None
        assert rs._enforce_bot_role_post_parse(None) is None


# ─── single-active-negotiation gate ────────────────────────────────────────

class TestHasActiveNegotiation:
    def test_no_state_no_pid_returns_false(self):
        ok, descriptor = rs._has_active_negotiation()
        assert ok is False
        assert descriptor is None

    def test_active_two_party_pointer_blocks(self, monkeypatch, _state_dir):
        state_store.write_state({
            "negotiation_id": "neg_active",
            "output_dir": "/tmp/safe_negotiate",
            "session_code": "INV-ACTIVE",
        })
        client = MagicMock()
        client.get_session.return_value = {
            "session_id": "session_neg_active",
            "status": "joined",
        }
        # Inject the client by patching the constructor used inside the func.
        with patch.object(rs, "SshsignSession", return_value=client):
            ok, descriptor = rs._has_active_negotiation()
        assert ok is True
        assert descriptor == "INV-ACTIVE"

    def test_terminal_pointer_does_not_block(self, monkeypatch, _state_dir):
        state_store.write_state({
            "negotiation_id": "neg_done",
            "output_dir": "/tmp/safe_negotiate",
            "session_code": "INV-DONE",
        })
        client = MagicMock()
        client.get_session.return_value = {
            "session_id": "session_neg_done",
            "status": "completed",
        }
        with patch.object(rs, "SshsignSession", return_value=client):
            ok, descriptor = rs._has_active_negotiation()
        assert ok is False

    def test_canceled_pointer_does_not_block(self, monkeypatch, _state_dir):
        state_store.write_state({
            "negotiation_id": "neg_x",
            "output_dir": "/tmp/safe_negotiate",
            "session_code": "INV-X",
        })
        client = MagicMock()
        client.get_session.return_value = {"status": "canceled"}
        with patch.object(rs, "SshsignSession", return_value=client):
            ok, _ = rs._has_active_negotiation()
        assert ok is False

    def test_sshsign_unreachable_does_not_block(self, _state_dir):
        from sshsign_session import SshsignSessionError
        state_store.write_state({
            "negotiation_id": "neg_x",
            "output_dir": "/tmp/safe_negotiate",
            "session_code": "INV-X",
        })
        client = MagicMock()
        client.get_session.side_effect = SshsignSessionError("network blip")
        with patch.object(rs, "SshsignSession", return_value=client):
            ok, _ = rs._has_active_negotiation()
        # Don't trap the user behind a transient transport failure.
        assert ok is False


# ─── integration: run_prepare blocks correctly ────────────────────────────

class TestRunPrepareWithGates:
    def test_wrong_bot_role_blocks_before_parse(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NEGOTIATE_SAFE_BOT_ROLE", "founder")
        monkeypatch.setenv("NEGOTIATE_SAFE_COUNTERPARTY_BOT", "InvBot")
        sender = MagicMock()
        # Mock identity check so it doesn't trip the welcome path.
        with patch.object(rs, "_identity_configured", return_value=True), \
             patch.object(rs, "extract_constraints") as parse, \
             patch.object(rs, "resolve_chat_id", return_value="123"):
            rc = rs.run_prepare(
                "Join INV-7K3X9 as investor, $40M cap",
                str(tmp_path / "out"),
                sender=sender,
            )
        assert rc == 1
        # Critical: parse_constraints was NEVER called — pre-parse gate
        # must intercept BEFORE the slow Claude round-trip.
        parse.assert_not_called()
        # Wrong-bot card was sent.
        msgs = [c.kwargs.get("message") or c.args[1] for c in sender.call_args_list]
        assert any("FOUNDER" in m for m in msgs)

    def test_active_negotiation_blocks_before_parse(
        self, tmp_path, monkeypatch, _state_dir,
    ):
        state_store.write_state({
            "negotiation_id": "neg_ongoing",
            "output_dir": "/tmp/safe_negotiate",
            "session_code": "INV-ONGOING",
        })
        client = MagicMock()
        client.get_session.return_value = {"status": "joined"}
        sender = MagicMock()
        with patch.object(rs, "_identity_configured", return_value=True), \
             patch.object(rs, "extract_constraints") as parse, \
             patch.object(rs, "SshsignSession", return_value=client), \
             patch.object(rs, "resolve_chat_id", return_value="123"):
            rc = rs.run_prepare(
                "Negotiate with Nora at Babes Fund",
                str(tmp_path / "out"),
                sender=sender,
            )
        assert rc == 1
        parse.assert_not_called()
        msgs = [c.kwargs.get("message") or c.args[1] for c in sender.call_args_list]
        assert any("INV-ONGOING" in m for m in msgs)
        assert any("cancel" in m.lower() for m in msgs)

    def test_post_parse_role_mismatch_blocks(
        self, tmp_path, monkeypatch, sample_constraints,
    ):
        """Catches the case where the regex misses an investor signal
        but parse_constraints classifies the role as investor anyway.
        """
        monkeypatch.setenv("NEGOTIATE_SAFE_BOT_ROLE", "founder")
        monkeypatch.setenv("NEGOTIATE_SAFE_COUNTERPARTY_BOT", "InvBot")
        sender = MagicMock()
        # Pre-parse regex won't match this ambiguous phrasing.
        msg = "Coordinating with Nora — see what she'll agree to"
        # Force parse_constraints to return role=investor.
        bad = dict(sample_constraints)
        bad["role"] = "investor"
        with patch.object(rs, "_identity_configured", return_value=True), \
             patch.object(rs, "extract_constraints", return_value=bad), \
             patch.object(rs, "resolve_chat_id", return_value="123"):
            rc = rs.run_prepare(msg, str(tmp_path / "out"), sender=sender)
        assert rc == 1
        # Confirm card must NOT have been pushed (no "👤 Negotiating
        # as founder" leak).
        msgs = [c.kwargs.get("message") or c.args[1] for c in sender.call_args_list]
        assert not any("Negotiating as" in m for m in msgs), (
            "post-parse gate let the wrong-role confirm card leak"
        )
        # Reject card was sent.
        assert any("FOUNDER" in m for m in msgs)
