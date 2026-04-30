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

from openclaw_turn_agent import OpenClawTurnAgent, make_validated_offer
from _stream_negotiate import _build_config, _load_upstream


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

    agent = OpenClawTurnAgent(role=role, constraints=constraints)

    def _validate_structure(candidate: dict) -> tuple[bool, str]:
        candidate["from"] = role
        candidate["negotiation_id"] = schema.negotiation_id
        candidate["round"] = state.current_round
        return module.validate_offer_structure(candidate, schema)

    def _validate_constraints(terms: dict) -> tuple[bool, list[str]]:
        return module.validate_apoa_constraints(terms, constraints)

    offer = await make_validated_offer(
        agent=agent,
        history=agent_history,
        validate=_validate_structure,
        constraint_validate=_validate_constraints,
    )
    offer["from"] = role
    offer["negotiation_id"] = schema.negotiation_id
    offer["round"] = state.current_round

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
