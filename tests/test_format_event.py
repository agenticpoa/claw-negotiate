"""Tests for format_event.py.

Covers each formatter, the format_event dispatcher, shared helpers, and the CLI
surface. Event shapes mirror what upstream agenticpoa/negotiate emits via
`--json-events`; wrapper-emitted events (confirm, authorized, signed) use the
shapes our scripts produce.

Output is Telegram legacy Markdown (see format_event.py module docstring).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import format_event as fe

SCRIPT = Path(__file__).parent.parent / "negotiate_safe" / "format_event.py"


class TestFormatters:
    def test_formatters_cover_expected_types(self):
        assert set(fe.FORMATTERS.keys()) == {
            "confirm", "authorized",
            "offer", "counter", "accept",
            "outcome", "signing", "signed",
            "profile",
            "invitation", "waiting", "counterparty_joined", "invitation_expired",
            "canceled_before_deal_initiator", "canceled_before_deal_observer",
            "canceled_after_deal_initiator", "canceled_after_deal_observer",
            "rescinded_after_sign_initiator", "rescinded_after_sign_observer",
            "cancel_completed_refused",
            "propose_new_terms",
            "session_expired",
            # Phase 8 (K1): group-mode bind UX
            "go_live", "group_bound",
            "bind_wrong_user", "bind_wrong_chat_type",
            "bind_unknown_code", "bind_already_bound",
            # P7-5: durable founder-wait via OpenClaw cron
            "founder_resumed",
            "investor_waiting_for_founder",
            "investor_waiting_heartbeat",
            "investor_both_online",
            "investor_wake_timeout",
            "investor_session_ended",
            # Single-active + role gates
            "active_negotiation_block",
            # Inverted-invitation
            "create_group_for_founder",
        }

    # ---- confirm (our emit) ----

    def test_confirm_new_copy(self, sample_constraints):
        event = {"type": "confirm", "constraints": sample_constraints}
        out = fe.format_confirm(event)
        assert "Please review the terms below and confirm:" in out
        assert "**Valuation cap:** $8M – $12M" in out
        assert "**Discount:** 20% or better" in out
        assert "**Pro-rata rights:** required" in out
        assert "**MFN clause:** preferred" in out
        assert "reply **GO**" in out
        assert "please send your edits" in out

    def test_confirm_drops_primer_section(self, sample_constraints):
        out = fe.format_confirm({"type": "confirm", "constraints": sample_constraints})
        assert "Quick primer" not in out
        assert "Pro-rata = right" not in out
        assert "MFN = you automatically" not in out

    def test_confirm_all_pro_rata_mfn_combos(self, sample_constraints):
        for flag in ("required", "preferred", "indifferent"):
            c = {**sample_constraints, "pro_rata": flag, "mfn": flag}
            out = fe.format_confirm({"type": "confirm", "constraints": c})
            assert "**Pro-rata rights:**" in out
            assert "**MFN clause:**" in out

    def test_confirm_shows_founder_role_header(self, sample_constraints):
        c = {**sample_constraints, "role": "founder"}
        out = fe.format_confirm({"type": "confirm", "constraints": c})
        assert "Negotiating as **founder**" in out
        assert "\U0001f464" in out  # 👤

    def test_confirm_shows_investor_role_header(self, sample_constraints):
        c = {**sample_constraints, "role": "investor"}
        out = fe.format_confirm({"type": "confirm", "constraints": c})
        assert "Negotiating as **investor**" in out
        assert "\U0001f4bc" in out  # 💼

    def test_confirm_defaults_to_founder_when_role_missing(self, sample_constraints):
        c = {k: v for k, v in sample_constraints.items() if k != "role"}
        out = fe.format_confirm({"type": "confirm", "constraints": c})
        assert "Negotiating as **founder**" in out

    def test_confirm_founder_identity_block(self, sample_constraints):
        c = {
            **sample_constraints,
            "role": "founder",
            "founder_name": "Jane Doe",
            "founder_title": "CEO",
            "company_name": "Acme Corp",
            "investor_name": "Mark Stone",
            "investor_firm": "Bay Capital",
        }
        out = fe.format_confirm({"type": "confirm", "constraints": c})
        assert "**You:** Jane Doe, CEO, of Acme Corp" in out
        assert "**Counterparty:** Mark Stone, at Bay Capital" in out

    def test_confirm_investor_identity_block(self, sample_constraints):
        c = {
            **sample_constraints,
            "role": "investor",
            "founder_name": "Dr. Rivera",
            "founder_title": "CEO",
            "company_name": "QuantumLabs",
            "investor_name": "Alex Chen",
            "investor_firm": "Blue Fund",
        }
        out = fe.format_confirm({"type": "confirm", "constraints": c})
        assert "**You:** Alex Chen, at Blue Fund" in out
        assert "**Counterparty:** Dr. Rivera, CEO, of QuantumLabs" in out

    def test_confirm_omits_missing_identity_fields(self, sample_constraints):
        # Only company_name known on the founder side; only firm on investor side
        c = {
            **sample_constraints,
            "role": "founder",
            "founder_name": None,
            "founder_title": None,
            "company_name": "Acme Corp",
            "investor_name": None,
            "investor_firm": "Bay Capital",
        }
        out = fe.format_confirm({"type": "confirm", "constraints": c})
        assert "**You:** Acme Corp" in out  # just the company when no name
        assert "**Counterparty:** Bay Capital" in out  # just the firm when no name

    def test_confirm_drops_identity_lines_when_nothing_known(self, sample_constraints):
        c = {
            **sample_constraints,
            "role": "founder",
            "founder_name": None,
            "founder_title": None,
            "company_name": None,
            "investor_name": None,
            "investor_firm": None,
        }
        out = fe.format_confirm({"type": "confirm", "constraints": c})
        assert "**You:**" not in out
        assert "**Counterparty:**" not in out
        assert "Negotiating as **founder**" in out  # role header still shown

    # ---- authorized (our emit) ----

    def test_authorized(self):
        event = {
            "type": "authorized",
            "constraints": {
                "valuation_cap_min": 8_000_000,
                "valuation_cap_max": 12_000_000,
                "discount_min": 0.20,
                "pro_rata": "required",
                "mfn": "preferred",
            },
            "ttl_hours": 1,
        }
        out = fe.format_authorized(event)
        assert "Your agent's authorization is set" in out
        assert "$8M – $12M" in out
        assert "cannot go higher" in out
        assert "20%" in out
        assert "Pro-rata rights: required" in out
        assert "MFN clause: preferred" in out
        assert "cancel" in out.lower()
        assert "APOA" in out  # cited in footer, not headline
        # Token ID should NOT be shown — user-first framing
        assert "tid_" not in out

    def test_authorized_handles_missing_fields_gracefully(self):
        out = fe.format_authorized({"type": "authorized", "constraints": {}})
        assert "Your agent's authorization is set" in out
        # No bounds to show → skip those lines, but footer is still present
        assert "APOA" in out

    # ---- offer/counter/accept (upstream schema) ----

    def test_offer_upstream_shape(self):
        event = {
            "type": "offer",
            "round": 2,
            "party": "founder",
            "message": "Counter at 10M, 20% discount.",
            "terms": {
                "valuation_cap": 10_000_000,
                "discount_rate": 0.20,
                "pro_rata": True,
                "mfn": False,
            },
            "immudb_tx": 48326,
        }
        out = fe.format_offer(event)
        assert out.startswith("\U0001f464 **Round 2 — Founder**")  # 👤
        assert '"Counter at 10M, 20% discount."' in out
        assert "• Cap: **$10M**" in out
        assert "• Discount: **20%**" in out
        assert "• Pro-rata: yes\n• MFN: no" in out

    def test_counter_uses_founder_icon_and_label(self):
        event = {"type": "counter", "round": 3, "party": "founder",
                 "terms": {"valuation_cap": 8_000_000, "discount_rate": 0.15}}
        out = fe.format_offer(event)
        assert out.startswith("\U0001f464 **Round 3 — Founder**")  # 👤 (counter uses same header)

    def test_investor_uses_briefcase_icon(self):
        event = {"type": "counter", "round": 3, "party": "investor",
                 "terms": {"valuation_cap": 8_000_000, "discount_rate": 0.15}}
        out = fe.format_offer(event)
        assert out.startswith("\U0001f4bc **Round 3 — Investor**")  # 💼

    def test_demo_mode_labels_ai_counterparty_as_ai(self):
        """Solo-demo: the AI side of the negotiation gets a '(AI)' suffix
        in the offer header so the user never mistakes it for a real
        counterparty. The user's own side does NOT get the suffix."""
        event = {"type": "offer", "round": 2, "party": "investor",
                 "terms": {"valuation_cap": 9_000_000, "discount_rate": 0.20}}
        # User is playing founder → investor side is AI
        out = fe.format_offer(event, constraints={"role": "founder", "mode": "demo"})
        assert "**Round 2 — Investor (AI)**" in out

        # User is playing investor → investor side is the human user
        out = fe.format_offer(event, constraints={"role": "investor", "mode": "demo"})
        assert "**Round 2 — Investor**" in out
        assert "(AI)" not in out

    def test_two_party_mode_never_shows_ai_suffix(self):
        """Both sides are real humans in two_party mode — no AI suffix
        regardless of party."""
        event = {"type": "counter", "round": 3, "party": "investor",
                 "terms": {"valuation_cap": 8_000_000, "discount_rate": 0.15}}
        out = fe.format_offer(event, constraints={"role": "founder", "mode": "two_party"})
        assert "(AI)" not in out

    def test_accept_renders_deal_celebration(self):
        event = {"type": "accept", "round": 5, "party": "founder",
                 "terms": {"valuation_cap": 9_000_000, "discount_rate": 0.20,
                           "pro_rata": True, "mfn": False}}
        out = fe.format_offer(event)
        assert out.startswith("\U0001f91d **Deal!**")  # 🤝
        assert "Terms agreed:" in out
        assert "• Cap: **$9M**" in out
        assert "• Discount: **20%**" in out
        assert "• Pro-rata: yes" in out
        assert "• MFN: no" in out
        # Not a round card — no "Round" header
        assert "Round 5" not in out

    def test_offer_without_message_omits_quote(self):
        event = {"type": "offer", "round": 1, "party": "founder",
                 "terms": {"valuation_cap": 10_000_000, "discount_rate": 0.20}}
        out = fe.format_offer(event)
        assert '"' not in out

    def test_offer_with_constraints_shows_range(self, sample_constraints):
        event = {
            "type": "offer",
            "round": 1,
            "party": "founder",
            "terms": {"valuation_cap": 10_000_000, "discount_rate": 0.20},
        }
        out = fe.format_offer(event, constraints=sample_constraints)
        assert "(your range: $8M–$12M)" in out
        assert "(your min: 20%)" in out

    def test_offer_without_constraints_no_range(self):
        event = {"type": "offer", "round": 1, "party": "founder",
                 "terms": {"valuation_cap": 10_000_000, "discount_rate": 0.20}}
        out = fe.format_offer(event)
        assert "(your range:" not in out
        assert "(your min:" not in out

    def test_offer_two_party_suppresses_range(self, sample_constraints):
        """PRIVACY regression: in two-party mode round cards land in
        the shared group. The "your range" / "your min" hints would
        leak each side's private bounds to the counterparty. Must
        be suppressed even when constraints are passed.
        """
        two_party_constraints = dict(sample_constraints, mode="two_party")
        event = {
            "type": "counter",
            "round": 1,
            "party": "investor",
            "terms": {"valuation_cap": 20_000_000, "discount_rate": 0.10},
        }
        out = fe.format_offer(event, constraints=two_party_constraints)
        assert "(your range:" not in out, f"PRIVACY LEAK: {out}"
        assert "(your min:" not in out, f"PRIVACY LEAK: {out}"

    def test_offer_missing_terms_renders_dashes(self):
        event = {"type": "offer", "round": 1, "party": "founder"}
        out = fe.format_offer(event)
        assert "• Cap: **-**" in out
        assert "• Discount: **-**" in out

    def test_offer_whitespace_message_ignored(self):
        event = {"type": "offer", "round": 1, "party": "founder",
                 "message": "   ", "terms": {}}
        out = fe.format_offer(event)
        assert '"' not in out

    def test_offer_escapes_markdown_in_message(self):
        event = {
            "type": "offer",
            "round": 1,
            "party": "founder",
            "message": "We want *bold* and _italic_ and `code`",
            "terms": {"valuation_cap": 10_000_000, "discount_rate": 0.20},
        }
        out = fe.format_offer(event)
        assert "\\*bold\\*" in out
        assert "\\_italic\\_" in out
        assert "\\`code\\`" in out

    # ---- outcome (upstream schema) ----

    def test_outcome_accepted_returns_none(self):
        """Accepted outcome is skipped — the preceding `accept` event already
        showed the terms and 'Deal!' would be redundant."""
        event = {
            "type": "outcome",
            "result": "accepted",
            "terms": {"valuation_cap": 9_000_000, "discount_rate": 0.20},
            "duration_seconds": 24.0,
        }
        assert fe.format_outcome(event) is None

    def test_outcome_max_rounds(self):
        out = fe.format_outcome({"type": "outcome", "result": "max_rounds"})
        assert "No agreement reached" in out
        assert "didn't overlap" in out.lower()

    def test_outcome_max_rounds_with_constraints_includes_range(self, sample_constraints):
        """No-ZOPA copy should cite the user's own cap range so they know
        which side of the gap was theirs."""
        out = fe.format_outcome(
            {"type": "outcome", "result": "max_rounds"},
            constraints=sample_constraints,
        )
        assert "$8M – $12M" in out
        assert "didn't overlap" in out.lower()

    def test_outcome_rejected(self):
        out = fe.format_outcome({"type": "outcome", "result": "rejected"})
        assert "rejected" in out.lower()

    def test_outcome_unknown_result_returns_none(self):
        out = fe.format_outcome({"type": "outcome", "result": "exploded"})
        assert out is None

    # ---- signing (upstream schema) ----

    def test_signing_with_url(self):
        event = {
            "type": "signing",
            "pending_id": "pnd_abc",
            "approval_url": "https://sshsign.dev/approve/pnd_abc?callback=x",
            "requires_signature": True,
        }
        out = fe.format_signing(event)
        assert "\u270d\ufe0f **Almost done — your signature, please.**" in out  # ✍️
        assert "Tap the link below and draw your signature." in out
        assert "within a minute of signing" in out
        # URL is emitted verbatim. Escaping underscores was breaking the link
        # on Telegram → sshsign (Telegram kept the backslash in the hyperlink
        # target, yielding "Invalid pending ID" on sshsign's side).
        assert "https://sshsign.dev/approve/pnd_abc?callback=x" in out
        assert "pnd\\_abc" not in out

    def test_signing_without_url_falls_back_to_ssh_command(self):
        event = {"type": "signing", "pending_id": "pnd_abc"}
        out = fe.format_signing(event)
        assert "ssh sshsign.dev approve --id pnd_abc" in out

    def test_signing_minimal(self):
        out = fe.format_signing({"type": "signing"})
        assert "signature" in out.lower()

    # ---- signed (synthesized by us after envelope=approved) ----

    def test_signed_with_terms(self):
        event = {
            "type": "signed",
            "terms": {"valuation_cap": 9_000_000, "discount_rate": 0.20, "pro_rata": True},
        }
        out = fe.format_signed(event)
        assert out.startswith("\u2705 **Signed & sealed.**")  # ✅
        assert "Here's your executed SAFE:" in out
        assert "• Cap: $9M" in out
        assert "• Discount: 20%" in out
        assert "• Pro-rata: yes" in out
        assert "audit trail" in out.lower()

    def test_signed_without_terms(self):
        out = fe.format_signed({"type": "signed"})
        assert "**Signed & sealed.**" in out
        assert "audit trail" in out.lower()


class TestFormatInvitation:
    def test_includes_session_code_and_founder_handle(self):
        """Inverted-invitation card: copy-pasteable block must contain
        BOTH the session code and the founder bot handle, so the
        investor can paste the literal block to their own bot and the
        parser extracts both fields cleanly."""
        out = fe.format_invitation({
            "type": "invitation",
            "session_code": "INV-7K3X9",
            "founder_bot_handle": "@alice_negotiator_bot",
            "ttl_hours": 24,
            "counterparty_label": "Alex Smith, Central Park Labs",
        })
        assert "INV-7K3X9" in out
        assert "@alice_negotiator_bot" in out
        assert "Alex Smith, Central Park Labs" in out
        # The "Joining INV-X via @handle" anchor phrase is what the
        # investor's parser keys on. Drop this and the design breaks.
        assert "Joining INV-7K3X9 via @alice_negotiator_bot" in out
        # Should hint at sharing channels, not just "copy this".
        assert any(ch in out for ch in ("Signal", "SMS", "email"))
        # Should tell the user what they can do while waiting.
        assert "cancel" in out.lower()

    def test_generic_counterparty_label_when_missing(self):
        out = fe.format_invitation({
            "type": "invitation",
            "session_code": "INV-X",
            "founder_bot_handle": "@bot",
        })
        assert "your investor" in out.lower() or "your counterparty" in out.lower()

    def test_normalizes_handle_without_at_prefix(self):
        out = fe.format_invitation({
            "type": "invitation",
            "session_code": "INV-X",
            "founder_bot_handle": "raw_bot_no_at_prefix",
        })
        # The card normalizes to @-prefixed for display consistency.
        assert "@raw_bot_no_at_prefix" in out

    def test_silent_generic_fallback_when_founder_bot_handle_missing(self):
        """When founder_bot_handle is empty the card falls back to the
        generic 'Joining INV-X as investor, …' template — no scary
        end-user-facing warning. Misconfig is an ops problem (logged
        to stderr from the call site), not an end-user one."""
        out = fe.format_invitation({
            "type": "invitation",
            "session_code": "INV-X",
            "founder_bot_handle": "",
        })
        # No "wasn't configured" / warning emoji leaked to user.
        assert "wasn't configured" not in out.lower()
        assert "⚠" not in out
        # Generic template still references the code so the investor
        # has SOMETHING to type.
        assert "INV-X" in out
        assert "as investor" in out


class TestFormatWaiting:
    def test_shows_elapsed_minutes_and_countdown(self):
        out = fe.format_waiting({"elapsed_minutes": 15, "remaining_hours": 23.75})
        assert "15 min" in out
        assert "expires" in out.lower()
        # Countdown shown as HH:MM rather than the old "~Xh" rough hours
        assert "23:" in out  # 23:45

    def test_omits_remaining_when_missing(self):
        out = fe.format_waiting({"elapsed_minutes": 30})
        assert "30 min" in out
        assert "expires" not in out.lower()

    def test_countdown_near_expiration(self):
        """Small remaining time should still render meaningfully."""
        out = fe.format_waiting({"elapsed_minutes": 1435, "remaining_hours": 0.083})
        # 5 minutes remaining → "00:04" or "00:05"
        assert "00:0" in out


class TestFmtHhmm:
    def test_basic(self):
        assert fe._fmt_hhmm(0) == "00:00"
        assert fe._fmt_hhmm(59) == "00:00"
        assert fe._fmt_hhmm(60) == "00:01"
        assert fe._fmt_hhmm(3600) == "01:00"
        assert fe._fmt_hhmm(3661) == "01:01"
        assert fe._fmt_hhmm(23.5 * 3600) == "23:30"

    def test_negative_clamps_to_zero(self):
        assert fe._fmt_hhmm(-100) == "00:00"


class TestFormatSessionExpired:
    def test_distinguished_from_invitation_expired(self):
        """Mid-flight expiration differs from pre-join: the user already
        had a counterparty, negotiated, maybe even signed."""
        out = fe.format_session_expired({})
        assert "expired" in out.lower()
        assert "mid-flight" in out.lower()
        assert "APOA" in out  # cite that authorization is what expired
        assert "No SAFE was executed" in out


class TestFormatCounterpartyJoined:
    def test_names_counterparty(self):
        out = fe.format_counterparty_joined({"counterparty_label": "Mark Stone"})
        assert "Mark Stone" in out
        assert "joined" in out.lower()


class TestFormatInvitationExpired:
    def test_tells_user_how_to_retry(self):
        out = fe.format_invitation_expired({})
        assert "expired" in out.lower()
        assert "negotiate my SAFE" in out or "try again" in out.lower()


class TestFormatProfile:
    def test_empty_profile_prompts_setup(self):
        out = fe.format_profile({"profile": {}})
        assert "profile is empty" in out.lower()
        assert "set it up" in out.lower() or "setup" in out.lower()

    def test_founder_side_only(self):
        out = fe.format_profile({"profile": {
            "founder_name": "Juan Figuera", "founder_title": "CEO",
            "company_name": "APOA Inc",
        }})
        assert "Your saved profile" in out
        assert "**Founder side**" in out
        assert "**Juan Figuera**" in out
        assert "CEO" in out
        assert "APOA Inc" in out
        assert "**Investor side**" not in out
        assert "update" in out.lower()

    def test_investor_side_only(self):
        out = fe.format_profile({"profile": {
            "investor_name": "Mark Stone", "investor_firm": "Blue Fund",
        }})
        assert "**Investor side**" in out
        assert "**Mark Stone**" in out
        assert "Blue Fund" in out
        assert "**Founder side**" not in out

    def test_both_sides_shown(self):
        out = fe.format_profile({"profile": {
            "founder_name": "Juan", "company_name": "APOA",
            "investor_name": "Mark", "investor_firm": "Blue Fund",
        }})
        assert "**Founder side**" in out
        assert "**Investor side**" in out

    def test_missing_fields_silently_dropped(self):
        out = fe.format_profile({"profile": {"founder_name": "Juan"}})
        # Only name line, no empty title/company slots
        assert "Juan" in out
        assert "Title:" not in out
        assert "Company:" not in out


class TestCancellationCards:
    def test_canceled_before_deal_initiator(self):
        # New format: includes session code prominently for clarity.
        out = fe.format_canceled_before_deal_initiator({"session_code": "INV-7K3X9"})
        assert "INV-7K3X9" in out
        assert "canceled" in out.lower()
        assert "APOA" in out

    def test_canceled_before_deal_initiator_no_code(self):
        # Falls back to generic phrasing when session code missing.
        out = fe.format_canceled_before_deal_initiator({})
        assert "canceled" in out.lower()
        assert "APOA" in out

    def test_canceled_before_deal_observer(self):
        out = fe.format_canceled_before_deal_observer({"by": "Jane"})
        assert "Jane stopped negotiating" in out

    def test_canceled_before_deal_observer_default_label(self):
        out = fe.format_canceled_before_deal_observer({})
        assert "The other party" in out

    def test_canceled_after_deal_initiator(self):
        out = fe.format_canceled_after_deal_initiator({})
        assert "You revoked the agreed deal" in out
        assert "before signing" in out

    def test_canceled_after_deal_observer(self):
        out = fe.format_canceled_after_deal_observer({"by": "Jane"})
        assert "Jane revoked the agreed deal" in out
        assert "No SAFE will be executed" in out

    def test_rescinded_after_sign_initiator(self):
        out = fe.format_rescinded_after_sign_initiator({"session_code": "INV-7K3X9"})
        assert "INV-7K3X9" in out
        assert "rescinded" in out.lower()
        assert "signature stays on record" in out
        assert "will NOT execute" in out

    def test_rescinded_after_sign_initiator_no_code(self):
        out = fe.format_rescinded_after_sign_initiator({})
        assert "rescinded" in out.lower()
        assert "signature stays on record" in out

    def test_rescinded_after_sign_observer(self):
        out = fe.format_rescinded_after_sign_observer({"by": "Jane"})
        assert "Jane rescinded after signing" in out
        assert "SAFE will NOT execute" in out

    def test_cancel_completed_refused(self):
        out = fe.format_cancel_completed_deal_refused({})
        assert "already executed" in out
        assert "rescission" in out


class TestDispatcher:
    def test_dispatch_offer(self):
        event = {"type": "offer", "round": 1, "party": "founder",
                 "terms": {"valuation_cap": 10_000_000, "discount_rate": 0.20}}
        out = fe.format_event(event)
        assert out is not None
        assert "**Round 1 — Founder**" in out

    def test_dispatch_offer_forwards_constraints(self, sample_constraints):
        event = {"type": "offer", "round": 1, "party": "founder",
                 "terms": {"valuation_cap": 10_000_000, "discount_rate": 0.20}}
        out = fe.format_event(event, constraints=sample_constraints)
        assert "(your range: $8M–$12M)" in out

    def test_dispatch_counter(self):
        event = {"type": "counter", "round": 2, "party": "investor",
                 "terms": {"valuation_cap": 8_000_000, "discount_rate": 0.15}}
        out = fe.format_event(event)
        assert "**Round 2 — Investor**" in out

    def test_dispatch_accept(self):
        event = {"type": "accept", "round": 4, "party": "founder",
                 "terms": {"valuation_cap": 9_000_000, "discount_rate": 0.20}}
        out = fe.format_event(event)
        # Accept renders as the Deal celebration, not a round card
        assert "Deal!" in out

    def test_dispatch_outcome(self):
        out = fe.format_event({"type": "outcome", "result": "max_rounds"})
        assert "No agreement" in out

    def test_dispatch_outcome_forwards_constraints(self, sample_constraints):
        out = fe.format_event(
            {"type": "outcome", "result": "max_rounds"},
            constraints=sample_constraints,
        )
        assert "$8M – $12M" in out

    def test_dispatch_propose_new_terms(self):
        out = fe.format_event({
            "type": "propose_new_terms",
            "counterparty_label": "Jane Doe",
        })
        assert "Try again" in out
        assert "Jane Doe" in out


class TestProposeNewTerms:
    def test_copy_basic(self):
        out = fe.format_propose_new_terms({"counterparty_label": "Jane Doe"})
        assert "Try again" in out
        assert "Jane Doe" in out
        assert "Negotiate again" in out  # example line

    def test_no_counterparty_label(self):
        out = fe.format_propose_new_terms({})
        assert "Try again" in out
        # Should not have a trailing " with ..."
        assert "I'll start a new negotiation." in out

    def test_dispatch_signing(self):
        out = fe.format_event({
            "type": "signing",
            "approval_url": "https://sshsign.dev/x",
            "pending_id": "pnd_1",
        })
        assert "signature" in out.lower()

    def test_dispatch_signed(self):
        out = fe.format_event({"type": "signed"})
        assert "Signed" in out

    def test_dispatch_confirm(self, sample_constraints):
        out = fe.format_event({"type": "confirm", "constraints": sample_constraints})
        assert "Please review the terms below" in out

    def test_dispatch_authorized(self):
        out = fe.format_event({
            "type": "authorized",
            "constraints": {
                "valuation_cap_min": 8_000_000,
                "valuation_cap_max": 12_000_000,
                "discount_min": 0.20,
                "pro_rata": "required",
                "mfn": "indifferent",
            },
        })
        assert "Your agent's authorization is set" in out

    def test_dispatch_unknown_returns_none(self):
        assert fe.format_event({"type": "mystery"}) is None

    def test_dispatch_missing_type_returns_none(self):
        assert fe.format_event({}) is None

    def test_dispatch_unknown_outcome_result_returns_none(self):
        assert fe.format_event({"type": "outcome", "result": "exploded"}) is None


class TestHelpers:
    @pytest.mark.parametrize("n,expected", [
        (None, "-"),
        (0, "$0"),
        (1, "$1"),
        (1000, "$1,000"),
        (9999, "$9,999"),
        (10_000, "$10K"),
        (500_000, "$500K"),
        (8_000_000, "$8M"),
        (12_500_000, "$12.5M"),
        (100_000_000, "$100M"),
        (12.7, "$12"),
        (-8_000_000, "-$8M"),
    ])
    def test_fmt_dollars(self, n, expected):
        assert fe.fmt_dollars(n) == expected

    @pytest.mark.parametrize("d,expected", [
        (None, "-"),
        (0, "0%"),
        (0.20, "20%"),
        (0.255, "26%"),
        (1.0, "100%"),
    ])
    def test_fmt_percent(self, d, expected):
        assert fe.fmt_percent(d) == expected

    def test_escape_md_handles_all_reserved_chars(self):
        assert fe._escape_md("*_`[") == "\\*\\_\\`\\["


class TestCli:
    def test_cli_formats_upstream_offer(self):
        event = {
            "type": "offer",
            "round": 2,
            "party": "founder",
            "terms": {"valuation_cap": 10_000_000, "discount_rate": 0.20},
            "message": "Counter at 10M",
        }
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            input=json.dumps(event),
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "**Round 2 — Founder**" in result.stdout

    def test_cli_formats_outcome(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            input=json.dumps({"type": "outcome", "result": "max_rounds"}),
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "No agreement" in result.stdout

    def test_cli_rejects_invalid_json(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            input="not json",
            capture_output=True,
            text=True,
        )
        assert result.returncode == 2
        assert "Invalid JSON" in result.stderr

    def test_cli_rejects_non_object(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            input="[]",
            capture_output=True,
            text=True,
        )
        assert result.returncode == 2
        assert "JSON object" in result.stderr

    def test_cli_rejects_unknown_type(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            input=json.dumps({"type": "nope"}),
            capture_output=True,
            text=True,
        )
        assert result.returncode == 2
        assert "Unknown event type" in result.stderr
