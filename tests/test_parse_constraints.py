"""Tests for parse_constraints.py.

The Anthropic client is mocked. Focus: response handling, JSON parsing edge
cases, CLI exit codes. Live-API behavior is covered in integration tests.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import parse_constraints as pc

SCRIPT = Path(__file__).parent.parent / "negotiate_safe" / "parse_constraints.py"


@pytest.fixture(autouse=True)
def _default_anthropic_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")


def make_mock_client(response_text: str) -> MagicMock:
    """Build a mock Anthropic client that returns response_text from messages.create()."""
    client = MagicMock()
    client.messages.create.return_value = SimpleNamespace(
        content=[SimpleNamespace(text=response_text)]
    )
    return client


class TestExtractConstraints:
    def test_happy_path(self, sample_constraints):
        response_text = json.dumps(sample_constraints)
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}), \
             patch("anthropic.Anthropic", return_value=make_mock_client(response_text)):
            result = pc.extract_constraints("Negotiate my SAFE with an $8M-$12M cap.")
        assert result == sample_constraints

    def test_strips_code_fences(self, sample_constraints):
        fenced = f"```json\n{json.dumps(sample_constraints)}\n```"
        with patch("anthropic.Anthropic", return_value=make_mock_client(fenced)):
            result = pc.extract_constraints("anything")
        assert result == sample_constraints

    def test_invalid_json_raises_value_error(self):
        with patch("anthropic.Anthropic", return_value=make_mock_client("not json at all")):
            with pytest.raises(ValueError, match="non-JSON"):
                pc.extract_constraints("anything")

    def test_missing_anthropic_raises_runtime_error(self):
        # Simulate ImportError when loading anthropic
        with patch.dict(sys.modules, {"anthropic": None}):
            with pytest.raises(RuntimeError, match="anthropic SDK not installed"):
                pc.extract_constraints("anything")


class TestModeAndSessionCodeDefaults:
    """The parser returns `mode` and `session_code` with defensive defaults
    when the LLM omits them."""

    def test_missing_mode_defaults_to_demo(self):
        body = json.dumps({
            "role": "founder",
            "valuation_cap_min": 1, "valuation_cap_max": 2,
            "discount_min": 0.1, "pro_rata": "indifferent", "mfn": "indifferent",
        })
        with patch("anthropic.Anthropic", return_value=make_mock_client(body)):
            r = pc.extract_constraints("x")
        assert r["mode"] == "demo"
        assert r["session_code"] is None

    def test_unknown_mode_coerces_to_demo(self):
        body = json.dumps({
            "role": "founder", "mode": "whatever",
            "valuation_cap_min": 1, "valuation_cap_max": 2,
            "discount_min": 0.1, "pro_rata": "indifferent", "mfn": "indifferent",
        })
        with patch("anthropic.Anthropic", return_value=make_mock_client(body)):
            r = pc.extract_constraints("x")
        assert r["mode"] == "demo"

    def test_session_code_forces_two_party_mode(self):
        """Even if Haiku picks up the code but leaves mode=demo, we override."""
        body = json.dumps({
            "role": "investor", "mode": "demo", "session_code": "INV-7K3X9",
            "valuation_cap_min": 0, "valuation_cap_max": 40000000,
            "discount_min": 0.1, "pro_rata": "required", "mfn": "indifferent",
        })
        with patch("anthropic.Anthropic", return_value=make_mock_client(body)):
            r = pc.extract_constraints("Join negotiation INV-7K3X9 as investor.")
        assert r["mode"] == "two_party"
        assert r["session_code"] == "INV-7K3X9"

    def test_session_code_normalized_to_uppercase(self):
        body = json.dumps({
            "role": "investor", "mode": "two_party", "session_code": "  inv-7k3x9 ",
            "valuation_cap_min": 0, "valuation_cap_max": 10,
            "discount_min": 0.1, "pro_rata": "indifferent", "mfn": "indifferent",
        })
        with patch("anthropic.Anthropic", return_value=make_mock_client(body)):
            r = pc.extract_constraints("x")
        assert r["session_code"] == "INV-7K3X9"

    def test_empty_session_code_normalized_to_none(self):
        body = json.dumps({
            "role": "founder", "mode": "two_party", "session_code": "",
            "valuation_cap_min": 1, "valuation_cap_max": 2,
            "discount_min": 0.1, "pro_rata": "indifferent", "mfn": "indifferent",
        })
        with patch("anthropic.Anthropic", return_value=make_mock_client(body)):
            r = pc.extract_constraints("x")
        assert r["session_code"] is None
        # Mode stays two_party since parser said so and we don't auto-downgrade
        assert r["mode"] == "two_party"

    def test_two_party_without_code_is_valid_for_founder_creating(self):
        """Founders create sessions and start in two-party mode without a
        code — they RECEIVE one from sshsign."""
        body = json.dumps({
            "role": "founder", "mode": "two_party", "session_code": None,
            "valuation_cap_min": 1, "valuation_cap_max": 2,
            "discount_min": 0.1, "pro_rata": "indifferent", "mfn": "indifferent",
        })
        with patch("anthropic.Anthropic", return_value=make_mock_client(body)):
            r = pc.extract_constraints("Live negotiation with my investor.")
        assert r["mode"] == "two_party"
        assert r["session_code"] is None

    def test_passes_message_through(self):
        client = make_mock_client('{"valuation_cap_min": 1}')
        with patch("anthropic.Anthropic", return_value=client):
            pc.extract_constraints("my custom message")
        call_kwargs = client.messages.create.call_args.kwargs
        assert call_kwargs["messages"] == [{"role": "user", "content": "my custom message"}]
        assert "SAFE" in call_kwargs["system"]


class TestCli:
    def test_missing_api_key_uses_deterministic_parser(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
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

    def test_message_arg(self, tmp_path, monkeypatch):
        """--message flag should be accepted as an alternative to stdin."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--message", "test"],
            capture_output=True,
            text=True,
        )
        # Will fail on API call, but should NOT fail on "no message"
        assert "No message" not in result.stderr

    def test_message_file_arg(self, tmp_path, monkeypatch):
        """--message-file should read from a file."""
        msg_file = tmp_path / "request.txt"
        msg_file.write_text("Negotiate my SAFE, cap $10M.")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--message-file", str(msg_file)],
            capture_output=True,
            text=True,
        )
        assert "No message" not in result.stderr

    def test_output_file_arg(self, tmp_path, monkeypatch, sample_constraints):
        """--output-file should write JSON to a file instead of stdout."""
        out_file = tmp_path / "constraints.json"
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        with patch.object(pc, "extract_constraints", return_value=sample_constraints):
            argv = ["parse_constraints.py", "--message", "test", "--output-file", str(out_file)]
            with patch.object(sys, "argv", argv):
                rc = pc.main()
        assert rc == 0
        assert out_file.exists()
        assert json.loads(out_file.read_text()) == sample_constraints

    def test_no_message_at_all(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            input="",
            capture_output=True,
            text=True,
        )
        assert result.returncode == 2
        assert "No message" in result.stderr
