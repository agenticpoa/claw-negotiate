from __future__ import annotations

import asyncio
import json
import subprocess

import openclaw_turn_agent as ota


def test_build_turn_prompt_frames_local_openclaw_agent():
    prompt = ota.build_turn_prompt(
        role="founder",
        constraints={
            "valuation_cap": {"min": 30_000_000, "max": 40_000_000},
            "investment_amount": {"min": 250_000, "max": 750_000},
            "discount_rate": {"min": 0.10, "max": 0.25},
            "pro_rata": {"required": True},
            "mfn": {"required": False},
        },
        history=[],
    )

    assert "user's OpenClaw agent" in prompt
    assert "$30,000,000 to $40,000,000" in prompt
    assert "$250,000 to $750,000" in prompt
    assert "Every move should have commercial substance" in prompt
    assert '"investment_amount": <integer dollars or null>' in prompt
    assert "Do not force extra rounds just for show" in prompt
    assert "Return ONLY a JSON object" in prompt


def test_parse_offer_text_strips_fences():
    out = ota.parse_offer_text("""```json
{"type":"offer","terms":{"valuation_cap":40000000,"discount_rate":0.1,"pro_rata":true,"mfn":false},"message":"Opening."}
```""")

    assert out["type"] == "offer"
    assert out["terms"]["valuation_cap"] == 40_000_000


def test_default_openclaw_backend_parses_agent_json(monkeypatch):
    monkeypatch.delenv("NEGOTIATE_SAFE_TURN_BACKEND", raising=False)
    offer_text = json.dumps({
        "type": "counter",
        "terms": {
            "valuation_cap": 35_000_000,
            "discount_rate": 0.10,
            "pro_rata": True,
            "mfn": False,
        },
        "message": "We can meet at $35M.",
    })
    payload = {
        "payloads": [{"text": offer_text, "mediaUrl": None}],
        "meta": {"provider": "openai-codex", "model": "gpt-5.4"},
    }

    def runner(cmd, **kwargs):
        assert cmd[:4] == ["openclaw", "agent", "--local", "--json"]
        assert "--session-id" in cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="logs\n" + json.dumps(payload), stderr="")

    agent = ota.OpenClawTurnAgent(
        role="founder",
        constraints={"valuation_cap": {"min": 30_000_000, "max": 40_000_000}},
        runner=runner,
    )

    offer = asyncio.run(agent.make_offer([]))
    assert offer["type"] == "counter"
    assert offer["terms"]["valuation_cap"] == 35_000_000


def test_make_validated_offer_retries_feedback():
    class FakeAgent:
        def __init__(self):
            self.calls = []

        async def make_offer(self, history, feedback=None):
            self.calls.append(list(feedback or []))
            if len(self.calls) == 1:
                return {"type": "counter", "terms": {"valuation_cap": 1}, "message": "bad"}
            return {
                "type": "counter",
                "terms": {
                    "valuation_cap": 35_000_000,
                    "discount_rate": 0.10,
                    "pro_rata": True,
                    "mfn": False,
                },
                "message": "valid",
            }

    fake = FakeAgent()

    def validate(offer):
        if "discount_rate" not in offer.get("terms", {}):
            return False, "missing discount"
        return True, ""

    def constraint_validate(_terms):
        return True, []

    offer = asyncio.run(ota.make_validated_offer(
        agent=fake,
        history=[],
        validate=validate,
        constraint_validate=constraint_validate,
    ))

    assert offer["message"] == "valid"
    assert fake.calls[1] == ["Your previous response was invalid: missing discount"]


def test_make_validated_offer_allows_early_accept_when_valid():
    class FakeAgent:
        def __init__(self):
            self.calls = []

        async def make_offer(self, history, feedback=None):
            self.calls.append(list(feedback or []))
            return {
                "type": "accept",
                "terms": {
                    "valuation_cap": 30_000_000,
                    "discount_rate": 0.15,
                    "pro_rata": True,
                    "mfn": False,
                },
                "message": "Accepted.",
            }

    def validate(_offer):
        return True, ""

    def constraint_validate(_terms):
        return True, []

    history = [
        {"type": "offer", "terms": {"valuation_cap": 40_000_000}},
        {"type": "counter", "terms": {"valuation_cap": 30_000_000}},
    ]
    fake = FakeAgent()

    offer = asyncio.run(ota.make_validated_offer(
        agent=fake,
        history=history,
        validate=validate,
        constraint_validate=constraint_validate,
    ))

    assert offer["type"] == "accept"
    assert fake.calls == [[]]
