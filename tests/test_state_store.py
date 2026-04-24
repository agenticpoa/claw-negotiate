"""Tests for state_store.py — P7-5 durable founder-wait pointer."""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

import state_store as ss


@pytest.fixture
def state_dir(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("CLAW_NEGOTIATE_STATE_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def sample_payload() -> dict:
    return {
        "negotiation_id": "neg_abc123",
        "output_dir": "/tmp/safe_negotiate",
        "session_code": "INV-ABCDE",
    }


class TestStateDir:
    def test_defaults_to_openclaw_skill_state(self, monkeypatch):
        monkeypatch.delenv("CLAW_NEGOTIATE_STATE_DIR", raising=False)
        d = ss.state_dir()
        assert d == Path.home() / ".openclaw" / "skill-state" / "negotiate_safe"

    def test_respects_env_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAW_NEGOTIATE_STATE_DIR", str(tmp_path))
        assert ss.state_dir() == tmp_path

    def test_state_path_creates_dir(self, tmp_path, monkeypatch):
        nested = tmp_path / "a" / "b" / "c"
        monkeypatch.setenv("CLAW_NEGOTIATE_STATE_DIR", str(nested))
        p = ss.state_path("neg_x")
        assert nested.exists()
        assert p == nested / "neg_x.json"


class TestWriteRead:
    def test_write_then_read_roundtrip(self, state_dir, sample_payload):
        ss.write_state(sample_payload)
        result = ss.read_state("neg_abc123")
        assert result == sample_payload

    def test_read_missing_returns_none(self, state_dir):
        assert ss.read_state("neg_missing") is None

    def test_write_rejects_missing_fields(self, state_dir):
        with pytest.raises(ss.StateCorruptError, match="missing required fields"):
            ss.write_state({"negotiation_id": "neg_x"})

    def test_read_rejects_corrupt_json(self, state_dir):
        (state_dir / "neg_bad.json").write_text("{not json")
        with pytest.raises(ss.StateCorruptError, match="invalid JSON"):
            ss.read_state("neg_bad")

    def test_read_rejects_non_object(self, state_dir):
        (state_dir / "neg_bad.json").write_text('"just a string"')
        with pytest.raises(ss.StateCorruptError, match="expected object"):
            ss.read_state("neg_bad")

    def test_read_rejects_missing_schema_fields(self, state_dir):
        (state_dir / "neg_partial.json").write_text(json.dumps({"negotiation_id": "neg_partial"}))
        with pytest.raises(ss.StateCorruptError, match="missing required fields"):
            ss.read_state("neg_partial")


class TestAtomicWrite:
    def test_atomic_write_uses_replace(self, state_dir, sample_payload):
        """If the rename step fails, the prior file must remain intact."""
        # Seed a prior good state.
        ss.write_state(sample_payload)
        prior = ss.read_state("neg_abc123")

        # Patch os.replace to raise mid-write; the old file must survive.
        with patch("state_store.os.replace", side_effect=OSError("disk full")):
            with pytest.raises(OSError):
                ss.write_state({**sample_payload, "output_dir": "/tmp/other"})

        # Old file intact; no half-written garbage.
        assert ss.read_state("neg_abc123") == prior

    def test_atomic_write_cleans_tempfile_on_failure(self, state_dir, sample_payload):
        with patch("state_store.os.replace", side_effect=OSError("boom")):
            with pytest.raises(OSError):
                ss.write_state(sample_payload)
        leftovers = [p for p in state_dir.iterdir() if p.name.startswith(".neg_abc123")]
        assert leftovers == [], f"tempfile not cleaned: {leftovers}"


class TestListActive:
    def test_empty_dir(self, state_dir):
        assert ss.list_active() == []

    def test_missing_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAW_NEGOTIATE_STATE_DIR", str(tmp_path / "not_yet"))
        assert ss.list_active() == []

    def test_returns_all_payloads(self, state_dir):
        a = {"negotiation_id": "neg_a", "output_dir": "/tmp/a", "session_code": "INV-A"}
        b = {"negotiation_id": "neg_b", "output_dir": "/tmp/b", "session_code": "INV-B"}
        ss.write_state(a)
        ss.write_state(b)
        result = ss.list_active()
        assert len(result) == 2
        ids = sorted(p["negotiation_id"] for p in result)
        assert ids == ["neg_a", "neg_b"]

    def test_skips_corrupt_files(self, state_dir, sample_payload):
        """One bad pointer doesn't halt the scan."""
        ss.write_state(sample_payload)
        (state_dir / "neg_bad.json").write_text("{not json")
        result = ss.list_active()
        assert len(result) == 1
        assert result[0]["negotiation_id"] == "neg_abc123"

    def test_skips_tempfiles_and_non_json(self, state_dir, sample_payload):
        ss.write_state(sample_payload)
        (state_dir / ".neg_xyz.tmp").write_text("{}")  # in-flight tempfile
        (state_dir / "README.txt").write_text("notes")
        result = ss.list_active()
        assert len(result) == 1


class TestDeleteState:
    def test_deletes_existing(self, state_dir, sample_payload):
        ss.write_state(sample_payload)
        ss.delete_state("neg_abc123")
        assert ss.read_state("neg_abc123") is None

    def test_missing_is_noop(self, state_dir):
        ss.delete_state("neg_never_existed")  # no raise


class TestPermissions:
    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores chmod")
    def test_write_surfaces_permission_error(self, state_dir, sample_payload):
        """Permission failures on the state dir must raise, not swallow."""
        state_dir.chmod(0o500)  # read+exec, no write
        try:
            with pytest.raises(OSError):
                ss.write_state(sample_payload)
        finally:
            state_dir.chmod(0o700)
