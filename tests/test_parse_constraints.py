"""Tests for parse_constraints.py."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch


import parse_constraints as pc

SCRIPT = Path(__file__).parent.parent / "negotiate_safe" / "parse_constraints.py"


class TestExtractConstraints:
    def test_missing_mode_defaults_to_demo(self):
        r = pc._normalize_constraints({"role": "founder"})
        assert r["mode"] == "demo"
        assert r["session_code"] is None

    def test_unknown_mode_coerces_to_demo(self):
        r = pc._normalize_constraints({"role": "founder", "mode": "whatever"})
        assert r["mode"] == "demo"

    def test_session_code_forces_two_party_mode(self):
        r = pc._normalize_constraints({
            "role": "investor", "mode": "demo", "session_code": "INV-7K3X9",
        })
        assert r["mode"] == "two_party"
        assert r["session_code"] == "INV-7K3X9"

    def test_session_code_normalized_to_uppercase(self):
        r = pc._normalize_constraints({"role": "investor", "mode": "two_party", "session_code": "  inv-7k3x9 "})
        assert r["session_code"] == "INV-7K3X9"

    def test_empty_session_code_normalized_to_none(self):
        r = pc._normalize_constraints({"role": "founder", "mode": "two_party", "session_code": ""})
        assert r["session_code"] is None
        assert r["mode"] == "two_party"

    def test_two_party_without_code_is_valid_for_founder_creating(self):
        r = pc.extract_constraints("Live negotiation with Nora at SD Capital. Cap $20M-$30M. Discount 0%.")
        assert r["mode"] == "two_party"
        assert r["session_code"] is None


class TestCli:
    def test_cli_uses_deterministic_parser(self):
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--message",
                "Live negotiation with Nora Vassileva at SD Fund. Cap $30M to $40M, 10% discount, pro-rata required.",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        parsed = json.loads(result.stdout)
        assert parsed["role"] == "founder"
        assert parsed["mode"] == "two_party"
        assert parsed["valuation_cap_min"] == 30_000_000
        assert parsed["valuation_cap_max"] == 40_000_000
        assert parsed["investor_name"] == "Nora Vassileva"
        assert parsed["investor_firm"] == "SD Fund"

    def test_deterministic_parser_extracts_check_size_range(self):
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--message",
                (
                    "Live negotiation for Series Seed SAFE with Nora Vassileva "
                    "(SD Capital). Cap: $15M-$30M post. Check: $250k-$750k. "
                    "Pro rata: required. Discount: 0%"
                ),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        parsed = json.loads(result.stdout)
        assert parsed["mode"] == "two_party"
        assert parsed["investor_name"] == "Nora Vassileva"
        assert parsed["investor_firm"] == "SD Capital"
        assert parsed["valuation_cap_min"] == 15_000_000
        assert parsed["valuation_cap_max"] == 30_000_000
        assert parsed["investment_amount"] == 250_000.0
        assert parsed["investment_amount_min"] == 250_000.0
        assert parsed["investment_amount_max"] == 750_000.0
        assert parsed["discount_min"] == 0.0
        assert parsed["discount_max"] == 0.0

    def test_message_arg(self):
        """--message flag should be accepted as an alternative to stdin."""
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--message", "test"],
            capture_output=True,
            text=True,
        )
        assert "No message" not in result.stderr

    def test_message_file_arg(self, tmp_path):
        """--message-file should read from a file."""
        msg_file = tmp_path / "request.txt"
        msg_file.write_text("Negotiate my SAFE, cap $10M.")
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--message-file", str(msg_file)],
            capture_output=True,
            text=True,
        )
        assert "No message" not in result.stderr

    def test_output_file_arg(self, tmp_path, sample_constraints):
        """--output-file should write JSON to a file instead of stdout."""
        out_file = tmp_path / "constraints.json"
        with patch.object(pc, "extract_constraints", return_value=sample_constraints):
            argv = ["parse_constraints.py", "--message", "test", "--output-file", str(out_file)]
            with patch.object(sys, "argv", argv):
                rc = pc.main()
        assert rc == 0
        assert out_file.exists()
        assert json.loads(out_file.read_text()) == sample_constraints

    def test_no_message_at_all(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            input="",
            capture_output=True,
            text=True,
        )
        assert result.returncode == 2
        assert "No message" in result.stderr
