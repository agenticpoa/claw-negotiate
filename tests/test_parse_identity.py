"""Tests for parse_identity.

Covers the defensive normalization (role always valid; all fields present).
The Anthropic call is mocked.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import parse_identity as pi


def _mock_client(response_text: str):
    from unittest.mock import MagicMock
    c = MagicMock()
    c.messages.create.return_value = SimpleNamespace(
        content=[SimpleNamespace(text=response_text)]
    )
    return c


class TestExtractIdentity:
    def test_founder_happy_path(self):
        body = json.dumps({
            "role": "founder", "name": "Juan", "title": "CEO",
            "company": "APOA", "firm": None,
        })
        with patch("anthropic.Anthropic", return_value=_mock_client(body)):
            r = pi.extract_identity("I'm Juan, CEO of APOA")
        assert r == {"role": "founder", "name": "Juan", "title": "CEO",
                     "company": "APOA", "firm": None}

    def test_investor_happy_path(self):
        body = json.dumps({
            "role": "investor", "name": "Mark", "title": "Partner",
            "company": None, "firm": "Blue Fund",
        })
        with patch("anthropic.Anthropic", return_value=_mock_client(body)):
            r = pi.extract_identity("Mark, partner at Blue Fund")
        assert r["role"] == "investor"
        assert r["firm"] == "Blue Fund"

    def test_missing_role_defaults_to_founder(self):
        body = json.dumps({"name": "Juan", "title": None, "company": "APOA", "firm": None})
        with patch("anthropic.Anthropic", return_value=_mock_client(body)):
            r = pi.extract_identity("anything")
        assert r["role"] == "founder"

    def test_unknown_role_coerces_to_founder(self):
        body = json.dumps({"role": "observer", "name": "X"})
        with patch("anthropic.Anthropic", return_value=_mock_client(body)):
            r = pi.extract_identity("anything")
        assert r["role"] == "founder"

    def test_fills_missing_fields_with_none(self):
        body = json.dumps({"role": "founder", "name": "X"})
        with patch("anthropic.Anthropic", return_value=_mock_client(body)):
            r = pi.extract_identity("anything")
        for f in ("name", "title", "company", "firm"):
            assert f in r

    def test_strips_code_fences(self):
        body = json.dumps({"role": "founder", "name": "X", "title": None,
                           "company": None, "firm": None})
        fenced = f"```json\n{body}\n```"
        with patch("anthropic.Anthropic", return_value=_mock_client(fenced)):
            r = pi.extract_identity("anything")
        assert r["name"] == "X"

    def test_invalid_json_raises(self):
        with patch("anthropic.Anthropic", return_value=_mock_client("nope")):
            with pytest.raises(ValueError, match="non-JSON"):
                pi.extract_identity("anything")
