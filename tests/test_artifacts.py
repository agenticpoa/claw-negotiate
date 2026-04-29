from __future__ import annotations

import json
from pathlib import Path

import artifacts


class TestBuildArtifactUri:
    def test_deterministic_per_session(self, tmp_path):
        uri = artifacts.build_artifact_uri("neg_xyz", tmp_path / "executed.pdf")
        assert uri == "sshsign://session/neg_xyz/executed.pdf"

    def test_does_not_leak_local_path(self):
        uri = artifacts.build_artifact_uri(
            "neg_abc",
            Path("/secret/path/file.pdf"),
        )
        assert "/secret/path" not in uri

    def test_embeds_creator_pending_id_for_joiner_finalize(self, tmp_path):
        uri = artifacts.build_artifact_uri(
            "neg_xyz",
            tmp_path / "x.pdf",
            creator_pending_id="pnd_f123",
            creator_role="founder",
        )
        assert "creator_pending=pnd_f123" in uri
        assert "creator_role=founder" in uri


class TestParseArtifactUri:
    def test_parses_creator_pending_and_role(self):
        pid, role = artifacts.parse_artifact_uri(
            "sshsign://session/neg_xyz/executed.pdf"
            "?creator_pending=pnd_abc&creator_role=founder"
        )
        assert pid == "pnd_abc"
        assert role == "founder"

    def test_missing_params_returns_empty(self):
        pid, role = artifacts.parse_artifact_uri(
            "sshsign://session/neg_x/executed.pdf"
        )
        assert pid == ""
        assert role == ""

    def test_only_creator_pending_partial(self):
        pid, role = artifacts.parse_artifact_uri(
            "sshsign://session/neg/executed.pdf?creator_pending=pnd_1"
        )
        assert pid == "pnd_1"
        assert role == ""


class TestWriteCounterpartyPending:
    def test_writes_to_expected_path(self, tmp_path):
        neg_dir = tmp_path / "negotiations" / "neg_abc"
        (neg_dir / "keys").mkdir(parents=True)
        (neg_dir / "founder.json").write_text("{}")
        (tmp_path / "mint.json").write_text(json.dumps({
            "negotiation_id": "neg_abc",
            "founder_config_path": str(neg_dir / "founder.json"),
        }))

        artifacts.write_counterparty_pending(
            tmp_path,
            session_id="neg_abc",
            role="founder",
            pending_id="pnd_f1",
        )

        expected = neg_dir / "output" / "neg_abc_founder_pending.txt"
        assert expected.exists()
        assert expected.read_text().strip() == "pnd_f1"

    def test_no_mint_json_is_silent(self, tmp_path):
        artifacts.write_counterparty_pending(
            tmp_path,
            session_id="neg_x",
            role="founder",
            pending_id="pnd_x",
        )
