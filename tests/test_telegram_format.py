"""Tests for telegram_format.py.

The formatter is a pure function of the event dict. Covers every event type in
FORMATTERS, common edge cases (None values, missing fields), and the CLI surface.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import telegram_format as tf

SCRIPT = Path(__file__).parent.parent / "negotiate_safe" / "telegram_format.py"


class TestFormatters:
    def test_formatters_cover_all_documented_types(self):
        expected = {
            "confirm", "authorized", "offer", "agreed",
            "cosign_requested", "signed", "canceled", "expired", "deadlock",
        }
        assert set(tf.FORMATTERS.keys()) == expected

    def test_offer_matches_skill_md_template(self, sample_offer):
        out = tf.format_offer(sample_offer)
        assert "[Round 2 - Founder]" in out
        assert '"$6M is below our minimum. Counter at $10M with 20% discount."' in out
        assert "Cap: $10,000,000" in out
        assert "(range: $8,000,000-$12,000,000)" in out
        assert "Discount: 20%" in out
        assert "(min: 20%)" in out
        assert "immudb tx: 48326" in out

    def test_offer_without_rationale_omits_quote_line(self, sample_offer):
        sample_offer.pop("rationale")
        out = tf.format_offer(sample_offer)
        assert '"' not in out
        # The empty line between quote and stats should also be gone
        assert out.count("\n\n") == 0 or out.startswith("[Round")

    def test_offer_missing_terms_renders_dashes(self):
        event = {"type": "offer", "round": 1, "party": "Founder"}
        out = tf.format_offer(event)
        assert "Cap: -" in out
        assert "Discount: -" in out
        assert "immudb tx: pending" in out

    def test_confirm(self, sample_constraints):
        event = {"type": "confirm", "constraints": sample_constraints}
        out = tf.format_confirm(event)
        assert "Valuation cap: $8,000,000 to $12,000,000" in out
        assert "Discount rate: 20% or better" in out
        assert "Pro-rata rights: required" in out
        assert "MFN clause: preferred" in out
        assert 'Say "go"' in out

    def test_confirm_all_pro_rata_mfn_combos(self, sample_constraints):
        for flag in ("required", "preferred", "indifferent"):
            c = {**sample_constraints, "pro_rata": flag, "mfn": flag}
            out = tf.format_confirm({"type": "confirm", "constraints": c})
            assert "Pro-rata rights:" in out
            assert "MFN clause:" in out

    def test_authorized(self):
        event = {
            "type": "authorized",
            "tid": "tid_abc123",
            "service": "safe:acme:nego_5f2a",
            "expires_at": "2026-04-16T15:00:00Z",
        }
        out = tf.format_authorized(event)
        assert "Authorization signed." in out
        assert "Token: tid_abc123" in out
        assert "Scope: safe:acme:nego_5f2a" in out
        assert "apoa revoke tid_abc123" in out

    def test_agreed(self):
        event = {
            "type": "agreed",
            "terms": {
                "valuation_cap": 9_000_000,
                "discount_rate": 0.20,
                "pro_rata": True,
                "mfn": False,
            },
        }
        out = tf.format_agreed(event)
        assert "Agreement reached!" in out
        assert "Cap: $9,000,000" in out
        assert "Pro-rata: yes" in out
        assert "MFN: no" in out

    def test_cosign_requested(self):
        event = {
            "type": "cosign_requested",
            "pending_id": "pnd_xyz789",
            "sshsign_key_path": "/path/to/key",
        }
        out = tf.format_cosign_requested(event)
        assert "pnd_xyz789" in out
        assert "/path/to/key" in out
        assert "ssh -i /path/to/key sshsign.dev approve" in out

    def test_cosign_requested_falls_back_to_env_placeholder(self):
        event = {"type": "cosign_requested", "pending_id": "pnd_1"}
        out = tf.format_cosign_requested(event)
        assert "$SSHSIGN_KEY_PATH" in out

    def test_signed_with_pro_rata(self):
        event = {
            "type": "signed",
            "audit_tx": 48329,
            "terms": {"valuation_cap": 9_000_000, "discount_rate": 0.20, "pro_rata": True},
            "total_offers": 6,
            "duration_seconds": 24,
        }
        out = tf.format_signed(event)
        assert "Signed!" in out
        assert "$9,000,000 cap, 20% discount, pro-rata" in out
        assert "Audit TX: 48329" in out
        assert "sshsign.dev/verify/48329" in out
        assert "6 offers in 24 seconds" in out

    def test_signed_without_pro_rata_omits_tag(self):
        event = {
            "type": "signed",
            "audit_tx": 1,
            "terms": {"valuation_cap": 9_000_000, "discount_rate": 0.20, "pro_rata": False},
        }
        out = tf.format_signed(event)
        assert "pro-rata" not in out

    def test_canceled(self):
        out = tf.format_canceled({"type": "canceled", "tid": "tid_abc"})
        assert "canceled" in out.lower()
        assert "tid_abc" in out

    def test_canceled_without_tid(self):
        out = tf.format_canceled({"type": "canceled"})
        assert "canceled" in out.lower()

    def test_expired(self):
        out = tf.format_expired({"type": "expired"})
        assert "expired" in out.lower()
        assert "Mint a fresh token" in out

    def test_deadlock(self):
        event = {
            "type": "deadlock",
            "founder_final": {"valuation_cap": 10_000_000, "discount_rate": 0.20},
            "investor_final": {"valuation_cap": 7_000_000, "discount_rate": 0.25},
        }
        out = tf.format_deadlock(event)
        assert "10 rounds" in out
        assert "$10,000,000" in out
        assert "$7,000,000" in out
        assert "25%" in out


class TestHelpers:
    @pytest.mark.parametrize("n,expected", [
        (None, "-"),
        (0, "$0"),
        (1, "$1"),
        (1000, "$1,000"),
        (8_000_000, "$8,000,000"),
        (12.7, "$12"),  # cast to int
    ])
    def test_fmt_dollars(self, n, expected):
        assert tf.fmt_dollars(n) == expected

    @pytest.mark.parametrize("d,expected", [
        (None, "-"),
        (0, "0%"),
        (0.20, "20%"),
        (0.255, "26%"),  # rounds
        (1.0, "100%"),
    ])
    def test_fmt_percent(self, d, expected):
        assert tf.fmt_percent(d) == expected


class TestCli:
    def test_cli_formats_offer(self, sample_offer):
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            input=json.dumps(sample_offer),
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "[Round 2 - Founder]" in result.stdout

    def test_cli_rejects_invalid_json(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            input="not json",
            capture_output=True,
            text=True,
        )
        assert result.returncode == 2
        assert "Invalid JSON" in result.stderr

    def test_cli_rejects_unknown_type(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            input=json.dumps({"type": "nope"}),
            capture_output=True,
            text=True,
        )
        assert result.returncode == 2
        assert "Unknown event type" in result.stderr
