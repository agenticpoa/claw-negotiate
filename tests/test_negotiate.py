"""Tests for the skill's negotiate.py wrapper.

Phase 1.1 refactor: direct import of upstream's run_local() instead of
subprocess. Tests cover namespace construction, module loading, main flow,
and the sshsign history streaming thread.
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import negotiate as ng


class TestBuildNamespace:
    def test_sets_negotiation_id(self, sample_mint_output, sample_founder_config, sample_investor_config, tmp_path):
        ns = ng.build_namespace(sample_mint_output, tmp_path, sample_founder_config, sample_investor_config)
        assert ns.negotiation_id == "neg_abc123"

    def test_sets_session_id(self, sample_mint_output, sample_founder_config, sample_investor_config, tmp_path):
        ns = ng.build_namespace(sample_mint_output, tmp_path, sample_founder_config, sample_investor_config)
        assert ns.session_id == "session_neg_abc123"

    def test_sets_token_paths(self, sample_mint_output, sample_founder_config, sample_investor_config, tmp_path):
        ns = ng.build_namespace(sample_mint_output, tmp_path, sample_founder_config, sample_investor_config)
        assert ns.founder_token == sample_mint_output["founder_token_path"]
        assert ns.investor_token == sample_mint_output["investor_token_path"]

    def test_sets_pubkey_paths_from_configs(self, sample_mint_output, sample_founder_config, sample_investor_config, tmp_path):
        ns = ng.build_namespace(sample_mint_output, tmp_path, sample_founder_config, sample_investor_config)
        assert ns.founder_pubkey == sample_founder_config["pubkey"]
        assert ns.investor_pubkey == sample_investor_config["pubkey"]

    def test_sets_schema_path(self, sample_mint_output, sample_founder_config, sample_investor_config, tmp_path):
        ns = ng.build_namespace(sample_mint_output, tmp_path, sample_founder_config, sample_investor_config)
        assert ns.schema == str(tmp_path / "schemas" / "safe.json")

    def test_sets_output_dir_per_negotiation(self, sample_mint_output, sample_founder_config, sample_investor_config, tmp_path):
        ns = ng.build_namespace(sample_mint_output, tmp_path, sample_founder_config, sample_investor_config)
        assert "output" in ns.output_dir
        assert str(Path(sample_mint_output["founder_config_path"]).parent) in ns.output_dir

    def test_sets_party_info(self, sample_mint_output, sample_founder_config, sample_investor_config, tmp_path):
        ns = ng.build_namespace(sample_mint_output, tmp_path, sample_founder_config, sample_investor_config)
        assert ns.company_name == "Acme Corp"
        assert ns.founder_name == "Jane Doe"
        assert ns.investor_name == "Angel Ventures"
        assert ns.investment_amount == 500_000.0

    def test_sets_signing_key_ids(self, sample_mint_output, sample_founder_config, sample_investor_config, tmp_path):
        ns = ng.build_namespace(sample_mint_output, tmp_path, sample_founder_config, sample_investor_config)
        assert ns.founder_signing_key_id == "key_founder_1"
        assert ns.investor_signing_key_id == "key_investor_1"
        # signing_key_id (singular) must be set for handle_signing() to run
        assert ns.signing_key_id == "key_founder_1"

    def test_sets_constraint_fallbacks(self, sample_mint_output, sample_founder_config, sample_investor_config, tmp_path):
        ns = ng.build_namespace(sample_mint_output, tmp_path, sample_founder_config, sample_investor_config)
        assert ns.founder_cap_min == 8_000_000
        assert ns.founder_cap_max == 12_000_000
        assert ns.investor_cap_min == 6_000_000

    def test_no_sshsign_flag(self, sample_mint_output, sample_founder_config, sample_investor_config, tmp_path):
        ns = ng.build_namespace(sample_mint_output, tmp_path, sample_founder_config, sample_investor_config, no_sshsign=True)
        assert ns.no_sshsign is True

    def test_defaults_to_sshsign_enabled(self, sample_mint_output, sample_founder_config, sample_investor_config, tmp_path):
        ns = ng.build_namespace(sample_mint_output, tmp_path, sample_founder_config, sample_investor_config)
        assert ns.no_sshsign is False

    def test_role_is_empty_for_local_mode(self, sample_mint_output, sample_founder_config, sample_investor_config, tmp_path):
        ns = ng.build_namespace(sample_mint_output, tmp_path, sample_founder_config, sample_investor_config)
        assert ns.role == ""


class TestTokenExpiry:
    def test_detects_expired_token(self):
        import base64, json, time
        # Build a JWT with exp in the past
        header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).rstrip(b"=").decode()
        payload = base64.urlsafe_b64encode(json.dumps({"exp": int(time.time()) - 60}).encode()).rstrip(b"=").decode()
        jwt = f"{header}.{payload}.fakesig"
        assert ng.check_token_expiry(jwt) == "expired"

    def test_detects_near_expiry(self):
        import base64, json, time
        header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).rstrip(b"=").decode()
        payload = base64.urlsafe_b64encode(json.dumps({"exp": int(time.time()) + 30}).encode()).rstrip(b"=").decode()
        jwt = f"{header}.{payload}.fakesig"
        assert ng.check_token_expiry(jwt) == "expiring_soon"

    def test_valid_token(self):
        import base64, json, time
        header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).rstrip(b"=").decode()
        payload = base64.urlsafe_b64encode(json.dumps({"exp": int(time.time()) + 3600}).encode()).rstrip(b"=").decode()
        jwt = f"{header}.{payload}.fakesig"
        assert ng.check_token_expiry(jwt) is None

    def test_missing_exp_returns_none(self):
        import base64, json
        header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).rstrip(b"=").decode()
        payload = base64.urlsafe_b64encode(json.dumps({"sub": "test"}).encode()).rstrip(b"=").decode()
        jwt = f"{header}.{payload}.fakesig"
        assert ng.check_token_expiry(jwt) is None


class TestLoadRunLocal:
    def test_returns_callable_when_present(self, tmp_path):
        (tmp_path / "negotiate.py").write_text(
            "async def run_local(args): pass\n"
        )
        fn = ng.load_run_local(tmp_path)
        assert fn is not None
        assert callable(fn)

    def test_returns_none_when_missing(self, tmp_path, capsys):
        result = ng.load_run_local(tmp_path)
        assert result is None
        assert "not found" in capsys.readouterr().err

    def test_returns_none_when_module_errors(self, tmp_path, capsys):
        (tmp_path / "negotiate.py").write_text("raise RuntimeError('broken')\n")
        result = ng.load_run_local(tmp_path)
        assert result is None
        assert "broken" in capsys.readouterr().err


class TestLoadMint:
    def test_from_file(self, tmp_path):
        path = tmp_path / "mint.json"
        path.write_text(json.dumps({"negotiation_id": "neg_1"}))
        assert ng.load_mint(str(path)) == {"negotiation_id": "neg_1"}

    def test_from_stdin(self, monkeypatch):
        import io
        monkeypatch.setattr("sys.stdin", io.StringIO('{"negotiation_id": "neg_2"}'))
        assert ng.load_mint("-") == {"negotiation_id": "neg_2"}


class TestMain:
    def test_missing_repo_env(self, sample_mint_output, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("NEGOTIATE_REPO_PATH", raising=False)
        mint_path = tmp_path / "mint.json"
        mint_path.write_text(json.dumps(sample_mint_output))
        argv = ["negotiate.py", "--mint-output", str(mint_path)]
        with patch.object(sys, "argv", argv):
            rc = ng.main()
        assert rc == 2
        assert "NEGOTIATE_REPO_PATH" in capsys.readouterr().err

    def test_nonexistent_repo(self, sample_mint_output, tmp_path, monkeypatch, capsys):
        mint_path = tmp_path / "mint.json"
        mint_path.write_text(json.dumps(sample_mint_output))
        monkeypatch.setenv("NEGOTIATE_REPO_PATH", str(tmp_path))
        argv = ["negotiate.py", "--mint-output", str(mint_path)]
        with patch.object(sys, "argv", argv):
            rc = ng.main()
        assert rc == 2
        assert "negotiate.py not found" in capsys.readouterr().err

    def test_happy_path_calls_run_local(self, sample_mint_output, tmp_path, monkeypatch, capsys):
        (tmp_path / "negotiate.py").write_text("async def run_local(args): pass\n")
        monkeypatch.setenv("NEGOTIATE_REPO_PATH", str(tmp_path))

        mint_path = tmp_path / "mint.json"
        mint_path.write_text(json.dumps(sample_mint_output))

        mock_run_local = AsyncMock()
        with patch.object(ng, "load_run_local", return_value=mock_run_local), \
             patch.object(ng, "load_get_history", return_value=None):
            argv = ["negotiate.py", "--mint-output", str(mint_path), "--no-sshsign"]
            with patch.object(sys, "argv", argv):
                rc = ng.main()

        assert rc == 0
        mock_run_local.assert_called_once()
        ns = mock_run_local.call_args[0][0]
        assert ns.negotiation_id == "neg_abc123"
        assert ns.no_sshsign is True

        out_lines = [l for l in capsys.readouterr().out.splitlines() if l.strip()]
        assert any('"type": "exit"' in l for l in out_lines)

    def test_run_local_failure_returns_1(self, sample_mint_output, tmp_path, monkeypatch, capsys):
        (tmp_path / "negotiate.py").write_text("async def run_local(args): pass\n")
        monkeypatch.setenv("NEGOTIATE_REPO_PATH", str(tmp_path))

        mint_path = tmp_path / "mint.json"
        mint_path.write_text(json.dumps(sample_mint_output))

        mock_run_local = AsyncMock(side_effect=RuntimeError("agent crashed"))
        with patch.object(ng, "load_run_local", return_value=mock_run_local), \
             patch.object(ng, "load_get_history", return_value=None):
            argv = ["negotiate.py", "--mint-output", str(mint_path), "--no-sshsign"]
            with patch.object(sys, "argv", argv):
                rc = ng.main()

        assert rc == 1
        assert "agent crashed" in capsys.readouterr().err

    def test_emits_pdf_path_when_exists(self, sample_mint_output, tmp_path, monkeypatch, capsys):
        (tmp_path / "negotiate.py").write_text("async def run_local(args): pass\n")
        monkeypatch.setenv("NEGOTIATE_REPO_PATH", str(tmp_path))

        mint_path = tmp_path / "mint.json"
        mint_path.write_text(json.dumps(sample_mint_output))

        # Create the PDF where build_namespace would point output_dir
        neg_dir = Path(sample_mint_output["founder_config_path"]).parent
        out_dir = neg_dir / "output"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "neg_abc123_executed.pdf").write_text("fake pdf")

        mock_run_local = AsyncMock()
        with patch.object(ng, "load_run_local", return_value=mock_run_local), \
             patch.object(ng, "load_get_history", return_value=None):
            argv = ["negotiate.py", "--mint-output", str(mint_path), "--no-sshsign"]
            with patch.object(sys, "argv", argv):
                rc = ng.main()

        assert rc == 0
        out = capsys.readouterr().out
        assert '"type": "pdf"' in out
        assert "neg_abc123_executed.pdf" in out

    def test_load_run_local_failure(self, sample_mint_output, tmp_path, monkeypatch, capsys):
        (tmp_path / "negotiate.py").write_text("# stub")
        monkeypatch.setenv("NEGOTIATE_REPO_PATH", str(tmp_path))

        mint_path = tmp_path / "mint.json"
        mint_path.write_text(json.dumps(sample_mint_output))

        with patch.object(ng, "load_run_local", return_value=None):
            argv = ["negotiate.py", "--mint-output", str(mint_path), "--no-sshsign"]
            with patch.object(sys, "argv", argv):
                rc = ng.main()

        assert rc == 2
        assert "Failed to load" in capsys.readouterr().err


class TestStreamOffers:
    def test_emits_new_offers_as_ndjson(self, capsys):
        histories = [
            [],
            [{"round": 1, "party": "Founder"}],
            [{"round": 1, "party": "Founder"}, {"round": 2, "party": "Investor"}],
        ]
        state = {"i": 0}

        def get_history_fn(host, negotiation_id):
            h = histories[min(state["i"], len(histories) - 1)]
            state["i"] += 1
            return h

        stop = threading.Event()
        thread = threading.Thread(
            target=ng.stream_offers,
            args=(get_history_fn, "sshsign.dev", "neg_x", stop, 0.01),
            daemon=True,
        )
        thread.start()
        import time
        time.sleep(0.1)
        stop.set()
        thread.join(timeout=1)
        assert not thread.is_alive()

        lines = [l for l in capsys.readouterr().out.splitlines() if l.strip()]
        parsed = [json.loads(l) for l in lines]
        assert len(parsed) == 2
        assert parsed[0]["round"] == 1
        assert parsed[0]["type"] == "offer"
        assert parsed[1]["round"] == 2

    def test_tolerates_transient_get_history_errors(self, capsys):
        calls = {"n": 0}

        def flaky(host, negotiation_id):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("sshsign timeout")
            return [{"round": 1, "party": "Founder"}]

        stop = threading.Event()
        thread = threading.Thread(
            target=ng.stream_offers,
            args=(flaky, "sshsign.dev", "neg_x", stop, 0.01),
            daemon=True,
        )
        thread.start()
        import time
        time.sleep(0.1)
        stop.set()
        thread.join(timeout=1)
        assert not thread.is_alive()

        captured = capsys.readouterr()
        assert "sshsign timeout" in captured.err
        assert "Founder" in captured.out


class TestLoadGetHistory:
    def test_returns_callable_when_module_present(self, tmp_path):
        (tmp_path / "sshsign_client.py").write_text(
            "def get_history(host, negotiation_id): return [{'tag': 'ok'}]\n"
        )
        fn = ng.load_get_history(tmp_path)
        assert fn is not None
        assert fn(host="h", negotiation_id="n") == [{"tag": "ok"}]

    def test_returns_none_when_module_missing(self, tmp_path, capsys):
        result = ng.load_get_history(tmp_path)
        assert result is None
        assert "cannot import sshsign_client" in capsys.readouterr().err

    def test_returns_none_when_module_raises_at_import(self, tmp_path, capsys):
        (tmp_path / "sshsign_client.py").write_text("raise RuntimeError('boom')\n")
        result = ng.load_get_history(tmp_path)
        assert result is None
        assert "boom" in capsys.readouterr().err
