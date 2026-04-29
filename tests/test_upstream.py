from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import upstream


class TestSshHistory:
    def test_returns_list_on_success(self):
        def runner(*args, **kwargs):
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps([{"round": 0, "from": "founder"}]),
                stderr="",
            )

        assert upstream.ssh_history("neg_1", runner=runner) == [
            {"round": 0, "from": "founder"},
        ]

    def test_returns_none_on_bad_output(self):
        def runner(*args, **kwargs):
            return SimpleNamespace(returncode=0, stdout="{bad", stderr="")

        assert upstream.ssh_history("neg_1", runner=runner) is None

    def test_returns_none_on_timeout(self):
        def runner(*args, **kwargs):
            raise subprocess.TimeoutExpired("ssh", 15)

        assert upstream.ssh_history("neg_1", runner=runner) is None


class TestSynthesizeOfferEvent:
    def test_synthesizes_history_row(self):
        event = upstream.synthesize_offer_event({
            "type": "offer",
            "round": 0,
            "from": "founder",
            "metadata": json.dumps({
                "_message": "We propose this.",
                "valuation_cap": 30_000_000,
            }),
        })

        assert event == {
            "type": "offer",
            "party": "founder",
            "round": 0,
            "terms": {"valuation_cap": 30_000_000},
            "message": "We propose this.",
        }

    def test_rejects_unknown_party(self):
        assert upstream.synthesize_offer_event({
            "type": "offer",
            "round": 0,
            "from": "lawyer",
            "metadata": {},
        }) is None


class TestAugmentSigningUrl:
    def test_adds_callback(self):
        event = {
            "type": "signing",
            "approval_url": "https://sshsign.dev/approve/pnd_1?token=t",
        }

        out = upstream.augment_signing_url(event, "AgenticPOA_bot")

        assert out["approval_url"].startswith(event["approval_url"] + "&callback=")
        assert "https%3A//t.me/AgenticPOA_bot" in out["approval_url"]

    def test_missing_url_is_unchanged(self):
        event = {"type": "signing"}
        assert upstream.augment_signing_url(event, "AgenticPOA_bot") is event
