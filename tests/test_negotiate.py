"""Tests for the skill's negotiate.py wrapper.

Mocks subprocess.run and the threading streamer. Focus: correct flag expansion
from two configs, mint-output loading, upstream invocation cwd, exit code
propagation. The sshsign polling thread is covered via a direct unit test.
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import negotiate as ng


class TestBuildUpstreamCmd:
    def test_expands_both_configs(self, sample_founder_config, sample_investor_config, tmp_path):
        repo = tmp_path / "repo"
        cmd = ng.build_upstream_cmd(repo, sample_founder_config, sample_investor_config, role="")
        # Upstream executable
        assert str(repo / "negotiate.py") in cmd
        # Party-level fields from the right config
        assert "--founder-name" in cmd
        assert "Jane Doe" in cmd
        assert "--investor-name" in cmd
        assert "Angel Ventures" in cmd
        # Tokens from each config
        assert sample_founder_config["token"] in cmd
        assert sample_investor_config["token"] in cmd
        # Signing keys (shared across configs)
        fsk_idx = cmd.index("--founder-signing-key-id")
        isk_idx = cmd.index("--investor-signing-key-id")
        assert cmd[fsk_idx + 1] == "key_founder_1"
        assert cmd[isk_idx + 1] == "key_investor_1"
        # Poll enabled
        assert "--poll" in cmd

    def test_role_flag_passthrough(self, sample_founder_config, sample_investor_config, tmp_path):
        cmd = ng.build_upstream_cmd(tmp_path, sample_founder_config, sample_investor_config, role="founder")
        idx = cmd.index("--role")
        assert cmd[idx + 1] == "founder"

    def test_no_role_omits_flag(self, sample_founder_config, sample_investor_config, tmp_path):
        cmd = ng.build_upstream_cmd(tmp_path, sample_founder_config, sample_investor_config, role="")
        assert "--role" not in cmd

    def test_missing_signing_key_falls_back(self, sample_founder_config, sample_investor_config, tmp_path):
        # Drop the shared keys; fall back to per-config signing_key_id
        del sample_founder_config["founder_signing_key_id"]
        del sample_investor_config["investor_signing_key_id"]
        cmd = ng.build_upstream_cmd(tmp_path, sample_founder_config, sample_investor_config, role="")
        # founder config has signing_key_id="key_founder_1"
        assert "key_founder_1" in cmd
        assert "key_investor_1" in cmd


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

    def test_happy_path_launches_subprocess_and_joins_streamer(self, sample_mint_output, tmp_path, monkeypatch, capsys):
        # Fake repo with negotiate.py and a sshsign_client stub
        (tmp_path / "negotiate.py").write_text("# stub")
        (tmp_path / "sshsign_client.py").write_text(
            "def get_history(host, negotiation_id): return []"
        )
        monkeypatch.setenv("NEGOTIATE_REPO_PATH", str(tmp_path))

        mint_path = tmp_path / "mint.json"
        mint_path.write_text(json.dumps(sample_mint_output))

        fake_proc = MagicMock(returncode=0)
        with patch("subprocess.run", return_value=fake_proc) as run, \
             patch("time.sleep"):  # skip the pre-shutdown sleep
            argv = ["negotiate.py", "--mint-output", str(mint_path), "--poll-interval", "0.01"]
            with patch.object(sys, "argv", argv):
                rc = ng.main()

        assert rc == 0
        # subprocess invoked with cwd=repo
        assert run.call_args.kwargs["cwd"] == tmp_path.resolve()
        # Final exit marker on stdout
        out_lines = [l for l in capsys.readouterr().out.splitlines() if l.strip()]
        assert any('"type": "exit"' in l for l in out_lines)

    def test_exit_code_propagates(self, sample_mint_output, tmp_path, monkeypatch):
        (tmp_path / "negotiate.py").write_text("# stub")
        (tmp_path / "sshsign_client.py").write_text("def get_history(host, negotiation_id): return []")
        monkeypatch.setenv("NEGOTIATE_REPO_PATH", str(tmp_path))

        mint_path = tmp_path / "mint.json"
        mint_path.write_text(json.dumps(sample_mint_output))

        with patch("subprocess.run", return_value=MagicMock(returncode=42)), patch("time.sleep"):
            argv = ["negotiate.py", "--mint-output", str(mint_path), "--poll-interval", "0.01"]
            with patch.object(sys, "argv", argv):
                rc = ng.main()
        assert rc == 42


class TestStreamOffers:
    def test_emits_new_offers_as_ndjson(self, capsys):
        # Growing history: [], [offer1], [offer1, offer2]
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
        time.sleep(0.1)  # let it poll a few times
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
        """A one-off sshsign failure should log to stderr and keep polling."""
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
        # tmp_path has no sshsign_client.py. importlib.util should report
        # the file as missing without falling through to a cached module.
        result = ng.load_get_history(tmp_path)
        assert result is None
        assert "cannot import sshsign_client" in capsys.readouterr().err

    def test_returns_none_when_module_raises_at_import(self, tmp_path, capsys):
        (tmp_path / "sshsign_client.py").write_text("raise RuntimeError('boom')\n")
        result = ng.load_get_history(tmp_path)
        assert result is None
        assert "boom" in capsys.readouterr().err
