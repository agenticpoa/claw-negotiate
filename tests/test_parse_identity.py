"""Tests for parse_identity."""
from __future__ import annotations

import parse_identity as pi


class TestExtractIdentity:
    def test_founder_happy_path(self):
        r = pi.extract_identity("I'm Juan, CEO of APOA")
        assert r == {"role": "founder", "name": "Juan", "title": "CEO",
                     "company": "APOA", "firm": None}

    def test_investor_happy_path(self):
        r = pi.extract_identity("Mark, partner at Blue Fund")
        assert r["role"] == "investor"
        assert r["firm"] == "Blue Fund"

    def test_missing_role_defaults_to_founder(self):
        r = pi._normalize_identity({"name": "Juan", "title": None, "company": "APOA", "firm": None})
        assert r["role"] == "founder"

    def test_unknown_role_coerces_to_founder(self):
        r = pi._normalize_identity({"role": "observer", "name": "X"})
        assert r["role"] == "founder"

    def test_fills_missing_fields_with_none(self):
        r = pi._normalize_identity({"role": "founder", "name": "X"})
        for f in ("name", "title", "company", "firm"):
            assert f in r

    def test_deterministic_founder(self):
        r = pi.extract_identity("I'm Juan Figuera, CEO of Avocado")
        assert r == {
            "role": "founder",
            "name": "Juan Figuera",
            "title": "CEO",
            "company": "Avocado",
            "firm": None,
        }

    def test_deterministic_investor(self):
        r = pi.extract_identity("I'm Nora Vassileva, partner at SD Capital")
        assert r["role"] == "investor"
        assert r["name"] == "Nora Vassileva"
        assert r["title"] == "partner"
        assert r["firm"] == "SD Capital"
