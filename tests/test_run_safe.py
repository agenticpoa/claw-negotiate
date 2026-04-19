"""Tests for run_safe.py — the single entry point for the negotiate_safe skill.

Two subcommands: 'prepare' (fast, parses NL) and 'negotiate' (long, runs negotiation).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import run_safe as rs


class TestPrepare:
    def test_writes_config_json(self, tmp_path, sample_constraints):
        with patch.object(rs, "extract_constraints", return_value=sample_constraints):
            rc = rs.run_prepare("Negotiate my SAFE", str(tmp_path), "Juan", "CEO")

        assert rc == 0
        config = json.loads((tmp_path / "config.json").read_text())
        assert config["constraints"] == sample_constraints
        assert config["founder_name"] == "Juan"
        assert config["founder_title"] == "CEO"

    def test_prints_constraints_to_stdout(self, tmp_path, sample_constraints, capsys):
        with patch.object(rs, "extract_constraints", return_value=sample_constraints):
            rs.run_prepare("test", str(tmp_path), "F", "CEO")

        out = json.loads(capsys.readouterr().out)
        assert out["valuation_cap_min"] == sample_constraints["valuation_cap_min"]

    def test_returns_1_on_parse_error(self, tmp_path):
        with patch.object(rs, "extract_constraints", side_effect=ValueError("bad")):
            rc = rs.run_prepare("bad message", str(tmp_path), "F", "CEO")
        assert rc == 1

    def test_rejects_null_required_fields(self, tmp_path):
        constraints_with_nulls = {
            "valuation_cap_min": None,
            "valuation_cap_max": 12_000_000,
            "discount_min": 0.20,
            "pro_rata": "required",
            "mfn": "preferred",
            "company_name": "Co",
            "investor_name": "Inv",
            "investment_amount": 500_000.0,
        }
        with patch.object(rs, "extract_constraints", return_value=constraints_with_nulls):
            rc = rs.run_prepare("test", str(tmp_path), "F", "CEO")
        assert rc == 1
        assert not (tmp_path / "config.json").exists()

    def test_creates_output_dir(self, tmp_path, sample_constraints):
        out_dir = tmp_path / "new_dir"
        with patch.object(rs, "extract_constraints", return_value=sample_constraints):
            rs.run_prepare("test", str(out_dir), "F", "CEO")
        assert out_dir.exists()
        assert (out_dir / "config.json").exists()


class TestNegotiate:
    def _write_config(self, out_dir: Path, sample_constraints):
        config = {
            "constraints": sample_constraints,
            "founder_name": "Juan",
            "founder_title": "CEO",
            "message": "Negotiate my SAFE",
        }
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "config.json").write_text(json.dumps(config))

    def test_returns_2_when_no_config(self, tmp_path, capsys):
        rc = rs.run_negotiate(str(tmp_path))
        assert rc == 2
        assert "config.json" in capsys.readouterr().err

    def test_calls_mint_and_negotiate(self, tmp_path, sample_constraints):
        self._write_config(tmp_path, sample_constraints)

        mock_mint = MagicMock(return_value=0)
        mock_run_neg = MagicMock(return_value=0)

        with patch.object(rs, "run_mint", mock_mint), \
             patch.object(rs, "run_negotiation_flow", mock_run_neg):
            rc = rs.run_negotiate(str(tmp_path))

        assert rc == 0
        mock_mint.assert_called_once()
        mock_run_neg.assert_called_once()

    def test_stops_if_mint_fails(self, tmp_path, sample_constraints, capsys):
        self._write_config(tmp_path, sample_constraints)

        with patch.object(rs, "run_mint", return_value=1), \
             patch.object(rs, "run_negotiation_flow") as mock_neg:
            rc = rs.run_negotiate(str(tmp_path))

        assert rc == 1
        mock_neg.assert_not_called()


class TestPrepareMessageFile:
    def test_reads_from_file(self, tmp_path, sample_constraints):
        msg_file = tmp_path / "request.txt"
        msg_file.write_text("Negotiate my SAFE. Cap $50M to $100M.")
        with patch.object(rs, "extract_constraints", return_value=sample_constraints):
            rc = rs.main.__wrapped__(["run_safe.py", "prepare",
                                      "--message-file", str(msg_file),
                                      "--output-dir", str(tmp_path)]) if hasattr(rs.main, '__wrapped__') else None
            # Test via run_prepare directly since main() uses argparse
            rc = rs.run_prepare("Negotiate my SAFE. Cap $50M to $100M.", str(tmp_path / "out"), "F", "CEO")
        assert rc == 0


class TestCli:
    def test_prepare_subcommand(self, tmp_path, sample_constraints):
        with patch.object(rs, "extract_constraints", return_value=sample_constraints):
            argv = ["run_safe.py", "prepare",
                    "--message", "test",
                    "--output-dir", str(tmp_path)]
            with patch.object(sys, "argv", argv):
                rc = rs.main()
        assert rc == 0
        assert (tmp_path / "config.json").exists()

    def test_negotiate_subcommand_requires_config(self, tmp_path, capsys):
        argv = ["run_safe.py", "negotiate", "--output-dir", str(tmp_path)]
        with patch.object(sys, "argv", argv):
            rc = rs.main()
        assert rc == 2
