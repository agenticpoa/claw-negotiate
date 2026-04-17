"""Tests for parse_constraints.py.

The Anthropic client is mocked. Focus: response handling, JSON parsing edge
cases, CLI exit codes. Live-API behavior is covered in integration tests.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import parse_constraints as pc

SCRIPT = Path(__file__).parent.parent / "negotiate_safe" / "parse_constraints.py"


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
        with patch("anthropic.Anthropic", return_value=make_mock_client(response_text)):
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

    def test_passes_message_through(self):
        client = make_mock_client('{"valuation_cap_min": 1}')
        with patch("anthropic.Anthropic", return_value=client):
            pc.extract_constraints("my custom message")
        call_kwargs = client.messages.create.call_args.kwargs
        assert call_kwargs["messages"] == [{"role": "user", "content": "my custom message"}]
        assert "SAFE" in call_kwargs["system"]


class TestCli:
    def test_missing_api_key(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            input="Negotiate my SAFE.",
            capture_output=True,
            text=True,
        )
        assert result.returncode == 2
        assert "ANTHROPIC_API_KEY" in result.stderr

    def test_empty_stdin(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            input="",
            capture_output=True,
            text=True,
        )
        assert result.returncode == 2
        assert "No message on stdin" in result.stderr
