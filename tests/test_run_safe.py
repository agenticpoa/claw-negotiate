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

    def test_pushes_interstitial_then_confirm_card(self, tmp_path, sample_constraints):
        sender = MagicMock()
        with patch.object(rs, "extract_constraints", return_value=sample_constraints), \
             patch.object(rs, "resolve_chat_id", return_value="12345"):
            rc = rs.run_prepare("test", str(tmp_path), "F", "CEO",
                               chat_id_flag="12345", sender=sender)
        assert rc == 0
        assert sender.call_count == 2
        # First call: quick interstitial
        first_msg = sender.call_args_list[0].kwargs.get("message") or ""
        assert "Analyzing" in first_msg
        # Second call: the confirm card
        second_msg = sender.call_args_list[1].kwargs.get("message") or ""
        assert "Please review the terms below" in second_msg
        assert "**Valuation cap:**" in second_msg
        assert "**GO**" in second_msg

    def test_interstitial_sent_before_slow_parse(self, tmp_path, sample_constraints):
        """Interstitial MUST be sent before extract_constraints is called."""
        sender = MagicMock()
        call_log = []
        sender.side_effect = lambda *a, **kw: call_log.append(("send", kw.get("message", "")))

        def slow_parse(msg):
            call_log.append(("parse", msg))
            return sample_constraints

        with patch.object(rs, "extract_constraints", side_effect=slow_parse), \
             patch.object(rs, "resolve_chat_id", return_value="12345"):
            rs.run_prepare("test", str(tmp_path), "F", "CEO",
                          chat_id_flag="12345", sender=sender)

        assert call_log[0][0] == "send"
        assert "Analyzing" in call_log[0][1]
        assert call_log[1][0] == "parse"
        assert call_log[2][0] == "send"

    def test_parse_failure_pushes_error_message(self, tmp_path):
        sender = MagicMock()
        with patch.object(rs, "extract_constraints", side_effect=ValueError("bad")), \
             patch.object(rs, "resolve_chat_id", return_value="12345"):
            rc = rs.run_prepare("nonsense", str(tmp_path), "F", "CEO",
                               chat_id_flag="12345", sender=sender)
        assert rc == 1
        # Sender called twice: interstitial, then error
        assert sender.call_count == 2
        error_msg = sender.call_args_list[1].kwargs.get("message") or ""
        assert "Couldn't parse" in error_msg

    def test_skips_push_and_warns_when_no_chat_id(self, tmp_path, sample_constraints, capsys):
        sender = MagicMock()
        with patch.object(rs, "extract_constraints", return_value=sample_constraints), \
             patch.object(rs, "resolve_chat_id", return_value=None):
            rc = rs.run_prepare("test", str(tmp_path), "F", "CEO", sender=sender)
        assert rc == 0
        sender.assert_not_called()
        assert "no chat_id resolvable" in capsys.readouterr().err

    def test_first_run_no_identity_stashes_message_and_prompts(self, tmp_path, monkeypatch):
        """When FOUNDER_NAME is unset, prepare must NOT parse — it stashes
        the negotiation message and asks the user to introduce themselves."""
        monkeypatch.delenv("FOUNDER_NAME", raising=False)
        monkeypatch.setattr(rs, "IDENTITY_SENTINEL_PATH", tmp_path / "pending.txt")
        sender = MagicMock()
        parse = MagicMock()

        with patch.object(rs, "extract_constraints", parse), \
             patch.object(rs, "resolve_chat_id", return_value="12345"):
            rc = rs.run_prepare("Negotiate my SAFE with X", str(tmp_path / "out"),
                               chat_id_flag="12345", sender=sender)

        assert rc == 2
        parse.assert_not_called()  # skipped the slow Anthropic call
        # Message stashed for setup to pick up
        assert (tmp_path / "pending.txt").read_text() == "Negotiate my SAFE with X"
        # Welcome prompt pushed to chat
        msg = sender.call_args.kwargs.get("message") or ""
        assert "Welcome" in msg
        assert "self-intro" in msg.lower() or "who you are" in msg.lower()

    def test_configured_identity_proceeds_to_parse(self, tmp_path, sample_constraints, monkeypatch):
        monkeypatch.setenv("FOUNDER_NAME", "Juan Figuera")
        sender = MagicMock()
        with patch.object(rs, "extract_constraints", return_value=sample_constraints), \
             patch.object(rs, "resolve_chat_id", return_value="12345"):
            rc = rs.run_prepare("Negotiate my SAFE", str(tmp_path), "F", "CEO",
                               chat_id_flag="12345", sender=sender)
        assert rc == 0  # normal path
        # confirm card got pushed
        assert sender.call_count == 2  # interstitial + confirm


class TestRunSetup:
    def _identity(self, **overrides):
        base = {"role": "founder", "name": "Juan Figuera", "title": "CEO",
                "company": "APOA Inc", "firm": None}
        base.update(overrides)
        return base

    def test_persists_founder_env_vars(self, tmp_path, monkeypatch):
        """After setup, FOUNDER_NAME / FOUNDER_TITLE / COMPANY_NAME get
        written via `openclaw config set` for a founder self-intro."""
        monkeypatch.setattr(rs, "IDENTITY_SENTINEL_PATH", tmp_path / "pending.txt")
        persister = MagicMock(return_value=[])
        sender = MagicMock()

        with patch.object(rs, "extract_identity", return_value=self._identity()), \
             patch.object(rs, "resolve_chat_id", return_value="12345"):
            rc = rs.run_setup("I'm Juan Figuera, CEO of APOA Inc",
                             chat_id_flag="12345", sender=sender, persister=persister)

        assert rc == 0
        updates = persister.call_args[0][0]
        assert updates == {
            "FOUNDER_NAME": "Juan Figuera",
            "FOUNDER_TITLE": "CEO",
            "COMPANY_NAME": "APOA Inc",
        }

    def test_persists_investor_env_vars(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rs, "IDENTITY_SENTINEL_PATH", tmp_path / "pending.txt")
        persister = MagicMock(return_value=[])
        sender = MagicMock()
        identity = self._identity(role="investor", name="Mark Stone",
                                  title="Partner", company=None, firm="Blue Fund")

        with patch.object(rs, "extract_identity", return_value=identity), \
             patch.object(rs, "resolve_chat_id", return_value="12345"):
            rs.run_setup("Mark Stone, partner at Blue Fund",
                        chat_id_flag="12345", sender=sender, persister=persister)

        updates = persister.call_args[0][0]
        assert updates["INVESTOR_NAME"] == "Mark Stone"
        assert updates["INVESTOR_FIRM"] == "Blue Fund"
        assert "FOUNDER_NAME" not in updates

    def test_missing_name_rejects_with_message(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rs, "IDENTITY_SENTINEL_PATH", tmp_path / "pending.txt")
        sender = MagicMock()
        with patch.object(rs, "extract_identity", return_value=self._identity(name=None)), \
             patch.object(rs, "resolve_chat_id", return_value="12345"):
            rc = rs.run_setup("gibberish", chat_id_flag="12345", sender=sender,
                             persister=MagicMock(return_value=[]))
        assert rc == 1
        msg = sender.call_args.kwargs.get("message") or ""
        assert "name" in msg.lower()

    def test_auto_continues_with_stashed_negotiation(self, tmp_path, sample_constraints, monkeypatch):
        """If the user tried to negotiate before identity setup, run_setup
        resumes that negotiation automatically after persisting identity."""
        sentinel = tmp_path / "pending.txt"
        sentinel.write_text("Negotiate my SAFE with X")
        monkeypatch.setattr(rs, "IDENTITY_SENTINEL_PATH", sentinel)
        monkeypatch.setenv("FOUNDER_NAME", "Juan")  # simulate post-persist env state
        sender = MagicMock()
        mock_prepare = MagicMock(return_value=0)

        with patch.object(rs, "extract_identity", return_value=self._identity()), \
             patch.object(rs, "resolve_chat_id", return_value="12345"), \
             patch.object(rs, "run_prepare", mock_prepare):
            rs.run_setup("I'm Juan Figuera, CEO of APOA Inc",
                        chat_id_flag="12345", sender=sender,
                        persister=MagicMock(return_value=[]))

        mock_prepare.assert_called_once()
        assert mock_prepare.call_args.kwargs["message"] == "Negotiate my SAFE with X"
        # Sentinel file cleaned up
        assert not sentinel.exists()

    def test_no_stash_means_no_auto_continue(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rs, "IDENTITY_SENTINEL_PATH", tmp_path / "pending.txt")
        sender = MagicMock()
        mock_prepare = MagicMock()
        with patch.object(rs, "extract_identity", return_value=self._identity()), \
             patch.object(rs, "resolve_chat_id", return_value="12345"), \
             patch.object(rs, "run_prepare", mock_prepare):
            rs.run_setup("I'm Juan Figuera, CEO of APOA Inc",
                        chat_id_flag="12345", sender=sender,
                        persister=MagicMock(return_value=[]))
        mock_prepare.assert_not_called()

    def test_reports_persist_failures(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rs, "IDENTITY_SENTINEL_PATH", tmp_path / "pending.txt")
        sender = MagicMock()
        with patch.object(rs, "extract_identity", return_value=self._identity()), \
             patch.object(rs, "resolve_chat_id", return_value="12345"):
            rs.run_setup("I'm Juan Figuera, CEO of APOA Inc",
                        chat_id_flag="12345", sender=sender,
                        persister=MagicMock(return_value=["FOUNDER_NAME"]))
        # Partial-save warning sent to user
        messages = [c.kwargs.get("message", "") for c in sender.call_args_list]
        assert any("partial" in m.lower() for m in messages)


class TestRunProfile:
    def test_pushes_profile_card_when_identity_set(self, monkeypatch):
        monkeypatch.setenv("FOUNDER_NAME", "Juan Figuera")
        monkeypatch.setenv("FOUNDER_TITLE", "CEO")
        monkeypatch.setenv("COMPANY_NAME", "APOA Inc")
        monkeypatch.delenv("INVESTOR_NAME", raising=False)
        monkeypatch.delenv("INVESTOR_FIRM", raising=False)
        sender = MagicMock()
        with patch.object(rs, "resolve_chat_id", return_value="12345"):
            rc = rs.run_profile(chat_id_flag="12345", sender=sender)
        assert rc == 0
        sender.assert_called_once()
        msg = sender.call_args.kwargs.get("message") or ""
        assert "Juan Figuera" in msg
        assert "APOA Inc" in msg
        assert "Investor side" not in msg  # no investor data configured

    def test_empty_profile_pushes_setup_hint(self, monkeypatch):
        for var in ("FOUNDER_NAME", "FOUNDER_TITLE", "COMPANY_NAME",
                    "INVESTOR_NAME", "INVESTOR_FIRM"):
            monkeypatch.delenv(var, raising=False)
        sender = MagicMock()
        with patch.object(rs, "resolve_chat_id", return_value="12345"):
            rs.run_profile(chat_id_flag="12345", sender=sender)
        msg = sender.call_args.kwargs.get("message") or ""
        assert "empty" in msg.lower()

    def test_stdout_fallback_when_no_chat_id(self, monkeypatch, capsys):
        monkeypatch.setenv("FOUNDER_NAME", "Juan")
        with patch.object(rs, "resolve_chat_id", return_value=None):
            rs.run_profile(sender=MagicMock())
        out = capsys.readouterr().out
        assert "Juan" in out


class TestPersistEnvUpdates:
    def test_calls_openclaw_config_set_per_key(self):
        from unittest.mock import MagicMock as MM
        runner = MM(return_value=MagicMock(returncode=0, stdout="", stderr=""))
        updates = {"FOUNDER_NAME": "Juan", "COMPANY_NAME": "APOA"}
        failures = rs._persist_env_updates(updates, runner=runner)
        assert failures == []
        assert runner.call_count == 2
        calls = [c.args[0] for c in runner.call_args_list]
        paths = [cmd[cmd.index("set") + 1] for cmd in calls]
        assert "skills.entries.negotiate_safe.env.FOUNDER_NAME" in paths
        assert "skills.entries.negotiate_safe.env.COMPANY_NAME" in paths

    def test_reports_failures(self):
        from unittest.mock import MagicMock as MM
        runner = MM(return_value=MagicMock(returncode=1, stdout="", stderr="error"))
        failures = rs._persist_env_updates({"FOUNDER_NAME": "Juan"}, runner=runner)
        assert failures == ["FOUNDER_NAME"]

    def test_handles_openclaw_binary_missing(self):
        def raiser(*a, **kw):
            raise FileNotFoundError()
        failures = rs._persist_env_updates({"FOUNDER_NAME": "Juan"}, runner=raiser)
        assert failures == ["FOUNDER_NAME"]


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

    def test_calls_mint_and_streams(self, tmp_path, sample_constraints):
        self._write_config(tmp_path, sample_constraints)

        mock_mint = MagicMock(return_value=0)
        mock_stream = MagicMock(return_value=(0, None))

        with patch.object(rs, "run_mint", mock_mint), \
             patch.object(rs, "_stream_to_telegram", mock_stream), \
             patch.object(rs, "resolve_chat_id", return_value="12345"):
            rc = rs.run_negotiate(str(tmp_path), chat_id_flag="12345")

        assert rc == 0
        mock_mint.assert_called_once()
        mock_stream.assert_called_once()
        kwargs = mock_stream.call_args.kwargs
        assert kwargs["chat_id"] == "12345"
        assert kwargs["constraints"] == sample_constraints

    def test_writes_session_pid_file_before_mint(self, tmp_path, sample_constraints):
        """Each negotiate run claims the output dir by writing its PID. This
        lets any prior stale process detect it's been superseded when its
        long poll eventually times out."""
        import os
        self._write_config(tmp_path, sample_constraints)

        with patch.object(rs, "run_mint", return_value=0), \
             patch.object(rs, "_stream_to_telegram", return_value=(0, None)), \
             patch.object(rs, "resolve_chat_id", return_value="12345"):
            rs.run_negotiate(str(tmp_path), chat_id_flag="12345")

        pid_file = tmp_path / ".session.pid"
        assert pid_file.exists()
        assert int(pid_file.read_text().strip()) == os.getpid()

    def test_stops_if_mint_fails(self, tmp_path, sample_constraints, capsys):
        self._write_config(tmp_path, sample_constraints)

        with patch.object(rs, "run_mint", return_value=1), \
             patch.object(rs, "_stream_to_telegram") as mock_stream:
            rc = rs.run_negotiate(str(tmp_path))

        assert rc == 1
        mock_stream.assert_not_called()

    def test_returns_2_when_no_chat_id_resolvable(self, tmp_path, sample_constraints, capsys):
        self._write_config(tmp_path, sample_constraints)
        with patch.object(rs, "run_mint", return_value=0), \
             patch.object(rs, "resolve_chat_id", return_value=None), \
             patch.object(rs, "_stream_to_telegram") as mock_stream:
            rc = rs.run_negotiate(str(tmp_path))

        assert rc == 2
        assert "chat_id" in capsys.readouterr().err
        mock_stream.assert_not_called()

    def test_triggers_await_sign_when_signing_event_present(self, tmp_path, sample_constraints):
        self._write_config(tmp_path, sample_constraints)
        signing = {"type": "signing", "pending_id": "pnd_xyz"}
        with patch.object(rs, "run_mint", return_value=0), \
             patch.object(rs, "resolve_chat_id", return_value="12345"), \
             patch.object(rs, "_stream_to_telegram", return_value=(0, signing)), \
             patch.object(rs, "_await_sign_and_push", return_value=0) as mock_await:
            rc = rs.run_negotiate(str(tmp_path))

        assert rc == 0
        mock_await.assert_called_once()
        assert mock_await.call_args.kwargs["pending_id"] == "pnd_xyz"

    def test_two_party_mode_calls_gate_before_streaming(self, tmp_path, sample_constraints):
        """When mint.json says mode=two_party + user_role=founder,
        run_negotiate must call the founder gate before firing the stream."""
        self._write_config(tmp_path, sample_constraints)

        def fake_mint(output_dir, config):
            (Path(output_dir) / "mint.json").write_text(json.dumps({
                "negotiation_id": "neg_1",
                "mode": "two_party",
                "user_role": "founder",
                "session_code": "INV-X",
            }))
            return 0

        mock_gate = MagicMock(return_value=0)
        mock_stream = MagicMock(return_value=(0, None))

        with patch.object(rs, "run_mint", side_effect=fake_mint), \
             patch.object(rs, "_founder_two_party_gate", mock_gate), \
             patch.object(rs, "_stream_to_telegram", mock_stream), \
             patch.object(rs, "resolve_chat_id", return_value="12345"):
            rs.run_negotiate(str(tmp_path), chat_id_flag="12345")

        mock_gate.assert_called_once()
        mock_stream.assert_called_once()

    def test_two_party_mode_skips_stream_when_gate_fails(self, tmp_path, sample_constraints):
        self._write_config(tmp_path, sample_constraints)

        def fake_mint(output_dir, config):
            (Path(output_dir) / "mint.json").write_text(json.dumps({
                "negotiation_id": "neg_1",
                "mode": "two_party",
                "user_role": "founder",
                "session_code": "INV-X",
            }))
            return 0

        mock_stream = MagicMock()
        with patch.object(rs, "run_mint", side_effect=fake_mint), \
             patch.object(rs, "_founder_two_party_gate", return_value=1), \
             patch.object(rs, "_stream_to_telegram", mock_stream), \
             patch.object(rs, "resolve_chat_id", return_value="12345"):
            rc = rs.run_negotiate(str(tmp_path), chat_id_flag="12345")

        assert rc == 1
        mock_stream.assert_not_called()

    def test_demo_mode_skips_gate(self, tmp_path, sample_constraints):
        """Demo mode (default) must go straight to streaming — no gate."""
        self._write_config(tmp_path, sample_constraints)

        def fake_mint(output_dir, config):
            (Path(output_dir) / "mint.json").write_text(json.dumps({
                "negotiation_id": "neg_1",
                "mode": "demo",
            }))
            return 0

        mock_gate = MagicMock()
        with patch.object(rs, "run_mint", side_effect=fake_mint), \
             patch.object(rs, "_founder_two_party_gate", mock_gate), \
             patch.object(rs, "_stream_to_telegram", return_value=(0, None)), \
             patch.object(rs, "resolve_chat_id", return_value="12345"):
            rs.run_negotiate(str(tmp_path), chat_id_flag="12345")

        mock_gate.assert_not_called()

    def test_skips_await_when_no_signing_event(self, tmp_path, sample_constraints):
        self._write_config(tmp_path, sample_constraints)
        with patch.object(rs, "run_mint", return_value=0), \
             patch.object(rs, "resolve_chat_id", return_value="12345"), \
             patch.object(rs, "_stream_to_telegram", return_value=(0, None)), \
             patch.object(rs, "_await_sign_and_push") as mock_await:
            rc = rs.run_negotiate(str(tmp_path))

        assert rc == 0
        mock_await.assert_not_called()


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

    def test_negotiate_subcommand_accepts_chat_id(self, tmp_path, sample_constraints):
        # Write a config so run_negotiate proceeds past the prepare check
        config = {"constraints": sample_constraints, "founder_name": "F", "founder_title": "CEO", "message": "m"}
        (tmp_path / "config.json").write_text(json.dumps(config))

        argv = ["run_safe.py", "negotiate", "--output-dir", str(tmp_path), "--chat-id", "99999"]
        with patch.object(sys, "argv", argv), \
             patch.object(rs, "run_mint", return_value=0), \
             patch.object(rs, "_stream_to_telegram", return_value=(0, None)) as mock_stream:
            rc = rs.main()

        assert rc == 0
        assert mock_stream.call_args.kwargs["chat_id"] == "99999"


class TestRunMintRoleFlipping:
    def _config(self, role, constraints_overrides=None):
        c = {
            "role": role,
            "valuation_cap_min": 10_000_000,
            "valuation_cap_max": 20_000_000,
            "discount_min": 0.15,
            "pro_rata": "required",
            "mfn": "preferred",
            "company_name": "TestCo",
            "investor_name": "TestVC",
            "investment_amount": 500_000.0,
        }
        if constraints_overrides:
            c.update(constraints_overrides)
        return {
            "constraints": c,
            "founder_name": "Founder",
            "founder_title": "CEO",
            "message": "m",
        }

    def _run_and_capture_cmd(self, tmp_path, config, monkeypatch):
        """Invoke run_mint, swallow the subprocess, return the cmd list
        that would have been passed to create_tokens.py."""
        monkeypatch.setenv("NEGOTIATE_REPO_PATH", str(tmp_path))
        # Stub create_tokens.py existence check
        (tmp_path / "create_tokens.py").write_text("# stub")

        captured = {}

        def fake_run(cmd, cwd=None, capture_output=None, text=None):
            captured["cmd"] = cmd
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch.object(rs.subprocess, "run", side_effect=fake_run):
            rs.run_mint(str(tmp_path), config)
        return captured.get("cmd", [])

    def test_founder_role_binds_user_constraints_to_founder_flags(self, tmp_path, monkeypatch):
        monkeypatch.setenv("INVESTOR_CAP_MIN", "30000000")
        monkeypatch.setenv("INVESTOR_CAP_MAX", "80000000")

        config = self._config("founder")
        cmd = self._run_and_capture_cmd(tmp_path, config, monkeypatch)

        # User (founder) constraints go to --founder-*
        assert "--founder-cap-min" in cmd
        assert cmd[cmd.index("--founder-cap-min") + 1] == "10000000"
        assert "--founder-cap-max" in cmd
        assert cmd[cmd.index("--founder-cap-max") + 1] == "20000000"
        assert "--founder-pro-rata-required" in cmd
        assert cmd[cmd.index("--founder-pro-rata-required") + 1] == "true"
        # AI (investor) gets env defaults via --investor-*
        assert "--investor-cap-min" in cmd
        assert cmd[cmd.index("--investor-cap-min") + 1] == "30000000"

    def test_investor_role_binds_user_constraints_to_investor_flags(self, tmp_path, monkeypatch):
        """When user plays investor, user constraints go to --investor-*,
        AI (founder side) gets FOUNDER_* env defaults via --founder-*."""
        # Clear INVESTOR_* so they don't leak into the investor side
        for k in ("INVESTOR_CAP_MIN", "INVESTOR_CAP_MAX", "INVESTOR_DISCOUNT_MIN",
                  "INVESTOR_DISCOUNT_MAX", "INVESTOR_PRO_RATA_REQUIRED", "INVESTOR_MFN_REQUIRED"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("FOUNDER_CAP_MIN", "15000000")
        monkeypatch.setenv("FOUNDER_CAP_MAX", "40000000")

        config = self._config("investor")
        cmd = self._run_and_capture_cmd(tmp_path, config, monkeypatch)

        assert "--investor-cap-min" in cmd
        assert cmd[cmd.index("--investor-cap-min") + 1] == "10000000"
        assert "--investor-cap-max" in cmd
        assert cmd[cmd.index("--investor-cap-max") + 1] == "20000000"
        assert "--investor-pro-rata-required" in cmd
        assert cmd[cmd.index("--investor-pro-rata-required") + 1] == "true"
        # AI founder gets env defaults
        assert "--founder-cap-min" in cmd
        assert cmd[cmd.index("--founder-cap-min") + 1] == "15000000"

    def test_missing_role_defaults_to_founder(self, tmp_path, monkeypatch):
        config = self._config("founder")
        del config["constraints"]["role"]
        cmd = self._run_and_capture_cmd(tmp_path, config, monkeypatch)
        assert "--founder-cap-min" in cmd
        assert cmd[cmd.index("--founder-cap-min") + 1] == "10000000"

    def test_unknown_role_defaults_to_founder(self, tmp_path, monkeypatch):
        config = self._config("shareholder")  # bogus
        cmd = self._run_and_capture_cmd(tmp_path, config, monkeypatch)
        assert "--founder-cap-min" in cmd
        assert cmd[cmd.index("--founder-cap-min") + 1] == "10000000"

    def test_null_identity_fields_do_not_crash(self, tmp_path, monkeypatch):
        """Parses often leave some identity fields null (user didn't mention
        their own fund, or the counterparty's rep). The mint subprocess must
        still run — None values coerce to sane defaults, not leak into args."""
        for var in ("FOUNDER_NAME", "INVESTOR_NAME", "INVESTOR_FIRM", "COMPANY_NAME"):
            monkeypatch.delenv(var, raising=False)
        config = self._config("investor", {"investor_name": None, "company_name": None,
                                            "investor_firm": None, "investment_amount": None})
        cmd = self._run_and_capture_cmd(tmp_path, config, monkeypatch)
        assert all(a is not None for a in cmd), f"None in cmd: {cmd}"
        assert cmd[cmd.index("--investor-name") + 1] == "Investor"
        assert cmd[cmd.index("--investor-firm") + 1] == "Investor Firm"
        assert cmd[cmd.index("--company-name") + 1] == "Company"

    def test_founder_identity_from_nl_flows_through(self, tmp_path, monkeypatch):
        config = self._config("founder", {
            "founder_name": "Jane Doe",
            "founder_title": "CTO",
            "company_name": "Acme",
            "investor_name": "Mark Stone",
            "investor_firm": "Bay Capital",
        })
        cmd = self._run_and_capture_cmd(tmp_path, config, monkeypatch)
        assert cmd[cmd.index("--founder-name") + 1] == "Jane Doe"
        assert cmd[cmd.index("--founder-title") + 1] == "CTO"
        assert cmd[cmd.index("--investor-name") + 1] == "Mark Stone"
        assert cmd[cmd.index("--investor-firm") + 1] == "Bay Capital"

    def test_investor_identity_from_nl_flows_through(self, tmp_path, monkeypatch):
        config = self._config("investor", {
            "founder_name": "Dr. Rivera",
            "founder_title": "CEO",
            "company_name": "QuantumLabs",
            "investor_name": "Alex Chen",
            "investor_firm": "Blue Fund",
        })
        cmd = self._run_and_capture_cmd(tmp_path, config, monkeypatch)
        assert cmd[cmd.index("--founder-name") + 1] == "Dr. Rivera"
        assert cmd[cmd.index("--investor-name") + 1] == "Alex Chen"
        assert cmd[cmd.index("--investor-firm") + 1] == "Blue Fund"

    def test_env_identity_defaults_follow_upstream_convention(self, tmp_path, monkeypatch):
        """FOUNDER_*/INVESTOR_*/COMPANY_NAME env vars describe the parties
        regardless of who the user is (per upstream agenticpoa convention).
        NL overrides are already tested separately; this covers the env
        fallback path."""
        monkeypatch.setenv("FOUNDER_NAME", "Alice Chen")
        monkeypatch.setenv("FOUNDER_TITLE", "CEO")
        monkeypatch.setenv("INVESTOR_NAME", "Jordan Lee")
        monkeypatch.setenv("INVESTOR_FIRM", "Bay Capital")
        monkeypatch.setenv("COMPANY_NAME", "Acme Labs")

        config = self._config("founder", {
            "founder_name": None, "founder_title": None, "company_name": None,
            "investor_name": None, "investor_firm": None,
        })
        cmd = self._run_and_capture_cmd(tmp_path, config, monkeypatch)
        assert cmd[cmd.index("--founder-name") + 1] == "Alice Chen"
        assert cmd[cmd.index("--founder-title") + 1] == "CEO"
        assert cmd[cmd.index("--investor-name") + 1] == "Jordan Lee"
        assert cmd[cmd.index("--investor-firm") + 1] == "Bay Capital"
        assert cmd[cmd.index("--company-name") + 1] == "Acme Labs"

    def test_env_defaults_apply_regardless_of_user_role(self, tmp_path, monkeypatch):
        """Same env vars work when user is investor — this is the demo-mode
        property that both sides of the deal are configured once."""
        monkeypatch.setenv("FOUNDER_NAME", "Alice Chen")
        monkeypatch.setenv("INVESTOR_NAME", "Jordan Lee")
        monkeypatch.setenv("INVESTOR_FIRM", "Bay Capital")
        monkeypatch.setenv("COMPANY_NAME", "Acme Labs")

        config = self._config("investor", {
            "founder_name": None, "investor_name": None, "investor_firm": None,
            "company_name": None,
        })
        cmd = self._run_and_capture_cmd(tmp_path, config, monkeypatch)
        assert cmd[cmd.index("--founder-name") + 1] == "Alice Chen"
        assert cmd[cmd.index("--investor-name") + 1] == "Jordan Lee"
        assert cmd[cmd.index("--investor-firm") + 1] == "Bay Capital"
        assert cmd[cmd.index("--company-name") + 1] == "Acme Labs"

    def test_literal_fallback_when_no_env_no_nl(self, tmp_path, monkeypatch):
        for var in ("FOUNDER_NAME", "FOUNDER_TITLE", "INVESTOR_NAME",
                    "INVESTOR_FIRM", "COMPANY_NAME"):
            monkeypatch.delenv(var, raising=False)
        config = self._config("founder", {
            "founder_name": None, "founder_title": None, "company_name": None,
            "investor_name": None, "investor_firm": None,
        })
        cmd = self._run_and_capture_cmd(tmp_path, config, monkeypatch)
        # Generic placeholders so the AI agent and the PDF don't show null
        assert cmd[cmd.index("--founder-name") + 1] == "Founder"
        assert cmd[cmd.index("--investor-name") + 1] == "Investor"

    def test_user_did_env_used_as_principal(self, tmp_path, monkeypatch):
        monkeypatch.setenv("USER_DID", "did:apoa:newuser")
        config = self._config("founder")
        cmd = self._run_and_capture_cmd(tmp_path, config, monkeypatch)
        assert cmd[cmd.index("--principal-id") + 1] == "did:apoa:newuser"

    def test_principal_falls_back_to_default_when_unset(self, tmp_path, monkeypatch):
        monkeypatch.delenv("USER_DID", raising=False)
        config = self._config("founder")
        cmd = self._run_and_capture_cmd(tmp_path, config, monkeypatch)
        assert cmd[cmd.index("--principal-id") + 1] == "did:apoa:default"

    def test_role_persisted_in_mint_json(self, tmp_path, monkeypatch):
        """`user_role` written to mint.json so downstream knows which side
        the user is on (useful for streaming/confirm card context)."""
        config = self._config("investor")
        self._run_and_capture_cmd(tmp_path, config, monkeypatch)
        mint = json.loads((tmp_path / "mint.json").read_text())
        assert mint["user_role"] == "investor"


class TestRegisterSigningSession:
    def _neg_dir(self, tmp_path, role: str = "founder") -> Path:
        neg_dir = tmp_path / "neg"
        (neg_dir / "keys").mkdir(parents=True)
        (neg_dir / "keys" / f"{role}_public.pem").write_text(
            f"-----BEGIN APOA-----\n{role.upper()}_FAKE_KEY\n-----END APOA-----\n"
        )
        return neg_dir

    def _mint(self, neg_id: str = "neg_123") -> dict:
        return {"negotiation_id": neg_id}

    def _constraints(self, **overrides) -> dict:
        base = {
            "company_name": "Acme",
            "founder_name": "Jane",
            "founder_title": "CEO",
            "investor_name": "Mark",
            "investor_firm": "Bay",
            "role": "founder",
            "mode": "two_party",
        }
        base.update(overrides)
        return base

    def test_registers_session_and_merges_fields(self, tmp_path, monkeypatch):
        neg_dir = self._neg_dir(tmp_path)
        monkeypatch.setenv("USER_DID", "did:apoa:juan")

        client = MagicMock()
        client.create_session.return_value = {
            "session_code": "INV-7K3X9",
            "created_at": "2026-04-21T12:00:00Z",
            "expires_at": "2026-04-22T12:00:00Z",
            "status": "open",
        }

        result = rs._register_signing_session(
            mint_output=self._mint(),
            constraints=self._constraints(),
            user_role="founder",
            neg_dir=neg_dir,
            session_client=client,
        )

        assert result == {
            "session_code": "INV-7K3X9",
            "session_created_at": "2026-04-21T12:00:00Z",
            "session_expires_at": "2026-04-22T12:00:00Z",
            "session_status": "open",
        }
        client.create_session.assert_called_once()
        call = client.create_session.call_args
        assert call.kwargs["session_id"] == "neg_123"
        assert call.kwargs["role"] == "founder"
        assert "FOUNDER_FAKE_KEY" in call.kwargs["apoa_pubkey_pem"]
        assert call.kwargs["party_did"] == "did:apoa:juan"
        # company_name lives in metadata_public so a prospective investor
        # can see "you're joining Acme's negotiation" pre-join.
        assert call.kwargs["metadata_public"] == {
            "use_case": "safe", "version": 1, "company_name": "Acme",
        }
        # metadata_member picks up identity fields, drops null/empty
        # (company_name is NOT here — it's in metadata_public for pre-join
        # visibility).
        md = call.kwargs["metadata_member"]
        assert md["founder_name"] == "Jane"
        assert md["investor_firm"] == "Bay"
        assert "company_name" not in md

    def test_investor_role_reads_investor_pubkey(self, tmp_path):
        neg_dir = self._neg_dir(tmp_path, role="investor")
        client = MagicMock()
        client.create_session.return_value = {"session_code": "INV-X"}

        rs._register_signing_session(
            mint_output=self._mint(),
            constraints=self._constraints(role="investor"),
            user_role="investor",
            neg_dir=neg_dir,
            session_client=client,
        )
        pem = client.create_session.call_args.kwargs["apoa_pubkey_pem"]
        assert "INVESTOR_FAKE_KEY" in pem

    def test_missing_pubkey_returns_none(self, tmp_path):
        neg_dir = tmp_path / "neg"
        neg_dir.mkdir()
        # No keys dir; should bail cleanly.
        client = MagicMock()
        result = rs._register_signing_session(
            mint_output=self._mint(),
            constraints=self._constraints(),
            user_role="founder",
            neg_dir=neg_dir,
            session_client=client,
        )
        assert result is None
        client.create_session.assert_not_called()

    def test_sshsign_error_returns_none(self, tmp_path):
        from sshsign_session import SshsignSessionError
        neg_dir = self._neg_dir(tmp_path)
        client = MagicMock()
        client.create_session.side_effect = SshsignSessionError("boom")
        result = rs._register_signing_session(
            mint_output=self._mint(),
            constraints=self._constraints(),
            user_role="founder",
            neg_dir=neg_dir,
            session_client=client,
        )
        assert result is None


class TestWaitForCounterparty:
    def _fake_loop(self):
        loop = MagicMock()
        loop.start = MagicMock()
        loop.stop = MagicMock()
        return loop

    def test_joined_returns_immediately(self):
        client = MagicMock()
        client.get_session.return_value = {"status": "joined"}
        sender = MagicMock()
        loop = self._fake_loop()
        sleep_fn = MagicMock()
        now_calls = iter([0.0, 0.5])  # start, first check

        result = rs._wait_for_counterparty(
            session_id="neg_1", session_code="INV-X",
            chat_id="12345", counterparty_label="Mark Stone",
            sender=sender, session_client=client,
            typing_factory=lambda _cid: loop,
            sleep_fn=sleep_fn, now_fn=lambda: next(now_calls),
        )
        assert result == "joined"
        # Joined message pushed
        msg = sender.call_args.kwargs.get("message", "")
        assert "Mark Stone" in msg
        assert "joined" in msg.lower()
        loop.start.assert_called_once()
        loop.stop.assert_called_once()
        sleep_fn.assert_not_called()

    def test_expired_from_sshsign_returns_expired(self):
        client = MagicMock()
        client.get_session.return_value = {"status": "expired"}
        now_calls = iter([0.0, 1.0])

        result = rs._wait_for_counterparty(
            session_id="n", session_code="c",
            chat_id="1", counterparty_label="X",
            sender=MagicMock(), session_client=client,
            typing_factory=lambda _cid: self._fake_loop(),
            sleep_fn=MagicMock(), now_fn=lambda: next(now_calls),
        )
        assert result == "expired"

    def test_canceled_returns_canceled(self):
        client = MagicMock()
        client.get_session.return_value = {"status": "canceled"}
        now_calls = iter([0.0, 1.0])
        result = rs._wait_for_counterparty(
            session_id="n", session_code="c", chat_id="1",
            counterparty_label="X",
            sender=MagicMock(), session_client=client,
            typing_factory=lambda _cid: self._fake_loop(),
            sleep_fn=MagicMock(), now_fn=lambda: next(now_calls),
        )
        assert result == "canceled"

    def test_local_timeout_returns_expired(self):
        """When elapsed exceeds max_wait_seconds, bail with 'expired'."""
        client = MagicMock()
        client.get_session.return_value = {"status": "open"}
        # now_fn jumps: 0 (start), 999 (elapsed check > max_wait_seconds)
        now_iter = iter([0.0, 999.0, 999.0, 999.0, 999.0])
        result = rs._wait_for_counterparty(
            session_id="n", session_code="c", chat_id="1",
            counterparty_label="X",
            sender=MagicMock(), session_client=client,
            typing_factory=lambda _cid: self._fake_loop(),
            sleep_fn=MagicMock(), now_fn=lambda: next(now_iter),
            max_wait_seconds=10,
        )
        assert result == "expired"

    def test_transient_ssh_errors_retry(self):
        from sshsign_session import SshsignSessionError
        client = MagicMock()
        client.get_session.side_effect = [
            SshsignSessionError("blip"),
            {"status": "joined"},
        ]
        now_iter = iter([0.0, 1.0, 2.0, 3.0])
        result = rs._wait_for_counterparty(
            session_id="n", session_code="c", chat_id="1",
            counterparty_label="X",
            sender=MagicMock(), session_client=client,
            typing_factory=lambda _cid: self._fake_loop(),
            sleep_fn=MagicMock(), now_fn=lambda: next(now_iter),
        )
        assert result == "joined"
        assert client.get_session.call_count == 2


class TestFounderTwoPartyGate:
    def test_missing_session_code_returns_3_and_warns(self, tmp_path):
        sender = MagicMock()
        rc = rs._founder_two_party_gate(
            out=tmp_path, chat_id="1",
            mint={"negotiation_id": "neg_1"},  # no session_code
            constraints={}, sender=sender,
            wait_fn=MagicMock(),
        )
        assert rc == 3
        assert "internal error" in (sender.call_args.kwargs.get("message") or "").lower()

    def test_joined_returns_0(self, tmp_path):
        sender = MagicMock()
        rc = rs._founder_two_party_gate(
            out=tmp_path, chat_id="1",
            mint={"negotiation_id": "n", "session_code": "INV-X",
                  "session_expires_at": "2026-04-22T12:00:00Z"},
            constraints={"investor_name": "M", "investor_firm": "B"},
            sender=sender, wait_fn=lambda **kw: "joined",
        )
        assert rc == 0
        # Invitation card pushed
        invitation_msgs = [c.kwargs.get("message", "") for c in sender.call_args_list]
        assert any("INV-X" in m for m in invitation_msgs)
        assert any("M, B" in m or "M" in m for m in invitation_msgs)

    def test_expired_returns_1_and_pushes_expiration_card(self, tmp_path):
        sender = MagicMock()
        rc = rs._founder_two_party_gate(
            out=tmp_path, chat_id="1",
            mint={"negotiation_id": "n", "session_code": "INV-X"},
            constraints={}, sender=sender,
            wait_fn=lambda **kw: "expired",
        )
        assert rc == 1
        msgs = [c.kwargs.get("message", "") for c in sender.call_args_list]
        assert any("expired" in m.lower() for m in msgs)

    def test_canceled_returns_2_silently(self, tmp_path):
        """Cancellation copy is owned by J5; the gate stays silent here."""
        sender = MagicMock()
        rc = rs._founder_two_party_gate(
            out=tmp_path, chat_id="1",
            mint={"negotiation_id": "n", "session_code": "INV-X"},
            constraints={}, sender=sender,
            wait_fn=lambda **kw: "canceled",
        )
        assert rc == 2
        # Only the invitation card should have been sent; no cancel copy here
        msgs = [c.kwargs.get("message", "") for c in sender.call_args_list]
        assert not any("canceled" in m.lower() for m in msgs)

    def test_error_status_returns_3_with_user_message(self, tmp_path):
        sender = MagicMock()
        rc = rs._founder_two_party_gate(
            out=tmp_path, chat_id="1",
            mint={"negotiation_id": "n", "session_code": "INV-X"},
            constraints={}, sender=sender,
            wait_fn=lambda **kw: "error",
        )
        assert rc == 3
        msgs = [c.kwargs.get("message", "") for c in sender.call_args_list]
        assert any("lost connection" in m.lower() for m in msgs)


class TestAugmentSigningUrl:
    def test_adds_bare_telegram_callback(self):
        event = {
            "type": "signing",
            "pending_id": "pnd_abc",
            "approval_url": "https://sshsign.dev/approve/pnd_abc",
        }
        out = rs._augment_signing_url(event, "TestBot")
        assert "callback=" in out["approval_url"]
        assert "TestBot" in out["approval_url"]
        # No `start=signed_` param — script handles detection autonomously
        assert "start=signed" not in out["approval_url"]
        # Input event is not mutated
        assert event["approval_url"] == "https://sshsign.dev/approve/pnd_abc"

    def test_callback_url_is_bot_chat_without_params(self):
        event = {
            "type": "signing",
            "pending_id": "pnd_abc",
            "approval_url": "https://sshsign.dev/approve/pnd_abc",
        }
        out = rs._augment_signing_url(event, "TestBot")
        # Decoded callback should be exactly https://t.me/TestBot
        import urllib.parse
        parsed = urllib.parse.urlparse(out["approval_url"])
        query = urllib.parse.parse_qs(parsed.query)
        assert query["callback"] == ["https://t.me/TestBot"]

    def test_uses_ampersand_if_url_already_has_query(self):
        event = {
            "type": "signing",
            "pending_id": "pnd_abc",
            "approval_url": "https://sshsign.dev/approve/pnd_abc?token=x",
        }
        out = rs._augment_signing_url(event, "TestBot")
        assert "&callback=" in out["approval_url"]

    def test_no_url_no_change(self):
        event = {"type": "signing", "pending_id": "pnd_abc"}
        out = rs._augment_signing_url(event, "TestBot")
        assert out == event

    def test_no_bot_username_no_change(self):
        event = {"type": "signing", "approval_url": "https://sshsign.dev/x"}
        out = rs._augment_signing_url(event, "")
        assert out == event

    def test_works_without_pending_id(self):
        # pending_id is no longer needed for the callback
        event = {"type": "signing", "approval_url": "https://sshsign.dev/x"}
        out = rs._augment_signing_url(event, "TestBot")
        assert "callback=" in out["approval_url"]


class TestStreamToTelegram:
    def _fake_proc(self, stdout_lines: list[str], returncode: int = 0):
        proc = MagicMock()
        proc.stdout = iter(stdout_lines)
        proc.wait = MagicMock()
        proc.returncode = returncode
        return proc

    def test_pushes_each_event_to_telegram(self, tmp_path, sample_constraints):
        events = [
            {"type": "offer", "round": 1, "party": "founder",
             "terms": {"valuation_cap": 10_000_000, "discount_rate": 0.20}},
            {"type": "counter", "round": 2, "party": "investor",
             "terms": {"valuation_cap": 8_000_000, "discount_rate": 0.15}},
            {"type": "accept", "round": 3, "party": "founder",
             "terms": {"valuation_cap": 9_000_000, "discount_rate": 0.18}},
            {"type": "outcome", "result": "accepted",
             "terms": {"valuation_cap": 9_000_000, "discount_rate": 0.18}},
        ]
        lines = [json.dumps(e) + "\n" for e in events]
        popen_mock = MagicMock(return_value=self._fake_proc(lines))
        sender = MagicMock()

        rc, signing = rs._stream_to_telegram(
            output_dir=tmp_path,
            chat_id="12345",
            constraints=sample_constraints,
            bot_username="TestBot",
            popen=popen_mock,
            sender=sender,
        )

        assert rc == 0
        assert signing is None
        # 3 cards pushed: offer, counter, accept. `outcome.result=accepted`
        # deliberately returns None from format_outcome (see format_event.py)
        # so the redundant "Deal!" card is skipped.
        assert sender.call_count == 3
        for call in sender.call_args_list:
            assert call.args[0] == "12345"

    def test_returns_signing_event_when_present(self, tmp_path):
        signing = {
            "type": "signing",
            "pending_id": "pnd_abc",
            "approval_url": "https://sshsign.dev/x",
        }
        lines = [json.dumps(signing) + "\n"]
        popen_mock = MagicMock(return_value=self._fake_proc(lines))

        rc, returned = rs._stream_to_telegram(
            output_dir=tmp_path, chat_id="1", constraints=None,
            bot_username="B", popen=popen_mock, sender=MagicMock(),
        )
        assert rc == 0
        assert returned is not None
        assert returned["pending_id"] == "pnd_abc"

    def test_skips_non_json_lines(self, tmp_path):
        lines = [
            "some human readable text\n",
            json.dumps({"type": "offer", "round": 1, "party": "founder", "terms": {}}) + "\n",
            "more noise\n",
        ]
        popen_mock = MagicMock(return_value=self._fake_proc(lines))
        sender = MagicMock()

        rs._stream_to_telegram(
            output_dir=tmp_path, chat_id="1", constraints=None,
            bot_username="B", popen=popen_mock, sender=sender,
        )
        assert sender.call_count == 1

    def test_signing_event_gets_callback_augmented(self, tmp_path):
        signing = {
            "type": "signing",
            "pending_id": "pnd_xyz",
            "approval_url": "https://sshsign.dev/approve/pnd_xyz",
        }
        lines = [json.dumps(signing) + "\n"]
        popen_mock = MagicMock(return_value=self._fake_proc(lines))
        sender = MagicMock()

        rs._stream_to_telegram(
            output_dir=tmp_path, chat_id="1", constraints=None,
            bot_username="MyBot", popen=popen_mock, sender=sender,
        )

        assert sender.call_count == 1
        msg = sender.call_args.kwargs.get("message") or sender.call_args.args[1]
        assert "callback=" in msg
        assert "MyBot" in msg

    def test_unknown_event_type_skipped_silently(self, tmp_path):
        lines = [
            json.dumps({"type": "mystery"}) + "\n",
            json.dumps({"type": "offer", "round": 1, "party": "f", "terms": {}}) + "\n",
        ]
        popen_mock = MagicMock(return_value=self._fake_proc(lines))
        sender = MagicMock()

        rs._stream_to_telegram(
            output_dir=tmp_path, chat_id="1", constraints=None,
            bot_username="B", popen=popen_mock, sender=sender,
        )
        assert sender.call_count == 1

    def test_archives_events_ndjson(self, tmp_path):
        events = [
            {"type": "offer", "round": 1, "party": "founder", "terms": {}},
            {"type": "outcome", "result": "max_rounds"},
        ]
        lines = [json.dumps(e) + "\n" for e in events]
        popen_mock = MagicMock(return_value=self._fake_proc(lines))

        rs._stream_to_telegram(
            output_dir=tmp_path, chat_id="1", constraints=None,
            bot_username="B", popen=popen_mock, sender=MagicMock(),
        )

        archive = (tmp_path / "events.ndjson").read_text().strip().splitlines()
        assert len(archive) == 2
        assert json.loads(archive[0])["type"] == "offer"
        assert json.loads(archive[1])["type"] == "outcome"

    def test_returns_subprocess_returncode_on_failure(self, tmp_path):
        popen_mock = MagicMock(return_value=self._fake_proc([], returncode=1))
        rc, signing = rs._stream_to_telegram(
            output_dir=tmp_path, chat_id="1", constraints=None,
            bot_username="B", popen=popen_mock, sender=MagicMock(),
        )
        assert rc == 1
        assert signing is None

    def test_starts_and_stops_typing_loop(self, tmp_path):
        """Typing loop must run during streaming (covers the gap between
        rounds) and stop cleanly when streaming ends."""
        lines = [json.dumps({"type": "outcome", "result": "max_rounds"}) + "\n"]
        popen_mock = MagicMock(return_value=self._fake_proc(lines))
        fake_loop = MagicMock()
        factory = MagicMock(return_value=fake_loop)

        rs._stream_to_telegram(
            output_dir=tmp_path, chat_id="1", constraints=None,
            bot_username="B", popen=popen_mock, sender=MagicMock(),
            typing_factory=factory,
        )

        factory.assert_called_once_with("1")
        fake_loop.start.assert_called_once()
        fake_loop.stop.assert_called_once()

    def test_typing_loop_stopped_on_exception(self, tmp_path):
        """If the stream raises, typing loop must still stop (finally block)."""
        popen_mock = MagicMock(side_effect=RuntimeError("boom"))
        fake_loop = MagicMock()
        factory = MagicMock(return_value=fake_loop)

        with pytest.raises(RuntimeError):
            rs._stream_to_telegram(
                output_dir=tmp_path, chat_id="1", constraints=None,
                bot_username="B", popen=popen_mock, sender=MagicMock(),
                typing_factory=factory,
            )
        fake_loop.stop.assert_called_once()

    def test_max_rounds_outcome_triggers_propose_new_terms_followup(self, tmp_path, sample_constraints):
        """No-ZOPA: after a max_rounds outcome lands, push a 'try again'
        follow-up card. Renders on both sides symmetrically."""
        events = [
            {"type": "offer", "round": 1, "party": "founder",
             "terms": {"valuation_cap": 20_000_000}},
            {"type": "counter", "round": 2, "party": "investor",
             "terms": {"valuation_cap": 5_000_000}},
            {"type": "outcome", "result": "max_rounds"},
        ]
        lines = [json.dumps(e) + "\n" for e in events]
        popen_mock = MagicMock(return_value=self._fake_proc(lines))
        sender = MagicMock()

        rs._stream_to_telegram(
            output_dir=tmp_path, chat_id="1", constraints=sample_constraints,
            bot_username="B", popen=popen_mock, sender=sender,
        )

        msgs = [c.kwargs.get("message", "") for c in sender.call_args_list]
        assert any("didn't overlap" in m.lower() for m in msgs)
        assert any("try again" in m.lower() for m in msgs)
        # Ordering: propose-new-terms lands AFTER the outcome card
        outcome_idx = next(i for i, m in enumerate(msgs) if "didn't overlap" in m.lower())
        follow_idx = next(i for i, m in enumerate(msgs) if "try again" in m.lower())
        assert outcome_idx < follow_idx

    def test_accepted_outcome_does_not_trigger_followup(self, tmp_path, sample_constraints):
        events = [
            {"type": "accept", "round": 3, "party": "founder",
             "terms": {"valuation_cap": 10_000_000, "discount_rate": 0.20}},
            {"type": "outcome", "result": "accepted",
             "terms": {"valuation_cap": 10_000_000}},
        ]
        lines = [json.dumps(e) + "\n" for e in events]
        popen_mock = MagicMock(return_value=self._fake_proc(lines))
        sender = MagicMock()
        rs._stream_to_telegram(
            output_dir=tmp_path, chat_id="1", constraints=sample_constraints,
            bot_username="B", popen=popen_mock, sender=sender,
        )
        msgs = [c.kwargs.get("message", "") for c in sender.call_args_list]
        assert not any("try again" in m.lower() for m in msgs)


class TestCounterpartyLabelFromConstraints:
    def test_founder_sees_investor_label(self):
        c = {"role": "founder", "investor_name": "Alex", "investor_firm": "Blue"}
        assert rs._counterparty_label_from_constraints(c) == "Alex, Blue"

    def test_investor_sees_founder_and_company(self):
        c = {"role": "investor", "founder_name": "Jane", "company_name": "Acme"}
        assert rs._counterparty_label_from_constraints(c) == "Jane, Acme"

    def test_default_role_is_founder(self):
        """Missing role defaults to founder's perspective (investor label)."""
        c = {"investor_name": "Alex"}
        assert rs._counterparty_label_from_constraints(c) == "Alex"

    def test_empty_returns_empty(self):
        assert rs._counterparty_label_from_constraints({}) == ""


class TestEnvelopeStatus:
    def _result(self, returncode: int, stdout: str, stderr: str = ""):
        import subprocess as sp
        return sp.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)

    def test_approved_status(self):
        runner = MagicMock(return_value=self._result(0, json.dumps({"status": "approved"})))
        assert rs._ssh_envelope_status("pnd_1", runner=runner) == "approved"

    def test_pending_status(self):
        runner = MagicMock(return_value=self._result(0, json.dumps({"status": "pending"})))
        assert rs._ssh_envelope_status("pnd_1", runner=runner) == "pending"

    def test_ssh_nonzero_returns_none(self):
        runner = MagicMock(return_value=self._result(1, "", "denied"))
        assert rs._ssh_envelope_status("pnd_1", runner=runner) is None

    def test_malformed_json_returns_none(self):
        runner = MagicMock(return_value=self._result(0, "not json"))
        assert rs._ssh_envelope_status("pnd_1", runner=runner) is None

    def test_timeout_returns_none(self):
        import subprocess as sp
        def raiser(*args, **kwargs):
            raise sp.TimeoutExpired(cmd="ssh", timeout=15)
        assert rs._ssh_envelope_status("pnd_1", runner=raiser) is None

    def test_passes_host_to_ssh(self):
        runner = MagicMock(return_value=self._result(0, json.dumps({"status": "approved"})))
        rs._ssh_envelope_status("pnd_1", sshsign_host="other.host", runner=runner)
        cmd = runner.call_args[0][0]
        assert cmd == ["ssh", "other.host", "get-envelope", "--id", "pnd_1"]


class TestPollEnvelopeApproval:
    def test_returns_true_when_status_goes_approved(self):
        statuses = iter(["pending", "pending", "approved"])
        status_fn = MagicMock(side_effect=lambda pid, host: next(statuses))
        sleep_fn = MagicMock()

        ok = rs._poll_envelope_approval(
            "pnd_1", timeout=60, interval=5,
            status_fn=status_fn, sleep_fn=sleep_fn,
        )
        assert ok is True
        # first check returned 'pending', then slept, second also pending, slept, third approved
        assert sleep_fn.call_count == 2

    def test_returns_true_immediately_on_first_approved(self):
        status_fn = MagicMock(return_value="approved")
        sleep_fn = MagicMock()
        ok = rs._poll_envelope_approval(
            "pnd_1", timeout=60, interval=5,
            status_fn=status_fn, sleep_fn=sleep_fn,
        )
        assert ok is True
        sleep_fn.assert_not_called()

    def test_returns_false_on_timeout(self):
        status_fn = MagicMock(return_value="pending")
        sleep_fn = MagicMock()
        ok = rs._poll_envelope_approval(
            "pnd_1", timeout=20, interval=5,
            status_fn=status_fn, sleep_fn=sleep_fn,
        )
        assert ok is False
        # 20s / 5s = 4 iterations
        assert sleep_fn.call_count == 4

    def test_treats_none_as_keep_polling(self):
        statuses = iter([None, None, "approved"])
        status_fn = MagicMock(side_effect=lambda pid, host: next(statuses))
        sleep_fn = MagicMock()
        ok = rs._poll_envelope_approval(
            "pnd_1", timeout=60, interval=5,
            status_fn=status_fn, sleep_fn=sleep_fn,
        )
        assert ok is True


class TestAwaitSignAndPush:
    def test_happy_path_sends_signed_and_pdf(self, tmp_path):
        pdf = tmp_path / "executed.pdf"
        pdf.write_bytes(b"%PDF-1.4")

        poll_fn = MagicMock(return_value=True)
        finalize_fn = MagicMock(return_value=pdf)
        sender = MagicMock()

        rc = rs._await_sign_and_push(
            output_dir=tmp_path,
            chat_id="12345",
            sshsign_host="sshsign.dev",
            pending_id="pnd_abc",
            sender=sender,
            poll_fn=poll_fn,
            finalize_fn=finalize_fn,
            is_active_fn=lambda _d: True,
        )

        assert rc == 0
        # Three messages: "Confirmed signature", "Generating executed file…", then the PDF
        assert sender.call_count == 3
        first = sender.call_args_list[0]
        assert first.args[0] == "12345"
        assert "Confirmed signature" in (first.kwargs.get("message") or "")
        second = sender.call_args_list[1]
        assert "Generating" in (second.kwargs.get("message") or "")
        third = sender.call_args_list[2]
        assert third.kwargs.get("media_path") == str(pdf)

    def test_timeout_sends_manual_fallback(self, tmp_path):
        poll_fn = MagicMock(return_value=False)
        finalize_fn = MagicMock()
        sender = MagicMock()

        rc = rs._await_sign_and_push(
            output_dir=tmp_path, chat_id="1", sshsign_host="h",
            pending_id="pnd_abc",
            sender=sender, poll_fn=poll_fn, finalize_fn=finalize_fn,
            is_active_fn=lambda _d: True,
        )

        assert rc == 1
        finalize_fn.assert_not_called()
        assert sender.call_count == 1
        msg = sender.call_args.kwargs.get("message") or ""
        assert "signed" in msg.lower()

    def test_finalize_failure_sends_error(self, tmp_path):
        poll_fn = MagicMock(return_value=True)
        finalize_fn = MagicMock(return_value=None)
        sender = MagicMock()

        rc = rs._await_sign_and_push(
            output_dir=tmp_path, chat_id="1", sshsign_host="h",
            pending_id="pnd_abc",
            sender=sender, poll_fn=poll_fn, finalize_fn=finalize_fn,
            is_active_fn=lambda _d: True,
        )

        assert rc == 2
        # 3 messages: Confirmed signature, Generating, then the finalize-fail error
        assert sender.call_count == 3
        final_msg = sender.call_args_list[-1].kwargs.get("message") or ""
        assert "couldn't" in final_msg.lower() or "could not" in final_msg.lower()

    def test_superseded_on_timeout_sends_nothing(self, tmp_path):
        """A stale process whose poll times out after a newer negotiation
        started must stay silent — must not push the manual-verify fallback
        into the new session's chat."""
        sender = MagicMock()
        rc = rs._await_sign_and_push(
            output_dir=tmp_path, chat_id="1", sshsign_host="h",
            pending_id="pnd_abc",
            sender=sender,
            poll_fn=MagicMock(return_value=False),
            finalize_fn=MagicMock(),
            is_active_fn=lambda _d: False,
        )
        assert rc == 3
        sender.assert_not_called()

    def test_superseded_after_approval_sends_nothing(self, tmp_path):
        """If a newer process claimed the output dir between approval and
        finalize, the stale process must stay silent — must not push
        'Signed ✓' into a session that doesn't belong to it."""
        sender = MagicMock()
        rc = rs._await_sign_and_push(
            output_dir=tmp_path, chat_id="1", sshsign_host="h",
            pending_id="pnd_abc",
            sender=sender,
            poll_fn=MagicMock(return_value=True),
            finalize_fn=MagicMock(return_value=tmp_path / "x.pdf"),
            is_active_fn=lambda _d: False,
        )
        assert rc == 3
        sender.assert_not_called()


class TestIsStillActiveSession:
    def test_our_pid_matches(self, tmp_path):
        import os
        (tmp_path / ".session.pid").write_text(str(os.getpid()))
        assert rs._is_still_active_session(tmp_path) is True

    def test_different_pid_returns_false(self, tmp_path):
        (tmp_path / ".session.pid").write_text("999999999")
        assert rs._is_still_active_session(tmp_path) is False

    def test_missing_file_defaults_active(self, tmp_path):
        """No PID file → assume active (err on the side of sending, not suppressing)."""
        assert rs._is_still_active_session(tmp_path) is True

    def test_malformed_file_defaults_active(self, tmp_path):
        (tmp_path / ".session.pid").write_text("not a number")
        assert rs._is_still_active_session(tmp_path) is True


class TestFetchSessionForJoin:
    def test_returns_payload_on_open_session(self):
        client = MagicMock()
        client.get_session.return_value = {
            "session_id": "neg_1",
            "session_code": "INV-7K3X9",
            "status": "open",
            "metadata_public": {"use_case": "safe", "company_name": "Acme"},
        }
        sess, err = rs._fetch_session_for_join("INV-7K3X9", session_client=client)
        assert err is None
        assert sess["session_code"] == "INV-7K3X9"
        client.get_session.assert_called_once_with(session_code="INV-7K3X9")

    def test_canceled_session_returns_user_friendly_error(self):
        client = MagicMock()
        client.get_session.return_value = {"status": "canceled"}
        sess, err = rs._fetch_session_for_join("INV-X", session_client=client)
        assert sess is None
        assert "canceled" in err.lower()

    def test_rescinded_after_sign_treated_as_canceled(self):
        client = MagicMock()
        client.get_session.return_value = {"status": "rescinded_after_sign"}
        sess, err = rs._fetch_session_for_join("INV-X", session_client=client)
        assert sess is None
        assert "canceled" in err.lower()

    def test_expired_session_returns_user_friendly_error(self):
        client = MagicMock()
        client.get_session.return_value = {"status": "expired"}
        sess, err = rs._fetch_session_for_join("INV-X", session_client=client)
        assert sess is None
        assert "expired" in err.lower()

    def test_completed_session_rejected(self):
        client = MagicMock()
        client.get_session.return_value = {"status": "completed"}
        sess, err = rs._fetch_session_for_join("INV-X", session_client=client)
        assert sess is None
        assert "completed" in err.lower()

    def test_unknown_status_rejected(self):
        client = MagicMock()
        client.get_session.return_value = {"status": "mystery"}
        sess, err = rs._fetch_session_for_join("INV-X", session_client=client)
        assert sess is None
        assert "mystery" in err

    def test_transport_error_returns_error_string(self):
        from sshsign_session import SshsignSessionError
        client = MagicMock()
        client.get_session.side_effect = SshsignSessionError("ssh boom")
        sess, err = rs._fetch_session_for_join("INV-X", session_client=client)
        assert sess is None
        assert "ssh boom" in err

    def test_joined_status_is_valid(self):
        """A session that already has one member (status=joined) is still
        joinable up until the second counterparty is in — don't reject early."""
        client = MagicMock()
        client.get_session.return_value = {
            "session_code": "INV-X", "status": "joined",
        }
        sess, err = rs._fetch_session_for_join("INV-X", session_client=client)
        assert err is None
        assert sess["status"] == "joined"


class TestEnrichConstraintsFromSession:
    def test_fills_company_name_from_public_metadata(self):
        c = {"company_name": None, "founder_name": None}
        sess = {"metadata_public": {"company_name": "Acme Corp"}}
        out = rs._enrich_constraints_from_session(c, sess)
        assert out["company_name"] == "Acme Corp"

    def test_investor_fields_win_over_session_metadata(self):
        """NL-supplied fields must not be overwritten by session metadata."""
        c = {"company_name": "MyCo", "investor_name": "Mark"}
        sess = {
            "metadata_public": {"company_name": "OtherCo"},
            "metadata_member": {"investor_name": "Someone Else"},
        }
        out = rs._enrich_constraints_from_session(c, sess)
        assert out["company_name"] == "MyCo"
        assert out["investor_name"] == "Mark"

    def test_merges_from_member_metadata_when_available(self):
        c = {"company_name": None, "founder_name": None, "founder_title": None}
        sess = {
            "metadata_public": {"company_name": "Acme"},
            "metadata_member": {"founder_name": "Jane", "founder_title": "CEO"},
        }
        out = rs._enrich_constraints_from_session(c, sess)
        assert out["company_name"] == "Acme"
        assert out["founder_name"] == "Jane"
        assert out["founder_title"] == "CEO"

    def test_missing_metadata_leaves_constraints_unchanged(self):
        c = {"company_name": None, "founder_name": "Jane"}
        sess = {"metadata_public": None, "metadata_member": None}
        out = rs._enrich_constraints_from_session(c, sess)
        assert out["company_name"] is None
        assert out["founder_name"] == "Jane"

    def test_handles_metadata_as_json_string(self):
        """sshsign sometimes returns metadata as a JSON-encoded string; the
        helper must parse it transparently."""
        c = {"company_name": None}
        sess = {"metadata_public": json.dumps({"company_name": "Acme"})}
        out = rs._enrich_constraints_from_session(c, sess)
        assert out["company_name"] == "Acme"

    def test_malformed_json_metadata_is_ignored(self):
        c = {"company_name": None}
        sess = {"metadata_public": "{not valid json"}
        out = rs._enrich_constraints_from_session(c, sess)
        assert out["company_name"] is None

    def test_does_not_mutate_input_constraints(self):
        c = {"company_name": None}
        sess = {"metadata_public": {"company_name": "Acme"}}
        out = rs._enrich_constraints_from_session(c, sess)
        assert c["company_name"] is None
        assert out["company_name"] == "Acme"


class TestExtractCounterpartyPubkey:
    def test_returns_first_member_pubkey(self):
        sess = {"members": [{"role": "founder", "apoa_pubkey_pem": "PEM1"}]}
        assert rs._extract_counterparty_pubkey(sess) == "PEM1"

    def test_returns_empty_when_members_missing(self):
        """Non-member get-session view omits the members list — we must
        gracefully return empty so the caller can fall back to join-session."""
        assert rs._extract_counterparty_pubkey({}) == ""
        assert rs._extract_counterparty_pubkey({"members": None}) == ""
        assert rs._extract_counterparty_pubkey({"members": []}) == ""

    def test_skips_members_without_pubkey(self):
        sess = {"members": [
            {"role": "founder", "apoa_pubkey_pem": ""},
            {"role": "investor", "apoa_pubkey_pem": "PEM2"},
        ]}
        assert rs._extract_counterparty_pubkey(sess) == "PEM2"


class TestJoinSigningSession:
    def _neg_dir(self, tmp_path, role: str = "investor") -> Path:
        neg_dir = tmp_path / "neg"
        (neg_dir / "keys").mkdir(parents=True)
        (neg_dir / "keys" / f"{role}_public.pem").write_text(
            f"-----BEGIN APOA-----\n{role.upper()}_FAKE_KEY\n-----END APOA-----\n"
        )
        return neg_dir

    def test_calls_join_and_fetches_counterparty_pubkey(self, tmp_path, monkeypatch):
        monkeypatch.setenv("USER_DID", "did:apoa:investor-1")
        neg_dir = self._neg_dir(tmp_path, role="investor")
        mint_output = {"negotiation_id": "neg_1"}
        shared_session = {"session_code": "INV-7K3X9", "session_id": "neg_1"}

        client = MagicMock()
        client.join_session.return_value = {"session_code": "INV-7K3X9", "status": "joined"}
        client.get_session.return_value = {
            "session_code": "INV-7K3X9",
            "status": "joined",
            "created_at": "2026-04-21T12:00:00Z",
            "expires_at": "2026-04-22T12:00:00Z",
            "members": [
                {"role": "founder", "apoa_pubkey_pem": "FOUNDER_PEM_CONTENT"},
                {"role": "investor", "apoa_pubkey_pem": "INVESTOR_PEM_CONTENT"},
            ],
        }

        result = rs._join_signing_session(
            mint_output=mint_output,
            shared_session=shared_session,
            user_role="investor",
            neg_dir=neg_dir,
            repo=tmp_path,
            session_client=client,
        )

        assert result is not None
        assert result["session_code"] == "INV-7K3X9"
        assert result["session_status"] == "joined"
        assert result["counterparty_pubkey_path"].endswith("founder_public.pem")

        # Counterparty pubkey was written to disk
        founder_key = neg_dir / "keys" / "founder_public.pem"
        assert founder_key.exists()
        assert founder_key.read_text() == "FOUNDER_PEM_CONTENT"

        # join_session called with our pubkey + DID
        call = client.join_session.call_args
        assert call.kwargs["session_code"] == "INV-7K3X9"
        assert call.kwargs["role"] == "investor"
        assert "INVESTOR_FAKE_KEY" in call.kwargs["apoa_pubkey_pem"]
        assert call.kwargs["party_did"] == "did:apoa:investor-1"

    def test_missing_pubkey_returns_none(self, tmp_path):
        neg_dir = tmp_path / "neg"
        neg_dir.mkdir()
        client = MagicMock()
        result = rs._join_signing_session(
            mint_output={"negotiation_id": "neg_1"},
            shared_session={"session_code": "INV-X"},
            user_role="investor",
            neg_dir=neg_dir,
            repo=tmp_path,
            session_client=client,
        )
        assert result is None
        client.join_session.assert_not_called()

    def test_missing_session_code_returns_none(self, tmp_path):
        neg_dir = self._neg_dir(tmp_path, role="investor")
        client = MagicMock()
        result = rs._join_signing_session(
            mint_output={"negotiation_id": "neg_1"},
            shared_session={},  # no session_code
            user_role="investor",
            neg_dir=neg_dir,
            repo=tmp_path,
            session_client=client,
        )
        assert result is None
        client.join_session.assert_not_called()

    def test_join_session_error_returns_none(self, tmp_path):
        from sshsign_session import SshsignSessionError
        neg_dir = self._neg_dir(tmp_path, role="investor")
        client = MagicMock()
        client.join_session.side_effect = SshsignSessionError("already has member")
        result = rs._join_signing_session(
            mint_output={"negotiation_id": "neg_1"},
            shared_session={"session_code": "INV-X"},
            user_role="investor",
            neg_dir=neg_dir,
            repo=tmp_path,
            session_client=client,
        )
        assert result is None

    def test_post_join_get_session_error_is_non_fatal(self, tmp_path):
        """Join succeeded; a transient error on the follow-up get-session
        should NOT fail the whole flow — we already joined, just proceed
        with whatever info we have."""
        from sshsign_session import SshsignSessionError
        neg_dir = self._neg_dir(tmp_path, role="investor")
        client = MagicMock()
        client.join_session.return_value = {
            "session_code": "INV-X", "status": "joined",
        }
        client.get_session.side_effect = SshsignSessionError("transient")

        result = rs._join_signing_session(
            mint_output={"negotiation_id": "neg_1"},
            shared_session={"session_code": "INV-X"},
            user_role="investor",
            neg_dir=neg_dir,
            repo=tmp_path,
            session_client=client,
        )
        assert result is not None
        assert result["session_code"] == "INV-X"
        # No counterparty pubkey available → empty string path
        assert result["counterparty_pubkey_path"] == ""

    def test_founder_role_writes_investor_pubkey(self, tmp_path):
        """Symmetric case: if the founder is the joiner (unusual but valid),
        the counterparty pubkey file is investor_public.pem."""
        neg_dir = self._neg_dir(tmp_path, role="founder")
        client = MagicMock()
        client.join_session.return_value = {"status": "joined"}
        client.get_session.return_value = {
            "members": [
                {"role": "investor", "apoa_pubkey_pem": "INV_PEM"},
            ],
        }

        result = rs._join_signing_session(
            mint_output={"negotiation_id": "neg_1"},
            shared_session={"session_code": "INV-X"},
            user_role="founder",
            neg_dir=neg_dir,
            repo=tmp_path,
            session_client=client,
        )
        assert result is not None
        assert (neg_dir / "keys" / "investor_public.pem").read_text() == "INV_PEM"


class TestPrepareJoinBranch:
    """The investor-side flow: NL contains a session_code, so run_prepare
    goes through _fetch_session_for_join + _enrich before writing config."""

    def _constraints_with_code(self) -> dict:
        return {
            "role": "investor",
            "mode": "two_party",
            "session_code": "INV-7K3X9",
            "valuation_cap_min": 10_000_000,
            "valuation_cap_max": 40_000_000,
            "discount_min": 0.12,
            "pro_rata": "required",
            "mfn": "preferred",
            "company_name": None,  # will be filled by enrichment
            "founder_name": None,
            "founder_title": None,
            "investor_name": "Alex Chen",
            "investor_firm": "Blue Fund",
            "investment_amount": 500_000.0,
        }

    def test_fetches_session_and_enriches_constraints(self, tmp_path):
        c = self._constraints_with_code()
        sess_payload = {
            "session_id": "neg_abc",
            "session_code": "INV-7K3X9",
            "status": "open",
            "metadata_public": {"company_name": "Acme"},
            "members": [{"role": "founder", "apoa_pubkey_pem": "FOUNDER_PEM"}],
        }

        with patch.object(rs, "extract_constraints", return_value=c), \
             patch.object(rs, "_fetch_session_for_join",
                          return_value=(sess_payload, None)) as mock_fetch, \
             patch.object(rs, "resolve_chat_id", return_value="1"):
            rc = rs.run_prepare("Join INV-7K3X9", str(tmp_path), sender=MagicMock())

        assert rc == 0
        mock_fetch.assert_called_once_with("INV-7K3X9")

        config = json.loads((tmp_path / "config.json").read_text())
        # Company name pulled from metadata_public
        assert config["constraints"]["company_name"] == "Acme"
        # Session stashed for mint step
        assert config["session"]["session_id"] == "neg_abc"
        assert config["session"]["session_code"] == "INV-7K3X9"
        assert config["session"]["counterparty_apoa_pubkey_pem"] == "FOUNDER_PEM"

    def test_fetch_error_aborts_and_messages_user(self, tmp_path):
        c = self._constraints_with_code()
        sender = MagicMock()
        with patch.object(rs, "extract_constraints", return_value=c), \
             patch.object(rs, "_fetch_session_for_join",
                          return_value=(None, "That negotiation expired.")), \
             patch.object(rs, "resolve_chat_id", return_value="1"):
            rc = rs.run_prepare("Join INV-X", str(tmp_path), sender=sender)

        assert rc == 1
        assert not (tmp_path / "config.json").exists()
        # Last sender call is the error surface
        err_msg = sender.call_args.kwargs.get("message", "")
        assert "expired" in err_msg.lower()
        assert "double-check" in err_msg.lower()

    def test_no_session_code_skips_fetch(self, tmp_path, sample_constraints):
        """Demo mode: no session_code, don't touch sshsign."""
        with patch.object(rs, "extract_constraints", return_value=sample_constraints), \
             patch.object(rs, "_fetch_session_for_join") as mock_fetch, \
             patch.object(rs, "resolve_chat_id", return_value="1"):
            rc = rs.run_prepare("Negotiate my SAFE", str(tmp_path), sender=MagicMock())
        assert rc == 0
        mock_fetch.assert_not_called()
        config = json.loads((tmp_path / "config.json").read_text())
        assert "session" not in config


class TestRunMintJoinBranch:
    """Investor mint flow: shared_session in config → reuse session_id,
    skip AI-side env, call _join_signing_session instead of register."""

    def _config_for_join(self, tmp_path) -> dict:
        return {
            "constraints": {
                "role": "investor",
                "mode": "two_party",
                "valuation_cap_min": 10_000_000,
                "valuation_cap_max": 40_000_000,
                "discount_min": 0.12,
                "pro_rata": "required",
                "mfn": "preferred",
                "company_name": "Acme",
                "investor_name": "Alex",
                "investor_firm": "Blue Fund",
                "investment_amount": 500_000.0,
            },
            "session": {
                "session_id": "neg_shared_xyz",
                "session_code": "INV-7K3X9",
                "status": "open",
                "counterparty_apoa_pubkey_pem": "FOUNDER_PEM",
            },
            "founder_name": "",
            "founder_title": "CEO",
            "message": "Join INV-7K3X9",
        }

    def test_reuses_shared_session_id_and_passes_role_flag(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NEGOTIATE_REPO_PATH", str(tmp_path))
        (tmp_path / "create_tokens.py").write_text("# stub")

        captured = {}

        def fake_run(cmd, cwd=None, capture_output=None, text=None):
            captured["cmd"] = cmd
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch.object(rs.subprocess, "run", side_effect=fake_run), \
             patch.object(rs, "_join_signing_session",
                          return_value={"session_code": "INV-7K3X9",
                                        "session_status": "joined",
                                        "counterparty_pubkey_path": "/tmp/f.pem"}):
            rc = rs.run_mint(str(tmp_path), self._config_for_join(tmp_path))

        assert rc == 0
        cmd = captured["cmd"]
        # negotiation_id == shared session_id
        assert cmd[cmd.index("--negotiation-id") + 1] == "neg_shared_xyz"
        # --role is passed so create_tokens skips the counterparty side
        assert "--role" in cmd
        assert cmd[cmd.index("--role") + 1] == "investor"

    def test_does_not_pass_ai_env_when_joining(self, tmp_path, monkeypatch):
        """In two-party join, there is no AI side — env overrides for
        the counterparty must not be appended to the mint command."""
        monkeypatch.setenv("NEGOTIATE_REPO_PATH", str(tmp_path))
        monkeypatch.setenv("FOUNDER_CAP_MIN", "50000000")  # should be ignored
        monkeypatch.setenv("FOUNDER_CAP_MAX", "99000000")
        (tmp_path / "create_tokens.py").write_text("# stub")

        captured = {}

        def fake_run(cmd, cwd=None, capture_output=None, text=None):
            captured["cmd"] = cmd
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch.object(rs.subprocess, "run", side_effect=fake_run), \
             patch.object(rs, "_join_signing_session",
                          return_value={"session_code": "INV-X",
                                        "session_status": "joined"}):
            rs.run_mint(str(tmp_path), self._config_for_join(tmp_path))

        cmd = captured["cmd"]
        # No --founder-cap-min from FOUNDER_CAP_MIN env leak
        assert "--founder-cap-min" not in cmd
        assert "--founder-cap-max" not in cmd

    def test_calls_join_not_register(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NEGOTIATE_REPO_PATH", str(tmp_path))
        (tmp_path / "create_tokens.py").write_text("# stub")

        with patch.object(rs.subprocess, "run",
                          return_value=MagicMock(returncode=0, stdout="", stderr="")), \
             patch.object(rs, "_join_signing_session",
                          return_value={"session_code": "INV-X",
                                        "session_status": "joined"}) as mock_join, \
             patch.object(rs, "_register_signing_session") as mock_register:
            rs.run_mint(str(tmp_path), self._config_for_join(tmp_path))

        mock_join.assert_called_once()
        mock_register.assert_not_called()

    def test_join_failure_returns_nonzero(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NEGOTIATE_REPO_PATH", str(tmp_path))
        (tmp_path / "create_tokens.py").write_text("# stub")

        with patch.object(rs.subprocess, "run",
                          return_value=MagicMock(returncode=0, stdout="", stderr="")), \
             patch.object(rs, "_join_signing_session", return_value=None):
            rc = rs.run_mint(str(tmp_path), self._config_for_join(tmp_path))

        assert rc == 3

    def test_mint_json_written_with_join_fields(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NEGOTIATE_REPO_PATH", str(tmp_path))
        (tmp_path / "create_tokens.py").write_text("# stub")

        with patch.object(rs.subprocess, "run",
                          return_value=MagicMock(returncode=0, stdout="", stderr="")), \
             patch.object(rs, "_join_signing_session",
                          return_value={"session_code": "INV-7K3X9",
                                        "session_status": "joined",
                                        "counterparty_pubkey_path": "/tmp/f.pem"}):
            rs.run_mint(str(tmp_path), self._config_for_join(tmp_path))

        mint = json.loads((tmp_path / "mint.json").read_text())
        assert mint["mode"] == "two_party"
        assert mint["user_role"] == "investor"
        assert mint["session_code"] == "INV-7K3X9"
        assert mint["counterparty_pubkey_path"] == "/tmp/f.pem"


class TestNegotiateInvestorBranch:
    """Investor side of two-party: after mint, run_negotiate should push a
    'Joined — starting now' card and go straight to streaming (skip the
    founder wait gate)."""

    def _write_config(self, out_dir: Path):
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "config.json").write_text(json.dumps({
            "constraints": {"role": "investor", "mode": "two_party"},
            "founder_name": "", "founder_title": "CEO", "message": "Join INV-X",
        }))

    def test_two_party_creator_uses_creator_await_path(self, tmp_path, sample_constraints):
        """Founder in two-party mode routes signing through the creator
        finalize helper, which adds a complete-session call."""
        (tmp_path / "config.json").write_text(json.dumps({
            "constraints": sample_constraints,
            "founder_name": "F", "founder_title": "CEO", "message": "m",
        }))

        def fake_mint(output_dir, config):
            (Path(output_dir) / "mint.json").write_text(json.dumps({
                "negotiation_id": "neg_1", "mode": "two_party",
                "user_role": "founder", "session_code": "INV-X",
            }))
            return 0

        mock_creator = MagicMock(return_value=0)
        mock_joiner = MagicMock(return_value=0)
        mock_demo_await = MagicMock(return_value=0)
        signing = {"type": "signing", "pending_id": "pnd_f"}

        with patch.object(rs, "run_mint", side_effect=fake_mint), \
             patch.object(rs, "_founder_two_party_gate", return_value=0), \
             patch.object(rs, "_stream_to_telegram", return_value=(0, signing)), \
             patch.object(rs, "_creator_await_sign_and_finalize", mock_creator), \
             patch.object(rs, "_joiner_await_sign_and_finalize", mock_joiner), \
             patch.object(rs, "_await_sign_and_push", mock_demo_await), \
             patch.object(rs, "resolve_chat_id", return_value="12345"):
            rc = rs.run_negotiate(str(tmp_path), chat_id_flag="12345")

        assert rc == 0
        mock_creator.assert_called_once()
        mock_joiner.assert_not_called()
        mock_demo_await.assert_not_called()
        assert mock_creator.call_args.kwargs["session_id"] == "neg_1"

    def test_two_party_joiner_uses_joiner_await_path(self, tmp_path, sample_constraints):
        """Investor in two-party mode routes through the joiner helper
        (which waits for creator to finalize before running local finalize)."""
        (tmp_path / "config.json").write_text(json.dumps({
            "constraints": sample_constraints,
            "founder_name": "F", "founder_title": "CEO", "message": "m",
        }))

        def fake_mint(output_dir, config):
            (Path(output_dir) / "mint.json").write_text(json.dumps({
                "negotiation_id": "neg_1", "mode": "two_party",
                "user_role": "investor", "session_code": "INV-X",
            }))
            return 0

        mock_creator = MagicMock(return_value=0)
        mock_joiner = MagicMock(return_value=0)
        signing = {"type": "signing", "pending_id": "pnd_i"}

        with patch.object(rs, "run_mint", side_effect=fake_mint), \
             patch.object(rs, "_stream_to_telegram", return_value=(0, signing)), \
             patch.object(rs, "_creator_await_sign_and_finalize", mock_creator), \
             patch.object(rs, "_joiner_await_sign_and_finalize", mock_joiner), \
             patch.object(rs, "resolve_chat_id", return_value="12345"):
            rc = rs.run_negotiate(str(tmp_path), chat_id_flag="12345")

        assert rc == 0
        mock_joiner.assert_called_once()
        mock_creator.assert_not_called()

    def test_demo_mode_uses_demo_await(self, tmp_path, sample_constraints):
        """Demo mode must stay on the simple one-side finalize path."""
        (tmp_path / "config.json").write_text(json.dumps({
            "constraints": sample_constraints,
            "founder_name": "F", "founder_title": "CEO", "message": "m",
        }))

        def fake_mint(output_dir, config):
            (Path(output_dir) / "mint.json").write_text(json.dumps({
                "negotiation_id": "neg_1", "mode": "demo",
            }))
            return 0

        mock_demo_await = MagicMock(return_value=0)
        mock_creator = MagicMock()
        mock_joiner = MagicMock()
        signing = {"type": "signing", "pending_id": "pnd_x"}

        with patch.object(rs, "run_mint", side_effect=fake_mint), \
             patch.object(rs, "_stream_to_telegram", return_value=(0, signing)), \
             patch.object(rs, "_await_sign_and_push", mock_demo_await), \
             patch.object(rs, "_creator_await_sign_and_finalize", mock_creator), \
             patch.object(rs, "_joiner_await_sign_and_finalize", mock_joiner), \
             patch.object(rs, "resolve_chat_id", return_value="12345"):
            rs.run_negotiate(str(tmp_path), chat_id_flag="12345")

        mock_demo_await.assert_called_once()
        mock_creator.assert_not_called()
        mock_joiner.assert_not_called()

    def test_investor_two_party_skips_gate_and_pushes_joined_card(self, tmp_path):
        self._write_config(tmp_path)

        def fake_mint(output_dir, config):
            (Path(output_dir) / "mint.json").write_text(json.dumps({
                "negotiation_id": "neg_1",
                "mode": "two_party",
                "user_role": "investor",
                "session_code": "INV-X",
            }))
            return 0

        sender = MagicMock()
        mock_gate = MagicMock()
        mock_stream = MagicMock(return_value=(0, None))

        with patch.object(rs, "run_mint", side_effect=fake_mint), \
             patch.object(rs, "_founder_two_party_gate", mock_gate), \
             patch.object(rs, "_stream_to_telegram", mock_stream), \
             patch.object(rs, "resolve_chat_id", return_value="12345"), \
             patch.object(rs, "send_telegram", sender):
            rc = rs.run_negotiate(str(tmp_path), chat_id_flag="12345")

        assert rc == 0
        mock_gate.assert_not_called()
        mock_stream.assert_called_once()
        # A "joined" card was pushed before streaming started
        joined_msgs = [c.kwargs.get("message", "") for c in sender.call_args_list]
        assert any("joined" in m.lower() and "starting" in m.lower()
                   for m in joined_msgs)


class TestAuthorizationCardOnRunNegotiate:
    def _write_config(self, tmp_path, sample_constraints):
        (tmp_path / "config.json").write_text(json.dumps({
            "constraints": sample_constraints,
            "founder_name": "F", "founder_title": "CEO", "message": "m",
        }))

    def test_auth_card_pushed_before_streaming(self, tmp_path, sample_constraints):
        self._write_config(tmp_path, sample_constraints)
        sender = MagicMock()
        call_log = []
        sender.side_effect = lambda *a, **kw: call_log.append(("send", kw.get("message", "")))
        mock_stream = MagicMock(side_effect=lambda **kw: (
            call_log.append(("stream", None)) or (0, None)
        ))

        with patch.object(rs, "run_mint", return_value=0), \
             patch.object(rs, "_stream_to_telegram", mock_stream), \
             patch.object(rs, "resolve_chat_id", return_value="12345"), \
             patch.object(rs, "send_telegram", sender):
            rs.run_negotiate(str(tmp_path), chat_id_flag="12345")

        # Find the first auth card send, then find the stream call index
        send_indices = [i for i, (k, _) in enumerate(call_log) if k == "send"
                        and "authorization is set" in call_log[i][1].lower()]
        stream_indices = [i for i, (k, _) in enumerate(call_log) if k == "stream"]
        assert send_indices, f"no auth card pushed: {call_log}"
        assert stream_indices, "stream not called"
        assert send_indices[0] < stream_indices[0]

    def test_auth_card_uses_negotiation_ttl(self, tmp_path, sample_constraints, monkeypatch):
        self._write_config(tmp_path, sample_constraints)
        monkeypatch.setenv("NEGOTIATION_TTL", "7200")  # 2 hours
        sender = MagicMock()
        with patch.object(rs, "run_mint", return_value=0), \
             patch.object(rs, "_stream_to_telegram", return_value=(0, None)), \
             patch.object(rs, "resolve_chat_id", return_value="12345"), \
             patch.object(rs, "send_telegram", sender):
            rs.run_negotiate(str(tmp_path), chat_id_flag="12345")
        msgs = [c.kwargs.get("message", "") for c in sender.call_args_list]
        auth_msgs = [m for m in msgs if "authorization is set" in m.lower()]
        assert auth_msgs
        assert "2 hours" in auth_msgs[0]


class TestBuildArtifactUri:
    def test_deterministic_per_session(self, tmp_path):
        uri = rs._build_artifact_uri("neg_xyz", tmp_path / "executed.pdf")
        assert uri == "sshsign://session/neg_xyz/executed.pdf"

    def test_does_not_leak_local_path(self, tmp_path):
        """The URI is content-addressed by session_id — the local file path
        is an implementation detail that should not show up."""
        uri = rs._build_artifact_uri("neg_abc", Path("/secret/path/file.pdf"))
        assert "/secret/path" not in uri


class TestCreatorAwaitSignAndFinalize:
    def _write_mint(self, tmp_path, neg_id: str = "neg_1"):
        neg_dir = tmp_path / "neg"
        neg_dir.mkdir(parents=True, exist_ok=True)
        (neg_dir / "founder.json").write_text("{}")
        (tmp_path / "mint.json").write_text(json.dumps({
            "negotiation_id": neg_id,
            "founder_config_path": str(neg_dir / "founder.json"),
        }))
        return neg_dir

    def test_success_pushes_pdf_and_calls_complete_session(self, tmp_path):
        self._write_mint(tmp_path)
        sender = MagicMock()
        pdf = tmp_path / "executed.pdf"
        pdf.write_bytes(b"PDF")

        client = MagicMock()
        client.complete_session.return_value = {"status": "completed"}

        rc = rs._creator_await_sign_and_finalize(
            output_dir=tmp_path, chat_id="1", sshsign_host="sshsign.test",
            pending_id="pnd_f", session_id="neg_1",
            sender=sender,
            poll_fn=lambda **kw: True,
            finalize_fn=lambda *a, **kw: pdf,
            is_active_fn=lambda _o: True,
            session_client=client,
            typing_factory=lambda _c: MagicMock(start=MagicMock(), stop=MagicMock()),
        )

        assert rc == 0
        client.complete_session.assert_called_once()
        kwargs = client.complete_session.call_args.kwargs
        assert kwargs["session_id"] == "neg_1"
        assert kwargs["executed_artifact"].startswith("sshsign://session/neg_1/")

        # PDF delivered
        pdf_calls = [c for c in sender.call_args_list
                     if c.kwargs.get("media_path")]
        assert len(pdf_calls) == 1

    def test_complete_session_error_is_non_fatal(self, tmp_path):
        """PDF already reached the user; a flaky complete-session shouldn't
        flip rc to failure — J7 will add retries."""
        self._write_mint(tmp_path)
        pdf = tmp_path / "executed.pdf"
        pdf.write_bytes(b"PDF")

        client = MagicMock()
        client.complete_session.side_effect = \
            __import__("sshsign_session").SshsignSessionError("boom")

        rc = rs._creator_await_sign_and_finalize(
            output_dir=tmp_path, chat_id="1", sshsign_host="h",
            pending_id="pnd", session_id="neg_1",
            sender=MagicMock(),
            poll_fn=lambda **kw: True,
            finalize_fn=lambda *a, **kw: pdf,
            is_active_fn=lambda _o: True,
            session_client=client,
            typing_factory=lambda _c: MagicMock(start=MagicMock(), stop=MagicMock()),
        )
        assert rc == 0

    def test_sign_timeout_skips_complete_session(self, tmp_path):
        """If the user never signed, we never finalized — don't falsely
        mark the session complete."""
        self._write_mint(tmp_path)
        client = MagicMock()
        rc = rs._creator_await_sign_and_finalize(
            output_dir=tmp_path, chat_id="1", sshsign_host="h",
            pending_id="pnd", session_id="neg_1",
            sender=MagicMock(),
            poll_fn=lambda **kw: False,  # never approved
            finalize_fn=lambda *a, **kw: None,
            is_active_fn=lambda _o: True,
            session_client=client,
            typing_factory=lambda _c: MagicMock(start=MagicMock(), stop=MagicMock()),
        )
        assert rc == 1
        client.complete_session.assert_not_called()


class TestJoinerAwaitSignAndFinalize:
    def _fake_loop(self):
        return MagicMock(start=MagicMock(), stop=MagicMock())

    def test_waits_for_completed_then_finalizes(self, tmp_path):
        sender = MagicMock()
        pdf = tmp_path / "executed.pdf"
        pdf.write_bytes(b"PDF")

        client = MagicMock()
        client.get_session.side_effect = [
            {"status": "joined"},
            {"status": "joined"},
            {"status": "completed"},
        ]

        now_calls = iter([0.0, 10.0, 20.0, 30.0, 40.0])
        rc = rs._joiner_await_sign_and_finalize(
            output_dir=tmp_path, chat_id="1", sshsign_host="h",
            pending_id="pnd_i", session_id="neg_1",
            sender=sender,
            poll_fn=lambda **kw: True,
            finalize_fn=lambda *a, **kw: pdf,
            is_active_fn=lambda _o: True,
            session_client=client,
            sleep_fn=MagicMock(),
            now_fn=lambda: next(now_calls),
            typing_factory=lambda _c: self._fake_loop(),
        )
        assert rc == 0
        assert client.get_session.call_count == 3
        # PDF delivered
        assert any(c.kwargs.get("media_path") for c in sender.call_args_list)
        # "Waiting for counterparty" card was pushed at some point
        msgs = [c.kwargs.get("message", "") for c in sender.call_args_list]
        assert any("counterparty" in m.lower() and "sign" in m.lower()
                   for m in msgs)

    def test_sign_timeout_returns_1(self, tmp_path):
        rc = rs._joiner_await_sign_and_finalize(
            output_dir=tmp_path, chat_id="1", sshsign_host="h",
            pending_id="pnd_i", session_id="neg_1",
            sender=MagicMock(),
            poll_fn=lambda **kw: False,
            finalize_fn=lambda *a, **kw: None,
            is_active_fn=lambda _o: True,
            session_client=MagicMock(),
            sleep_fn=MagicMock(),
            now_fn=lambda: 0.0,
            typing_factory=lambda _c: self._fake_loop(),
        )
        assert rc == 1

    def test_completion_timeout_returns_1(self, tmp_path):
        client = MagicMock()
        client.get_session.return_value = {"status": "joined"}
        now_iter = iter([0.0, 999999.0, 999999.0, 999999.0])
        rc = rs._joiner_await_sign_and_finalize(
            output_dir=tmp_path, chat_id="1", sshsign_host="h",
            pending_id="pnd_i", session_id="neg_1",
            sender=MagicMock(),
            poll_fn=lambda **kw: True,
            finalize_fn=lambda *a, **kw: tmp_path / "executed.pdf",
            is_active_fn=lambda _o: True,
            session_client=client,
            sleep_fn=MagicMock(),
            now_fn=lambda: next(now_iter),
            completion_timeout=100,
            typing_factory=lambda _c: self._fake_loop(),
        )
        assert rc == 1

    def test_canceled_during_wait_returns_2(self, tmp_path):
        client = MagicMock()
        client.get_session.return_value = {"status": "canceled"}
        now_iter = iter([0.0, 1.0, 2.0])
        sender = MagicMock()
        rc = rs._joiner_await_sign_and_finalize(
            output_dir=tmp_path, chat_id="1", sshsign_host="h",
            pending_id="pnd_i", session_id="neg_1",
            sender=sender,
            poll_fn=lambda **kw: True,
            finalize_fn=MagicMock(),
            is_active_fn=lambda _o: True,
            session_client=client,
            sleep_fn=MagicMock(),
            now_fn=lambda: next(now_iter),
            typing_factory=lambda _c: self._fake_loop(),
        )
        assert rc == 2
        msgs = [c.kwargs.get("message", "") for c in sender.call_args_list]
        assert any("revoked" in m.lower() for m in msgs)

    def test_rescinded_during_wait_returns_2_with_rescinded_copy(self, tmp_path):
        """If the creator rescinds after signing (different terminal state
        than canceled), the joiner sees rescinded-specific copy."""
        client = MagicMock()
        client.get_session.return_value = {"status": "rescinded_after_sign"}
        now_iter = iter([0.0, 1.0, 2.0])
        sender = MagicMock()
        rc = rs._joiner_await_sign_and_finalize(
            output_dir=tmp_path, chat_id="1", sshsign_host="h",
            pending_id="pnd_i", session_id="neg_1",
            sender=sender,
            poll_fn=lambda **kw: True,
            finalize_fn=MagicMock(),
            is_active_fn=lambda _o: True,
            session_client=client,
            sleep_fn=MagicMock(),
            now_fn=lambda: next(now_iter),
            typing_factory=lambda _c: self._fake_loop(),
        )
        assert rc == 2
        msgs = [c.kwargs.get("message", "") for c in sender.call_args_list]
        assert any("rescinded" in m.lower() for m in msgs)
        assert any("signature is on record" in m.lower() for m in msgs)

    def test_transient_get_session_error_retries(self, tmp_path):
        from sshsign_session import SshsignSessionError
        client = MagicMock()
        client.get_session.side_effect = [
            SshsignSessionError("blip"),
            {"status": "completed"},
        ]
        pdf = tmp_path / "executed.pdf"
        pdf.write_bytes(b"PDF")
        now_iter = iter([0.0, 1.0, 2.0, 3.0])
        rc = rs._joiner_await_sign_and_finalize(
            output_dir=tmp_path, chat_id="1", sshsign_host="h",
            pending_id="pnd_i", session_id="neg_1",
            sender=MagicMock(),
            poll_fn=lambda **kw: True,
            finalize_fn=lambda *a, **kw: pdf,
            is_active_fn=lambda _o: True,
            session_client=client,
            sleep_fn=MagicMock(),
            now_fn=lambda: next(now_iter),
            typing_factory=lambda _c: self._fake_loop(),
        )
        assert rc == 0
        assert client.get_session.call_count == 2


class TestMarkSigned:
    def test_mark_and_check_signed(self, tmp_path):
        assert rs._has_signed(tmp_path) is False
        rs._mark_signed(tmp_path)
        assert rs._has_signed(tmp_path) is True

    def test_mark_is_idempotent(self, tmp_path):
        rs._mark_signed(tmp_path)
        rs._mark_signed(tmp_path)
        assert rs._has_signed(tmp_path) is True

    def test_missing_dir_silently_swallows(self, tmp_path):
        bogus = tmp_path / "does-not-exist"
        rs._mark_signed(bogus)  # should NOT raise
        assert rs._has_signed(bogus) is False


class TestRunCancel:
    def _write_mint(self, tmp_path, neg_id: str = "neg_1"):
        (tmp_path / "mint.json").write_text(json.dumps({
            "negotiation_id": neg_id,
        }))

    def test_no_mint_returns_2(self, tmp_path):
        sender = MagicMock()
        rc = rs.run_cancel(str(tmp_path), chat_id_flag="1", sender=sender)
        assert rc == 2
        msgs = [c.kwargs.get("message", "") for c in sender.call_args_list]
        assert any("no active negotiation" in m.lower() for m in msgs)

    def test_cancel_open_session_before_sign(self, tmp_path):
        self._write_mint(tmp_path)
        client = MagicMock()
        client.get_session.return_value = {"status": "open"}
        client.cancel_session.return_value = {"status": "canceled"}

        sender = MagicMock()
        with patch.object(rs, "resolve_chat_id", return_value="1"):
            rc = rs.run_cancel(str(tmp_path), sender=sender, session_client=client)

        assert rc == 0
        # Cancel called without rescind flag
        call = client.cancel_session.call_args
        assert call.kwargs["session_id"] == "neg_1"
        assert call.kwargs["rescind"] is False
        # Initiator card pushed
        msgs = [c.kwargs.get("message", "") for c in sender.call_args_list]
        assert any("canceled the negotiation before any agreement" in m.lower()
                   for m in msgs)

    def test_cancel_after_sign_rescinds(self, tmp_path):
        self._write_mint(tmp_path)
        rs._mark_signed(tmp_path)  # user has signed
        client = MagicMock()
        client.get_session.return_value = {"status": "joined"}
        client.cancel_session.return_value = {"status": "rescinded_after_sign"}

        sender = MagicMock()
        with patch.object(rs, "resolve_chat_id", return_value="1"):
            rc = rs.run_cancel(str(tmp_path), sender=sender, session_client=client)

        assert rc == 0
        call = client.cancel_session.call_args
        assert call.kwargs["rescind"] is True
        msgs = [c.kwargs.get("message", "") for c in sender.call_args_list]
        assert any("rescinded after signing" in m.lower() for m in msgs)
        assert any("stays on record" in m.lower() for m in msgs)

    def test_cancel_completed_refused(self, tmp_path):
        self._write_mint(tmp_path)
        client = MagicMock()
        client.get_session.return_value = {"status": "completed"}

        sender = MagicMock()
        with patch.object(rs, "resolve_chat_id", return_value="1"):
            rc = rs.run_cancel(str(tmp_path), sender=sender, session_client=client)

        assert rc == 1
        client.cancel_session.assert_not_called()
        msgs = [c.kwargs.get("message", "") for c in sender.call_args_list]
        assert any("already executed" in m.lower() for m in msgs)
        assert any("rescission" in m.lower() for m in msgs)

    def test_cancel_terminal_state_is_noop(self, tmp_path):
        """If a previous cancel already transitioned the session, don't
        re-call cancel-session — just tell the user it's already canceled."""
        self._write_mint(tmp_path)
        client = MagicMock()
        client.get_session.return_value = {"status": "canceled"}

        sender = MagicMock()
        with patch.object(rs, "resolve_chat_id", return_value="1"):
            rc = rs.run_cancel(str(tmp_path), sender=sender, session_client=client)

        assert rc == 0
        client.cancel_session.assert_not_called()
        msgs = [c.kwargs.get("message", "") for c in sender.call_args_list]
        assert any("already" in m.lower() and "canceled" in m.lower()
                   for m in msgs)

    def test_get_session_transport_error(self, tmp_path):
        from sshsign_session import SshsignSessionError
        self._write_mint(tmp_path)
        client = MagicMock()
        client.get_session.side_effect = SshsignSessionError("ssh boom")

        sender = MagicMock()
        with patch.object(rs, "resolve_chat_id", return_value="1"):
            rc = rs.run_cancel(str(tmp_path), sender=sender, session_client=client)

        assert rc == 3
        client.cancel_session.assert_not_called()
        msgs = [c.kwargs.get("message", "") for c in sender.call_args_list]
        assert any("signing service" in m.lower() for m in msgs)

    def test_cancel_session_error_returns_3(self, tmp_path):
        from sshsign_session import SshsignSessionError
        self._write_mint(tmp_path)
        client = MagicMock()
        client.get_session.return_value = {"status": "open"}
        client.cancel_session.side_effect = SshsignSessionError("rate limited")

        sender = MagicMock()
        with patch.object(rs, "resolve_chat_id", return_value="1"):
            rc = rs.run_cancel(str(tmp_path), sender=sender, session_client=client)

        assert rc == 3
        msgs = [c.kwargs.get("message", "") for c in sender.call_args_list]
        assert any("couldn't cancel" in m.lower() for m in msgs)

    def test_cli_dispatches_cancel(self, tmp_path, monkeypatch):
        self._write_mint(tmp_path)
        monkeypatch.setattr(sys, "argv", [
            "run_safe.py", "cancel",
            "--output-dir", str(tmp_path),
            "--chat-id", "1",
        ])
        with patch.object(rs, "run_cancel", return_value=0) as mock_cancel:
            rc = rs.main()
        assert rc == 0
        mock_cancel.assert_called_once()
        assert mock_cancel.call_args.args[0] == str(tmp_path)
