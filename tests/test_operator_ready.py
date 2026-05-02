"""Tests for operator install/setup helpers."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import operator_ready as op


def _cp(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


class TestBuildOperatorUpdates:
    def test_load_skill_manifest(self):
        manifest = op.load_skill_manifest()
        assert manifest["name"] == "negotiate_safe"
        assert manifest["entrypoint"] == "run_safe.py"
        assert "run_safe.py" in manifest["files"]

    def test_normalizes_role_and_bot_handle(self):
        updates = op.build_operator_updates(
            role="Founder",
            bot_username="@FounderBot",
            sshsign_host="sshsign.dev",
            negotiate_repo_path="/opt/negotiate",
            scan_interval="5s",
        )
        assert updates == {
            "NEGOTIATE_SAFE_BOT_ROLE": "founder",
            "TELEGRAM_BOT_USERNAME": "FounderBot",
            "SSHSIGN_HOST": "sshsign.dev",
            "NEGOTIATE_REPO_PATH": "/opt/negotiate",
            "CLAW_NEGOTIATE_SCAN_INTERVAL": "5s",
        }

    def test_rejects_invalid_role(self):
        try:
            op.build_operator_updates(role="lawyer")
        except ValueError as exc:
            assert "founder or investor" in str(exc)
        else:
            raise AssertionError("expected ValueError")


class TestPersistOperatorUpdates:
    def test_calls_openclaw_config_set(self):
        runner = MagicMock(return_value=_cp())
        failures = op.persist_operator_updates(
            {"NEGOTIATE_SAFE_BOT_ROLE": "founder"},
            runner=runner,
        )
        assert failures == []
        cmd = runner.call_args.args[0]
        assert cmd == [
            "openclaw", "config", "set",
            "skills.entries.negotiate_safe.env.NEGOTIATE_SAFE_BOT_ROLE",
            "founder",
        ]

    def test_reports_failures(self):
        runner = MagicMock(return_value=_cp(returncode=1, stderr="nope"))
        failures = op.persist_operator_updates({"SSHSIGN_HOST": "x"}, runner=runner)
        assert failures == ["SSHSIGN_HOST"]

    def test_accepts_written_value_despite_nonzero(self, tmp_path, monkeypatch):
        cfg = tmp_path / "openclaw.json"
        cfg.write_text(json.dumps({
            "skills": {
                "entries": {
                    "negotiate_safe": {
                        "env": {"SSHSIGN_HOST": "sshsign.dev"},
                    },
                },
            },
        }))
        monkeypatch.setattr(op, "OPENCLAW_CONFIG_PATH", cfg)
        runner = MagicMock(return_value=_cp(returncode=1, stderr="workspace edit failed"))

        failures = op.persist_operator_updates({"SSHSIGN_HOST": "sshsign.dev"}, runner=runner)

        assert failures == []

    def test_accepts_written_value_after_timeout(self, tmp_path, monkeypatch):
        cfg = tmp_path / "openclaw.json"
        cfg.write_text(json.dumps({
            "skills": {
                "entries": {
                    "negotiate_safe": {
                        "env": {"SSHSIGN_HOST": "sshsign.dev"},
                    },
                },
            },
        }))
        monkeypatch.setattr(op, "OPENCLAW_CONFIG_PATH", cfg)

        def runner(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get("timeout"))

        failures = op.persist_operator_updates({"SSHSIGN_HOST": "sshsign.dev"}, runner=runner)

        assert failures == []


class TestDoctor:
    def test_reports_missing_env(self, tmp_path):
        checks = op.doctor_checks(env={}, runner=MagicMock(return_value=_cp()))
        by_name = {c.name: c for c in checks}
        assert by_name["ANTHROPIC_API_KEY"].ok is False
        assert by_name["USER_DID"].ok is False
        assert by_name["NEGOTIATE_SAFE_BOT_ROLE"].ok is False
        assert by_name["workflow leases"].ok is False

    def test_reads_openclaw_skill_env_when_env_not_in_process(self, tmp_path, monkeypatch):
        repo = tmp_path / "negotiate"
        repo.mkdir()
        (repo / "create_tokens.py").write_text("")
        (repo / "negotiate.py").write_text(
            "from dataclasses import dataclass\n"
            "@dataclass\n"
            "class NegotiationConfig:\n"
            "    role: str = ''\n"
            "    signer_role: str = ''\n"
        )
        cfg = tmp_path / "openclaw.json"
        cfg.write_text(json.dumps({
            "skills": {
                "entries": {
                    "negotiate_safe": {
                        "env": {
                            "ANTHROPIC_API_KEY": "test",
                            "USER_DID": "did:apoa:test",
                            "TELEGRAM_BOT_USERNAME": "Bot",
                            "NEGOTIATE_SAFE_BOT_ROLE": "investor",
                            "NEGOTIATE_REPO_PATH": str(repo),
                        },
                    },
                },
            },
        }))
        monkeypatch.setattr(op, "OPENCLAW_CONFIG_PATH", cfg)
        for key in [
            "ANTHROPIC_API_KEY",
            "USER_DID",
            "TELEGRAM_BOT_USERNAME",
            "NEGOTIATE_SAFE_BOT_ROLE",
            "NEGOTIATE_REPO_PATH",
        ]:
            monkeypatch.delenv(key, raising=False)

        checks = op.doctor_checks(runner=MagicMock(return_value=_cp()))

        by_name = {c.name: c for c in checks}
        assert by_name["ANTHROPIC_API_KEY"].ok is True
        assert by_name["USER_DID"].ok is True
        assert by_name["TELEGRAM_BOT_USERNAME"].ok is True
        assert by_name["NEGOTIATE_SAFE_BOT_ROLE"].ok is True
        assert by_name["NEGOTIATE_REPO_PATH"].ok is True

    def test_feature_probes_upstream_config(self, tmp_path):
        repo = tmp_path / "negotiate"
        repo.mkdir()
        (repo / "create_tokens.py").write_text("")
        (repo / "negotiate.py").write_text(
            "from dataclasses import dataclass\n"
            "@dataclass\n"
            "class NegotiationConfig:\n"
            "    role: str = ''\n"
            "    signer_role: str = ''\n"
        )

        def runner(argv, **kwargs):
            if argv[0] == "ssh":
                return _cp(stdout=json.dumps({"error": "session not found"}))
            return _cp(stdout="ok")

        checks = op.doctor_checks(
            env={
                "USER_DID": "did:apoa:test",
                "TELEGRAM_BOT_USERNAME": "Bot",
                "NEGOTIATE_SAFE_BOT_ROLE": "founder",
                "NEGOTIATE_REPO_PATH": str(repo),
            },
            runner=runner,
        )
        by_name = {c.name: c for c in checks}
        assert by_name["upstream NegotiationConfig.role"].ok is True
        assert by_name["upstream NegotiationConfig.signer_role"].ok is True
        assert by_name["sshsign get-session"].ok is True
        assert by_name["workflow leases"].ok is True

    def test_doctor_rejects_old_sshsign_without_leases(self):
        def runner(argv, **kwargs):
            if argv[0] == "ssh" and argv[2] == "acquire-lease":
                return _cp(stdout=json.dumps({"error": "unknown command 'acquire-lease'"}))
            if argv[0] == "ssh":
                return _cp(stdout=json.dumps({"error": "session not found"}))
            return _cp(stdout="ok")

        checks = op.doctor_checks(
            env={
                "USER_DID": "did:apoa:test",
                "TELEGRAM_BOT_USERNAME": "Bot",
                "NEGOTIATE_SAFE_BOT_ROLE": "founder",
            },
            runner=runner,
        )
        by_name = {c.name: c for c in checks}
        assert by_name["workflow leases"].ok is False
        assert "acquire-lease" in by_name["workflow leases"].fix

    def test_format_doctor_includes_fixes(self):
        out = op.format_doctor([
            op.Check("x", True, "ok"),
            op.Check("y", False, "missing", "set y"),
        ])
        assert "ok    x - ok" in out
        assert "fail  y - missing" in out
        assert "fix   set y" in out
