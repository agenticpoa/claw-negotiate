#!/usr/bin/env python3
"""Run exactly one due negotiation turn.

This helper is intentionally short-lived. It loads the upstream negotiate repo,
rehydrates sshsign history, verifies that the requested role is currently due,
asks that role's AI agent for one offer, validates it against that side's APOA
authorization, logs it to sshsign, emits one JSON event, and exits.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

from _stream_negotiate import _build_config, _load_upstream


MAX_VALIDATION_RETRIES = 3


def _emit(event: dict) -> None:
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def _load_module(repo: Path, name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, repo / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {filename} from {repo}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


async def _run(args: argparse.Namespace) -> int:
    repo = Path(args.negotiate_repo).resolve()
    module = _load_upstream(repo)
    config = _build_config(module, Path(args.output_dir), args.sshsign_host)
    ns = config.to_namespace()
    role = ns.role
    if role not in ("founder", "investor"):
        raise RuntimeError(f"turn-once requires distributed role, got {role!r}")

    schema = module.ProtocolSchema.load(ns.schema)
    if ns.negotiation_id:
        schema.negotiation_id = ns.negotiation_id
    state = module.NegotiationState(schema=schema)

    sshsign_client = _load_module(repo, "turn_once_sshsign_client", "sshsign_client.py")
    history_raw = sshsign_client.get_history(
        host=ns.sshsign_host,
        negotiation_id=schema.negotiation_id,
    )
    if not isinstance(history_raw, list):
        history_raw = []

    agent_history: list[dict] = []
    previous_terms: dict | None = None
    previous_tx: int | None = None
    for entry in history_raw:
        offer = module._history_entry_to_offer(entry)
        if offer.get("terms"):
            previous_terms = offer["terms"]
        previous_tx = offer.get("immudb_tx") or previous_tx
        state.record_offer(offer)
        agent_history.append(offer)

    if state.terminated:
        if state.outcome == "accepted":
            pending_file = (
                Path(ns.output_dir)
                / f"{schema.negotiation_id}_{role}_pending.txt"
            )
            if not pending_file.exists():
                module.handle_agreement(ns, schema, state, time.monotonic())
                return 0
        _emit({"type": "noop", "reason": "terminated"})
        return 0
    if state.whose_turn() != role:
        _emit({
            "type": "noop",
            "reason": "not_my_turn",
            "next_role": state.whose_turn(),
            "history_count": len(state.history),
        })
        return 0

    token_path = ns.founder_token if role == "founder" else ns.investor_token
    pubkey_path = ns.founder_pubkey if role == "founder" else ns.investor_pubkey
    if token_path:
        _token, constraints = module.load_apoa_token(token_path, pubkey_path)
    elif role == "founder":
        constraints = module.build_founder_constraints(
            cap_min=ns.founder_cap_min,
            cap_max=ns.founder_cap_max,
            discount_min=ns.founder_discount_min,
            pro_rata_required=ns.founder_pro_rata_required,
            mfn_required=ns.founder_mfn_required,
        )
    else:
        constraints = module.build_investor_constraints(
            cap_min=ns.investor_cap_min,
            cap_max=ns.investor_cap_max,
            discount_min=ns.investor_discount_min,
            discount_max=ns.investor_discount_max,
            pro_rata_required=ns.investor_pro_rata_required,
            mfn_required=ns.investor_mfn_required,
        )

    prompt_path = str(module.PROMPTS_DIR / f"{role}.txt")
    agent = module.ClaudeAgent(role=role, constraints=constraints, prompt_path=prompt_path)

    offer = None
    for attempt in range(MAX_VALIDATION_RETRIES):
        raw_offer = await agent.make_offer(agent_history)
        raw_offer["from"] = role
        raw_offer["negotiation_id"] = schema.negotiation_id
        raw_offer["round"] = state.current_round

        valid, reason = module.validate_offer_structure(raw_offer, schema)
        if not valid:
            agent_history.append({
                "role": "user",
                "content": f"Your offer was invalid: {reason}. Please try again.",
            })
            continue

        if raw_offer["type"] in ("offer", "counter"):
            constraint_valid, violations = module.validate_apoa_constraints(
                raw_offer["terms"], constraints,
            )
            if not constraint_valid:
                agent_history.append({
                    "role": "user",
                    "content": (
                        f"Your offer violates APOA constraints: {', '.join(violations)}. "
                        "Adjust your terms and try again."
                    ),
                })
                continue
        offer = raw_offer
        break

    if offer is None:
        raise RuntimeError("agent failed to produce a valid offer")

    offer["timestamp"] = module.datetime.now(module.timezone.utc).isoformat()
    offer["apoa_validated"] = True
    tx_result = sshsign_client.log_offer(
        host=ns.sshsign_host,
        negotiation_id=schema.negotiation_id,
        round_num=state.current_round,
        from_party=role,
        offer_type=offer["type"],
        metadata={**offer.get("terms", {}), "_message": offer.get("message", "")},
        previous_tx=previous_tx,
    )
    offer["immudb_tx"] = tx_result.get("immudb_tx") or tx_result.get("audit_tx_id")

    event = {
        "type": offer["type"],
        "party": role,
        "round": state.current_round,
        "terms": offer.get("terms", {}),
        "message": offer.get("message", ""),
    }
    _emit(event)

    state.record_offer(offer)
    if state.outcome == "accepted":
        elapsed = time.monotonic()
        module.handle_agreement(ns, schema, state, elapsed)
    elif state.outcome:
        _emit({"type": "outcome", "result": state.outcome})
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one shared-orchestrator turn")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--negotiate-repo", required=True)
    parser.add_argument("--sshsign-host", default=os.environ.get("SSHSIGN_HOST", "sshsign.dev"))
    args = parser.parse_args()
    try:
        return asyncio.run(_run(args))
    except Exception as e:
        sys.stderr.write(f"turn-once error: {e}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
