"""Tests for mint_token.py.

The create_tokens.py subprocess is mocked. Focus: argument validation, CLI
assembly (the right flags passed to upstream), output schema, edge cases.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import mint_token as mt

SCRIPT = Path(__file__).parent.parent / "negotiate_safe" / "mint_token.py"


class TestValidateConstraints:
    def test_happy_path(self, sample_constraints):
        out = mt.validate_constraints(json.dumps(sample_constraints))
        assert out == sample_constraints

    def test_missing_required_field(self, sample_constraints):
        del sample_constraints["valuation_cap_min"]
        with pytest.raises(SystemExit, match="valuation_cap_min"):
            mt.validate_constraints(json.dumps(sample_constraints))

    def test_null_required_field(self, sample_constraints):
        sample_constraints["discount_min"] = None
        with pytest.raises(SystemExit, match="discount_min"):
            mt.validate_constraints(json.dumps(sample_constraints))

    def test_invalid_json(self):
        with pytest.raises(SystemExit, match="Invalid"):
            mt.validate_constraints("not json")

    def test_optional_fields_may_be_null(self, sample_constraints):
        # company_name/investor_name/investment_amount aren't in REQUIRED_CONSTRAINTS
        sample_constraints["company_name"] = None
        sample_constraints["investor_name"] = None
        out = mt.validate_constraints(json.dumps(sample_constraints))
        assert out["company_name"] is None


class TestSlugify:
    @pytest.mark.parametrize("src,expected", [
        ("Acme Corp", "acme-corp"),
        ("Acme, Inc.", "acme--inc"),
        ("  trim  ", "trim"),
        ("AllCaps", "allcaps"),
        ("multi---dash", "multi---dash"),
    ])
    def test_slugify(self, src, expected):
        assert mt.slugify(src) == expected


class TestCli:
    def _base_args(self, constraints_json: str) -> list[str]:
        return [
            "--constraints-json", constraints_json,
            "--company-name", "Acme Corp",
            "--founder-name", "Jane Doe",
            "--investor-name", "Angel Ventures",
            "--investment-amount", "500000",
        ]

    def test_missing_negotiate_repo(self, sample_constraints, monkeypatch, capsys):
        monkeypatch.delenv("NEGOTIATE_REPO_PATH", raising=False)
        argv = ["mint_token.py"] + self._base_args(json.dumps(sample_constraints))
        with patch.object(sys, "argv", argv):
            rc = mt.main()
        assert rc == 2
        assert "NEGOTIATE_REPO_PATH" in capsys.readouterr().err

    def test_nonexistent_repo(self, sample_constraints, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("NEGOTIATE_REPO_PATH", str(tmp_path))
        argv = ["mint_token.py"] + self._base_args(json.dumps(sample_constraints))
        with patch.object(sys, "argv", argv):
            rc = mt.main()
        assert rc == 2
        assert "create_tokens.py not found" in capsys.readouterr().err

    def test_builds_correct_subprocess_call(self, sample_constraints, tmp_path, monkeypatch):
        # Fake repo structure: just create_tokens.py
        (tmp_path / "create_tokens.py").write_text("# stub")
        monkeypatch.setenv("NEGOTIATE_REPO_PATH", str(tmp_path))

        fake_result = MagicMock(returncode=0, stdout="", stderr="")
        with patch("subprocess.run", return_value=fake_result) as run:
            argv = ["mint_token.py"] + self._base_args(json.dumps(sample_constraints)) + [
                "--negotiation-id", "neg_test123",
                "--ttl-seconds", "7200",
                "--skip-sshsign-keys",
            ]
            with patch.object(sys, "argv", argv):
                rc = mt.main()

        assert rc == 0
        cmd = run.call_args.args[0]
        # Should invoke the upstream create_tokens.py
        assert str(tmp_path / "create_tokens.py") in cmd
        # Flags present and properly paired
        assert "--negotiation-id" in cmd
        assert "neg_test123" in cmd
        assert "--founder-cap-min" in cmd and "8000000" in cmd
        assert "--founder-cap-max" in cmd and "12000000" in cmd
        assert "--founder-pro-rata-required" in cmd and "true" in cmd
        assert "--founder-mfn-required" in cmd and "false" in cmd
        # No sshsign keys when flag passed
        assert "--create-keys" not in cmd
        # cwd is the repo (required for sshsign_client import)
        assert run.call_args.kwargs["cwd"] == tmp_path.resolve()

    def test_investor_env_vars_passed_when_set(self, sample_constraints, tmp_path, monkeypatch):
        (tmp_path / "create_tokens.py").write_text("# stub")
        monkeypatch.setenv("NEGOTIATE_REPO_PATH", str(tmp_path))
        monkeypatch.setenv("INVESTOR_CAP_MIN", "30000000")
        monkeypatch.setenv("INVESTOR_CAP_MAX", "80000000")
        monkeypatch.setenv("INVESTOR_DISCOUNT_MIN", "0.05")
        monkeypatch.setenv("INVESTOR_PRO_RATA_REQUIRED", "false")

        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")) as run:
            argv = ["mint_token.py"] + self._base_args(json.dumps(sample_constraints)) + [
                "--skip-sshsign-keys",
            ]
            with patch.object(sys, "argv", argv):
                mt.main()

        cmd = run.call_args.args[0]
        assert "--investor-cap-min" in cmd and "30000000" in cmd
        assert "--investor-cap-max" in cmd and "80000000" in cmd
        assert "--investor-discount-min" in cmd and "0.05" in cmd
        assert "--investor-pro-rata-required" in cmd and "false" in cmd

    def test_investor_env_vars_omitted_when_unset(self, sample_constraints, tmp_path, monkeypatch):
        (tmp_path / "create_tokens.py").write_text("# stub")
        monkeypatch.setenv("NEGOTIATE_REPO_PATH", str(tmp_path))
        monkeypatch.delenv("INVESTOR_CAP_MIN", raising=False)
        monkeypatch.delenv("INVESTOR_CAP_MAX", raising=False)

        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")) as run:
            argv = ["mint_token.py"] + self._base_args(json.dumps(sample_constraints)) + [
                "--skip-sshsign-keys",
            ]
            with patch.object(sys, "argv", argv):
                mt.main()

        cmd = run.call_args.args[0]
        assert "--investor-cap-min" not in cmd
        assert "--investor-cap-max" not in cmd

    def test_sshsign_keys_enabled_by_default(self, sample_constraints, tmp_path, monkeypatch):
        (tmp_path / "create_tokens.py").write_text("# stub")
        monkeypatch.setenv("NEGOTIATE_REPO_PATH", str(tmp_path))
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")) as run:
            argv = ["mint_token.py"] + self._base_args(json.dumps(sample_constraints))
            with patch.object(sys, "argv", argv):
                mt.main()
        assert "--create-keys" in run.call_args.args[0]

    def test_output_json_has_expected_keys(self, sample_constraints, tmp_path, monkeypatch, capsys):
        (tmp_path / "create_tokens.py").write_text("# stub")
        monkeypatch.setenv("NEGOTIATE_REPO_PATH", str(tmp_path))
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")):
            argv = ["mint_token.py"] + self._base_args(json.dumps(sample_constraints)) + [
                "--negotiation-id", "neg_out",
                "--skip-sshsign-keys",
            ]
            with patch.object(sys, "argv", argv):
                mt.main()

        out = json.loads(capsys.readouterr().out)
        assert out["negotiation_id"] == "neg_out"
        assert out["founder_config_path"].endswith("neg_out/founder.json")
        assert out["investor_config_path"].endswith("neg_out/investor.json")
        assert out["founder_token_path"].endswith("tokens/founder.jwt")
        assert out["investor_token_path"].endswith("tokens/investor.jwt")
        assert out["expires_at"].endswith("Z")
        assert out["intended_service"] == "safe:acme-corp:neg_out"
        # Documents the upstream gap: service in token is generic
        assert out["actual_service_in_token"] == "safe-agreement"

    def test_subprocess_failure_propagates(self, sample_constraints, tmp_path, monkeypatch, capsys):
        (tmp_path / "create_tokens.py").write_text("# stub")
        monkeypatch.setenv("NEGOTIATE_REPO_PATH", str(tmp_path))
        fail = MagicMock(returncode=3, stdout="upstream stdout\n", stderr="upstream error\n")
        with patch("subprocess.run", return_value=fail):
            argv = ["mint_token.py"] + self._base_args(json.dumps(sample_constraints)) + [
                "--skip-sshsign-keys",
            ]
            with patch.object(sys, "argv", argv):
                rc = mt.main()
        assert rc == 3
        err = capsys.readouterr().err
        assert "upstream stdout" in err
        assert "upstream error" in err

    def test_generated_negotiation_id_is_fresh(self, sample_constraints, tmp_path, monkeypatch, capsys):
        (tmp_path / "create_tokens.py").write_text("# stub")
        monkeypatch.setenv("NEGOTIATE_REPO_PATH", str(tmp_path))
        ids = []
        for _ in range(2):
            with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")):
                argv = ["mint_token.py"] + self._base_args(json.dumps(sample_constraints)) + [
                    "--skip-sshsign-keys"
                ]
                with patch.object(sys, "argv", argv):
                    mt.main()
            ids.append(json.loads(capsys.readouterr().out)["negotiation_id"])
        assert ids[0] != ids[1]
        assert all(i.startswith("neg_") for i in ids)
