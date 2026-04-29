"""Adapters around the upstream agenticpoa/negotiate engine."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.parse
from pathlib import Path

from minting import identity_value


def ssh_history(
    negotiation_id: str,
    sshsign_host: str = "sshsign.dev",
    runner=subprocess.run,
) -> list[dict] | None:
    """Return sshsign's `history --negotiation-id` rows, or None on error."""
    try:
        result = runner(
            ["ssh", sshsign_host, "history", "--negotiation-id", negotiation_id],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if result.returncode != 0 or not (result.stdout or "").strip():
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, list) else None


def synthesize_offer_event(entry: dict) -> dict | None:
    """Translate a sshsign history row into an upstream-compatible event."""
    if not isinstance(entry, dict):
        return None
    etype = entry.get("type")
    if etype not in ("offer", "counter", "accept"):
        return None
    try:
        round_num = int(entry.get("round", 0))
    except (TypeError, ValueError):
        return None
    if round_num < 0:
        return None
    party = entry.get("from") or ""
    if party not in ("founder", "investor"):
        return None

    raw_meta = entry.get("metadata")
    terms: dict = {}
    message = ""
    if isinstance(raw_meta, str) and raw_meta.strip():
        try:
            parsed = json.loads(raw_meta)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            message = str(parsed.pop("_message", "") or "")
            terms = parsed
    elif isinstance(raw_meta, dict):
        meta_copy = dict(raw_meta)
        message = str(meta_copy.pop("_message", "") or "")
        terms = meta_copy

    return {
        "type": etype,
        "party": party,
        "round": round_num,
        "terms": terms,
        "message": message,
    }


def augment_signing_url(event: dict, bot_username: str) -> dict:
    """Append a bare Telegram deep-link callback to a signing event URL."""
    url = (event.get("approval_url") or "").strip()
    if not url or not bot_username:
        return event
    callback = urllib.parse.quote(f"https://t.me/{bot_username}")
    sep = "&" if "?" in url else "?"
    return {**event, "approval_url": f"{url}{sep}callback={callback}"}


def finalize_executed_pdf(
    output_dir: Path,
    pending_id: str,
    sshsign_host: str,
) -> Path | None:
    """Call upstream's run_finalize and return the generated executed PDF."""
    import dataclasses
    import importlib.util

    repo_str = os.environ.get("NEGOTIATE_REPO_PATH", "")
    if not repo_str:
        return None
    repo = Path(repo_str).resolve()

    mint = json.loads((output_dir / "mint.json").read_text())

    def _maybe_load_cfg(path_field: str) -> dict:
        path = mint.get(path_field, "")
        if not path:
            return {}
        try:
            return json.loads(Path(path).read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    f_cfg = _maybe_load_cfg("founder_config_path")
    i_cfg = _maybe_load_cfg("investor_config_path")
    neg_id = mint["negotiation_id"]
    config_anchor = mint.get("founder_config_path") or mint.get("investor_config_path") or ""
    neg_dir = Path(config_anchor).parent if config_anchor else output_dir
    neg_output = neg_dir / "output"

    spec = importlib.util.spec_from_file_location(
        "negotiate_upstream_fin", repo / "negotiate.py",
    )
    if spec is None or spec.loader is None:
        return None
    sys.path.insert(0, str(repo))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        return None

    user_role = mint.get("user_role", "founder")

    try:
        constraints = json.loads((output_dir / "config.json").read_text()).get("constraints") or {}
    except (OSError, json.JSONDecodeError):
        constraints = {}

    def _pick(cfg_keys, constraint_key, env_key, default=""):
        for cfg, key in cfg_keys:
            value = identity_value(
                cfg.get(key),
                field="company" if constraint_key == "company_name" else "",
            )
            if value:
                return value
        value = identity_value(
            constraints.get(constraint_key),
            field="company" if constraint_key == "company_name" else "",
            drop_placeholders=False,
        )
        if value:
            return value
        value = identity_value(
            os.environ.get(env_key),
            field="company" if constraint_key == "company_name" else "",
        )
        return value or default

    counterparty_pubkey = mint.get("counterparty_pubkey_path", "")
    founder_pubkey = f_cfg.get("pubkey") or (
        counterparty_pubkey if user_role == "investor" else ""
    )
    investor_pubkey = i_cfg.get("pubkey") or (
        counterparty_pubkey if user_role == "founder" else ""
    )

    kwargs = dict(
        negotiate_repo=repo,
        negotiation_id=neg_id,
        founder_token_path=mint.get("founder_token_path", ""),
        investor_token_path=mint.get("investor_token_path", ""),
        founder_pubkey_path=founder_pubkey,
        investor_pubkey_path=investor_pubkey,
        company_name=_pick(
            [(f_cfg, "company_name")], "company_name", "COMPANY_NAME", "Company",
        ),
        founder_name=_pick(
            [(f_cfg, "name"), (f_cfg, "founder_name")],
            "founder_name", "FOUNDER_NAME", "Founder",
        ),
        founder_title=_pick(
            [(f_cfg, "title")], "founder_title", "FOUNDER_TITLE", "",
        ),
        investor_name=_pick(
            [(i_cfg, "name"), (i_cfg, "investor_name")],
            "investor_name", "INVESTOR_NAME", "Investor",
        ),
        investor_firm=_pick(
            [(i_cfg, "firm")], "investor_firm", "INVESTOR_FIRM", "",
        ),
        investment_amount=(
            f_cfg.get("investment_amount")
            or i_cfg.get("investment_amount")
            or constraints.get("investment_amount")
            or 500_000.0
        ),
        sshsign_host=sshsign_host,
        output_dir=str(neg_output),
        signing_key_id=(
            f_cfg.get("founder_signing_key_id")
            or i_cfg.get("investor_signing_key_id")
            or f_cfg.get("signing_key_id")
            or i_cfg.get("signing_key_id", "")
        ),
        founder_signing_key_id=(
            f_cfg.get("founder_signing_key_id") or f_cfg.get("signing_key_id", "")
        ),
        investor_signing_key_id=(
            i_cfg.get("investor_signing_key_id") or i_cfg.get("signing_key_id", "")
        ),
        json_events=False,
        poll=False,
    )
    if "signer_role" in {f.name for f in dataclasses.fields(module.NegotiationConfig)}:
        kwargs["signer_role"] = user_role
    config = module.NegotiationConfig(**kwargs)

    ns = config.to_namespace()
    ns.finalize = pending_id

    original_cwd = os.getcwd()
    os.chdir(str(repo))
    try:
        module.run_finalize(ns)
    except Exception as e:
        sys.stderr.write(f"Finalize error: {e}\n")
        return None
    finally:
        os.chdir(original_cwd)

    executed = neg_output / f"{neg_id}_executed.pdf"
    return executed if executed.exists() else None
