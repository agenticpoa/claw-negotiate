#!/usr/bin/env python3
"""Single entry point for the negotiate_safe skill.

Two subcommands:
  prepare   — parse NL message into constraints, write config (fast, <10s)
  negotiate — mint tokens + run negotiation + emit results (long, 90-180s)

The output-dir is the shared state between the two calls. The model never
needs to pass files between scripts or construct shell pipes.

Usage:
  python3 run_safe.py prepare --message "Negotiate my SAFE..." --output-dir /tmp/safe_123
  python3 run_safe.py negotiate --output-dir /tmp/safe_123
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from parse_constraints import extract_constraints
from parse_identity import extract_identity
from format_event import format_event
from telegram_push import resolve_chat_id, send_telegram
from typing_loop import TypingLoop, get_bot_token
from sshsign_session import (
    SshsignSession,
    SshsignSessionError,
    SessionTerminalError,
    SessionExpiredError,
)


IDENTITY_SENTINEL_PATH = Path("/tmp/safe_negotiate/pending_negotiation.txt")


def _identity_configured() -> bool:
    """Return True if the installed user's identity is already set up.

    We treat FOUNDER_NAME as the anchor — on first install it's unset, and
    the user runs the in-chat setup wizard to populate it alongside the
    rest of the FOUNDER_*/INVESTOR_*/COMPANY_NAME env vars.
    """
    return bool((os.environ.get("FOUNDER_NAME") or "").strip())


SKILL_DIR = Path(__file__).resolve().parent
STREAM_HELPER = SKILL_DIR / "_stream_negotiate.py"


def run_prepare(
    message: str,
    output_dir: str,
    founder_name: str = "",
    founder_title: str = "CEO",
    chat_id_flag: str | None = None,
    sender=send_telegram,
) -> int:
    """Parse NL constraints, write config.json, and push the confirm card to chat.

    The confirm card uses format_event.py so the copy lives in one place and
    the model never generates UI itself.
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # Resolve chat_id first so we can push an interstitial before the slow parse.
    chat_id = resolve_chat_id(chat_id_flag)

    # First-run guard: if the user hasn't configured their identity yet,
    # stash their negotiation message and ask for identity before parsing.
    # They'll reply with "I'm Name, Title at Company" → run_setup picks up
    # the stashed message and continues the flow automatically.
    if not _identity_configured():
        try:
            IDENTITY_SENTINEL_PATH.parent.mkdir(parents=True, exist_ok=True)
            IDENTITY_SENTINEL_PATH.write_text(message)
        except OSError as e:
            sys.stderr.write(f"Could not stash pending message: {e}\n")
        if chat_id:
            sender(chat_id, message=(
                "\U0001f44b Welcome! Before we negotiate, tell me who you are.\n\n"  # 👋
                "Reply with a short self-intro, for example:\n"
                "• \"I'm Juan Figuera, CEO of APOA Inc\" (founder)\n"
                "• \"Mark Stone, partner at Blue Fund\" (investor)\n\n"
                "I'll remember it for next time."
            ))
        return 2

    if chat_id:
        sender(chat_id, message="\u23f3 Analyzing\u2026")  # ⏳

    # Keep the typing indicator alive during the Anthropic call and the rest
    # of prepare. Stopped in `finally` so the confirm card isn't competing
    # with a lingering "typing" status.
    typing = TypingLoop(chat_id=chat_id or "", bot_token=get_bot_token())
    typing.start()

    try:
        try:
            constraints = extract_constraints(message)
        except (ValueError, RuntimeError) as e:
            sys.stderr.write(f"Parse error: {e}\n")
            if chat_id:
                sender(chat_id, message=(
                    "\u26a0\ufe0f Couldn't parse your request. "  # ⚠️
                    "Please rephrase your terms and try again."
                ))
            return 1

        required_fields = ("valuation_cap_min", "valuation_cap_max", "discount_min", "pro_rata", "mfn")
        missing = [f for f in required_fields if constraints.get(f) is None]
        if missing:
            sys.stderr.write(f"Ambiguous constraints (null values): {missing}. Ask the user to clarify.\n")
            return 1

        config = {
            "constraints": constraints,
            # Legacy field; run_mint reads identity from constraints +
            # FOUNDER_*/INVESTOR_*/COMPANY_NAME env per upstream convention.
            "founder_name": founder_name or os.environ.get("FOUNDER_NAME") or "Founder",
            "founder_title": founder_title,
            "message": message,
        }

        if chat_id:
            body = format_event({"type": "confirm", "constraints": constraints})
            if body:
                sender(chat_id, message=body)
        else:
            sys.stderr.write(
                "Warning: no chat_id resolvable; skipping confirm push. "
                "The model will need to relay constraints manually.\n"
            )
        (out / "config.json").write_text(json.dumps(config, indent=2))

        sys.stdout.write(json.dumps(constraints, indent=2) + "\n")
        return 0
    finally:
        typing.stop()


def run_mint(output_dir: str, config: dict) -> int:
    """Mint APOA tokens using the constraints from config.json."""
    repo = os.environ.get("NEGOTIATE_REPO_PATH", "")
    if not repo:
        sys.stderr.write("NEGOTIATE_REPO_PATH not set.\n")
        return 2

    repo = Path(repo).resolve()
    if not (repo / "create_tokens.py").exists():
        sys.stderr.write(f"create_tokens.py not found under {repo}\n")
        return 2

    constraints = config["constraints"]
    out = Path(output_dir)

    negotiation_id = f"neg_{uuid.uuid4().hex[:12]}"
    neg_dir = repo / "negotiations" / negotiation_id
    neg_dir.mkdir(parents=True, exist_ok=True)

    expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=int(os.environ.get("NEGOTIATION_TTL", "3600"))
    )
    expires_str = expires_at.strftime("%Y-%m-%dT%H:%M:%SZ")

    pro_rata_required = constraints.get("pro_rata") == "required"
    mfn_required = constraints.get("mfn") == "required"
    discount_min = float(constraints.get("discount_min", 0.20))
    discount_max = discount_min + 0.10

    # `get(k, default)` returns default only when the key is MISSING; the
    # parser emits null for unknown fields (key-present, value-None), which
    # would leak through and break subprocess.run. Use `or default` to catch both.
    amount = constraints.get("investment_amount") or 500_000.0

    # Dual-role: user's constraints bind whichever side the user is playing.
    # The OTHER side's constraints come from env-var defaults so the AI
    # opposing agent has sensible negotiating bounds.
    user_role = (constraints.get("role") or "founder").lower()
    if user_role not in ("founder", "investor"):
        user_role = "founder"
    user_flag_prefix = "--founder-" if user_role == "founder" else "--investor-"
    ai_flag_prefix = "--investor-" if user_role == "founder" else "--founder-"
    ai_env_prefix = "INVESTOR_" if user_role == "founder" else "FOUNDER_"

    # Identity resolution — aligned with upstream agenticpoa/negotiate
    # `.env.example`. Precedence per field: NL > env > literal fallback.
    # Upstream convention: FOUNDER_* describes the founder side of the deal
    # (regardless of who the user is), INVESTOR_* describes the investor
    # side. In demo mode the user's own identity ends up in whichever set
    # matches their role.
    founder_name = (
        constraints.get("founder_name")
        or os.environ.get("FOUNDER_NAME")
        or "Founder"
    )
    founder_title = (
        constraints.get("founder_title")
        or os.environ.get("FOUNDER_TITLE")
        or "CEO"
    )
    investor_name = (
        constraints.get("investor_name")
        or os.environ.get("INVESTOR_NAME")
        or "Investor"
    )
    investor_firm = (
        constraints.get("investor_firm")
        or os.environ.get("INVESTOR_FIRM")
        or "Investor Firm"
    )
    company = (
        constraints.get("company_name")
        or os.environ.get("COMPANY_NAME")
        or "Company"
    )

    slug = "".join(c.lower() if c.isalnum() else "-" for c in company).strip("-")
    service = f"safe:{slug}:{negotiation_id}"

    cmd = [
        sys.executable, str(repo / "create_tokens.py"),
        "--negotiation-id", negotiation_id,
        "--principal-id", os.environ.get("USER_DID") or "did:apoa:default",
        "--expires", expires_str,
        "--service", service,
        "--company-name", company,
        "--founder-name", founder_name,
        "--founder-title", founder_title,
        "--investor-name", investor_name,
        "--investor-firm", investor_firm,
        "--investment-amount", str(amount),
        f"{user_flag_prefix}cap-min", str(constraints["valuation_cap_min"]),
        f"{user_flag_prefix}cap-max", str(constraints["valuation_cap_max"]),
        f"{user_flag_prefix}discount-min", str(constraints["discount_min"]),
        f"{user_flag_prefix}discount-max", str(discount_max),
        f"{user_flag_prefix}pro-rata-required", "true" if pro_rata_required else "false",
        f"{user_flag_prefix}mfn-required", "true" if mfn_required else "false",
        "--keys-dir", str(neg_dir / "keys"),
        "--tokens-dir", str(neg_dir / "tokens"),
        "--config-dir", str(neg_dir),
        "--create-keys",
    ]

    ai_flag_suffixes = {
        "CAP_MIN": "cap-min",
        "CAP_MAX": "cap-max",
        "DISCOUNT_MIN": "discount-min",
        "DISCOUNT_MAX": "discount-max",
        "PRO_RATA_REQUIRED": "pro-rata-required",
        "MFN_REQUIRED": "mfn-required",
    }
    for env_key_suffix, flag_suffix in ai_flag_suffixes.items():
        val = os.environ.get(f"{ai_env_prefix}{env_key_suffix}")
        if val:
            cmd.extend([f"{ai_flag_prefix}{flag_suffix}", val])

    result = subprocess.run(cmd, cwd=repo, capture_output=True, text=True)
    if result.returncode != 0:
        sys.stderr.write(f"Mint failed:\n{result.stdout}\n{result.stderr}\n")
        return result.returncode

    mint_output = {
        "negotiation_id": negotiation_id,
        "founder_config_path": str(neg_dir / "founder.json"),
        "investor_config_path": str(neg_dir / "investor.json"),
        "founder_token_path": str(neg_dir / "tokens" / "founder.jwt"),
        "investor_token_path": str(neg_dir / "tokens" / "investor.jwt"),
        "expires_at": expires_str,
        "service": service,
        "user_role": user_role,
        "mode": (constraints.get("mode") or "demo").lower(),
    }

    # Two-party mode: register the signing session with sshsign and record
    # the resulting session_code in mint.json so downstream code (waiting
    # loop, invitation card, cancellation flow) can reference it.
    if mint_output["mode"] == "two_party":
        session_registered = _register_signing_session(
            mint_output, constraints, user_role, neg_dir,
        )
        if session_registered is None:
            return 3  # registration failure; caller surfaces to user
        mint_output.update(session_registered)

    (out / "mint.json").write_text(json.dumps(mint_output, indent=2))

    sys.stdout.write(json.dumps({
        "type": "authorized",
        "negotiation_id": negotiation_id,
        "service": service,
        "expires_at": expires_str,
        "mode": mint_output["mode"],
        "session_code": mint_output.get("session_code"),
    }) + "\n")
    sys.stdout.flush()

    return 0


def _register_signing_session(
    mint_output: dict,
    constraints: dict,
    user_role: str,
    neg_dir: Path,
    session_client=None,
) -> dict | None:
    """Call sshsign.dev create-session for a two-party negotiation.

    Reads the user's APOA pubkey PEM from the minted keys on disk and
    publishes it + a minimal metadata envelope so the counterparty can
    retrieve it on join. Returns a dict of session fields to merge into
    mint.json, or None on error.
    """
    role_to_pubkey_path = {
        "founder": neg_dir / "keys" / "founder_public.pem",
        "investor": neg_dir / "keys" / "investor_public.pem",
    }
    pubkey_path = role_to_pubkey_path.get(user_role)
    if pubkey_path is None or not pubkey_path.exists():
        sys.stderr.write(
            f"APOA pubkey for role={user_role} not found at {pubkey_path}\n"
        )
        return None

    try:
        apoa_pubkey_pem = pubkey_path.read_text()
    except OSError as e:
        sys.stderr.write(f"reading {pubkey_path}: {e}\n")
        return None

    metadata_public = {"use_case": "safe", "version": 1}
    metadata_member = {
        k: v for k, v in {
            "company_name": constraints.get("company_name"),
            "founder_name": constraints.get("founder_name"),
            "founder_title": constraints.get("founder_title"),
            "investor_name": constraints.get("investor_name"),
            "investor_firm": constraints.get("investor_firm"),
        }.items() if v
    }

    client = session_client or SshsignSession(
        host=os.environ.get("SSHSIGN_HOST", "sshsign.dev"),
    )
    try:
        sess = client.create_session(
            session_id=mint_output["negotiation_id"],
            role=user_role,
            apoa_pubkey_pem=apoa_pubkey_pem,
            party_did=os.environ.get("USER_DID") or None,
            metadata_public=metadata_public,
            metadata_member=metadata_member,
        )
    except SshsignSessionError as e:
        sys.stderr.write(f"create-session failed: {e}\n")
        return None

    return {
        "session_code": sess.get("session_code"),
        "session_created_at": sess.get("created_at"),
        "session_expires_at": sess.get("expires_at"),
        "session_status": sess.get("status"),
    }


def _wait_for_counterparty(
    session_id: str,
    session_code: str,
    chat_id: str,
    counterparty_label: str,
    sender=send_telegram,
    session_client=None,
    poll_interval: int = 10,
    max_wait_seconds: int = 24 * 60 * 60,  # matches session TTL; sshsign expires first
    typing_factory=None,
    sleep_fn=None,
    now_fn=None,
) -> str:
    """Poll sshsign for the counterparty's join. Returns the final status:
      'joined' — success; caller proceeds to run_distributed
      'expired' — invitation TTL hit; session unusable
      'canceled' — founder canceled; exit cleanly
      'error' — persistent ssh errors; caller surfaces to user

    Periodic waiting cards are pushed at elapsed milestones (5, 15, 60, 180,
    600 minutes) so the user knows we're still alive without being spammed.
    Typing loop runs continuously to keep the UI feeling active.
    """
    if sleep_fn is None:
        import time as _time
        sleep_fn = _time.sleep
    if now_fn is None:
        import time as _time
        now_fn = _time.time

    client = session_client or SshsignSession(
        host=os.environ.get("SSHSIGN_HOST", "sshsign.dev"),
    )

    if typing_factory is None:
        typing = TypingLoop(chat_id=chat_id, bot_token=get_bot_token())
    else:
        typing = typing_factory(chat_id)
    typing.start()

    start = now_fn()
    milestones_minutes = [5, 15, 60, 180, 600]
    fired_milestones: set[int] = set()

    try:
        while True:
            elapsed = now_fn() - start
            if elapsed > max_wait_seconds:
                return "expired"

            try:
                sess = client.get_session(session_id=session_id)
            except SshsignSessionError as e:
                # Transient ssh errors happen (network blips); retry.
                sys.stderr.write(f"get-session transient error: {e}\n")
                sleep_fn(poll_interval)
                continue

            status = (sess.get("status") or "").lower()
            if status == "joined":
                body = format_event({
                    "type": "counterparty_joined",
                    "counterparty_label": counterparty_label,
                })
                if body:
                    sender(chat_id, message=body)
                return "joined"
            if status in ("canceled", "rescinded_after_sign"):
                return "canceled"
            if status == "expired":
                return "expired"

            # Still open — push a waiting card at milestones.
            elapsed_min = int(elapsed / 60)
            for milestone in milestones_minutes:
                if elapsed_min >= milestone and milestone not in fired_milestones:
                    fired_milestones.add(milestone)
                    remaining_hours = (max_wait_seconds - elapsed) / 3600
                    body = format_event({
                        "type": "waiting",
                        "elapsed_minutes": elapsed_min,
                        "remaining_hours": remaining_hours,
                    })
                    if body:
                        sender(chat_id, message=body)
                    break

            sleep_fn(poll_interval)
    finally:
        typing.stop()


def _augment_signing_url(event: dict, bot_username: str) -> dict:
    """Append a bare Telegram deep-link callback to the signing event's approval_url.

    After signing, the browser redirects to `https://t.me/<bot>` which opens the
    Telegram chat without sending any message (for returning users — which the
    user always is at this point, since they just invoked the skill). The script
    is already polling get-envelope and will push the signed confirmation and PDF
    autonomously; this callback just brings the user back to the chat window.
    """
    url = (event.get("approval_url") or "").strip()
    if not url or not bot_username:
        return event
    callback = urllib.parse.quote(f"https://t.me/{bot_username}")
    sep = "&" if "?" in url else "?"
    return {**event, "approval_url": f"{url}{sep}callback={callback}"}


def _stream_to_telegram(
    output_dir: Path,
    chat_id: str,
    constraints: dict | None,
    bot_username: str,
    stream_helper: Path = STREAM_HELPER,
    sender=send_telegram,
    popen=subprocess.Popen,
    typing_factory=None,
) -> tuple[int, dict | None]:
    """Spawn the streaming helper; push each event to Telegram as it fires.

    Returns 0 on clean exit, the subprocess returncode otherwise.
    `sender` and `popen` are injectable for testing.
    """
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    cmd = [sys.executable, "-u", str(stream_helper), "--output-dir", str(output_dir)]

    # Typing loop covers the gap between each round (upstream agents take
    # ~6-8s per turn, during which nothing is posted to the chat).
    if typing_factory is None:
        typing = TypingLoop(chat_id=chat_id, bot_token=get_bot_token())
    else:
        typing = typing_factory(chat_id)
    typing.start()

    try:
        proc = popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )

        events: list[dict] = []
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            events.append(event)

            if event.get("type") == "signing":
                event = _augment_signing_url(event, bot_username)

            message = format_event(event, constraints=constraints)
            if message:
                sender(chat_id, message=message)

        proc.wait()
    finally:
        typing.stop()

    # Archive events to disk for debuggability (replaces results.md)
    try:
        (output_dir / "events.ndjson").write_text(
            "\n".join(json.dumps(e) for e in events) + "\n"
        )
    except OSError:
        pass

    signing_event = next((e for e in events if e.get("type") == "signing"), None)
    return proc.returncode or 0, signing_event


# ─── Post-stream: envelope polling + finalize + PDF push ────────────────────


def _ssh_envelope_status(
    pending_id: str,
    sshsign_host: str = "sshsign.dev",
    runner=subprocess.run,
) -> str | None:
    """Return the envelope status ('approved' / 'pending' / other) or None on error."""
    try:
        result = runner(
            ["ssh", sshsign_host, "get-envelope", "--id", pending_id],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        return str(json.loads(result.stdout).get("status", "")) or None
    except json.JSONDecodeError:
        return None


def _poll_envelope_approval(
    pending_id: str,
    sshsign_host: str = "sshsign.dev",
    timeout: int = 900,
    interval: int = 5,
    status_fn=_ssh_envelope_status,
    sleep_fn=None,
) -> bool:
    """Poll until envelope is approved or timeout elapses. True = approved."""
    if sleep_fn is None:
        import time as _time
        sleep_fn = _time.sleep

    elapsed = 0
    while elapsed < timeout:
        status = status_fn(pending_id, sshsign_host)
        if status == "approved":
            return True
        sleep_fn(interval)
        elapsed += interval
    return False


def _finalize_executed_pdf(
    output_dir: Path,
    pending_id: str,
    sshsign_host: str,
) -> Path | None:
    """Call upstream's run_finalize via importlib; return executed PDF path if generated.

    Bypasses upstream main()/auto_setup() by invoking run_finalize directly with
    a fully-constructed Namespace built from our mint.json + config files.
    """
    import importlib.util

    repo_str = os.environ.get("NEGOTIATE_REPO_PATH", "")
    if not repo_str:
        return None
    repo = Path(repo_str).resolve()

    mint = json.loads((output_dir / "mint.json").read_text())
    f_cfg = json.loads(Path(mint["founder_config_path"]).read_text())
    i_cfg = json.loads(Path(mint["investor_config_path"]).read_text())
    neg_id = mint["negotiation_id"]
    neg_dir = Path(mint["founder_config_path"]).parent
    neg_output = neg_dir / "output"

    spec = importlib.util.spec_from_file_location("negotiate_upstream_fin", repo / "negotiate.py")
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
    kwargs = dict(
        negotiate_repo=repo,
        negotiation_id=neg_id,
        founder_token_path=mint["founder_token_path"],
        investor_token_path=mint["investor_token_path"],
        founder_pubkey_path=f_cfg["pubkey"],
        investor_pubkey_path=i_cfg["pubkey"],
        company_name=f_cfg["company_name"],
        founder_name=f_cfg["name"],
        founder_title=f_cfg.get("title", ""),
        investor_name=i_cfg["name"],
        investor_firm=i_cfg.get("firm", ""),
        investment_amount=f_cfg["investment_amount"],
        sshsign_host=sshsign_host,
        output_dir=str(neg_output),
        signing_key_id=f_cfg.get("founder_signing_key_id") or f_cfg.get("signing_key_id", ""),
        founder_signing_key_id=f_cfg.get("founder_signing_key_id") or f_cfg.get("signing_key_id", ""),
        investor_signing_key_id=i_cfg.get("investor_signing_key_id") or i_cfg.get("signing_key_id", ""),
        json_events=False,
        poll=False,
    )
    import dataclasses
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


def _is_still_active_session(output_dir: Path) -> bool:
    """Check if this process is still the active negotiation for this output dir.

    When a new `run_safe.py negotiate` starts, it overwrites `.session.pid`
    with its own PID. A previous process whose long poll later times out
    should silently exit rather than push a stale timeout message into the
    chat (that was bug #4 in the PR-A feedback — the 15-min timeout from a
    prior aborted negotiation posted into a new session).
    """
    try:
        pid_file = output_dir / ".session.pid"
        return int(pid_file.read_text().strip()) == os.getpid()
    except (OSError, ValueError):
        # If the file is missing or unreadable, assume we're still active —
        # better to occasionally send a timeout message than suppress a
        # legitimate one.
        return True


def _await_sign_and_push(
    output_dir: Path,
    chat_id: str,
    sshsign_host: str,
    pending_id: str,
    timeout: int = 900,
    poll_interval: int = 5,
    sender=send_telegram,
    poll_fn=_poll_envelope_approval,
    finalize_fn=_finalize_executed_pdf,
    is_active_fn=_is_still_active_session,
    typing_factory=None,
) -> int:
    """Wait for signature, finalize PDF, push confirmation + attachment.

    Returns 0 if the user received the executed PDF, 1 on timeout, 2 on
    finalize failure, 3 if this process was superseded by a newer session.
    """
    # Typing covers the gap between "Almost done — sign here" and either
    # "Signed ✓" (user signed quickly) or the eventual PDF generation.
    if typing_factory is None:
        typing = TypingLoop(chat_id=chat_id, bot_token=get_bot_token())
    else:
        typing = typing_factory(chat_id)
    typing.start()

    try:
        approved = poll_fn(
            pending_id=pending_id,
            sshsign_host=sshsign_host,
            timeout=timeout,
            interval=poll_interval,
        )
        if not approved:
            if not is_active_fn(output_dir):
                return 3
            sender(chat_id, message=(
                "Signature not received after waiting.\n"
                'When you sign, reply "signed" and I\'ll verify manually.'
            ))
            return 1

        if not is_active_fn(output_dir):
            return 3

        # Confirm signature receipt immediately, then let the user know the
        # PDF is coming. Finalize (upstream pdf generation) takes a few
        # seconds; posting "Generating executed file\u2026" fills that gap.
        sender(chat_id, message="\u2705 Confirmed signature.")  # ✅
        sender(chat_id, message="\U0001f4c4 Generating executed file\u2026")  # 📄

        pdf_path = finalize_fn(output_dir, pending_id, sshsign_host)
        if not pdf_path:
            if not is_active_fn(output_dir):
                return 3
            sender(chat_id, message=(
                "Signature received but I couldn't generate the executed PDF. "
                "Check the negotiation output directory on the server."
            ))
            return 2

        if not is_active_fn(output_dir):
            return 3

        sender(chat_id, media_path=str(pdf_path))
        return 0
    finally:
        typing.stop()


def run_negotiate(output_dir: str, chat_id_flag: str | None = None) -> int:
    """Full negotiate flow: mint tokens then stream the negotiation to chat."""
    out = Path(output_dir)
    config_path = out / "config.json"
    if not config_path.exists():
        sys.stderr.write(f"No config.json in {output_dir}. Run 'prepare' first.\n")
        return 2

    config = json.loads(config_path.read_text())

    # Claim this output dir for this process. Any prior process still polling
    # an envelope on behalf of a previous negotiation will see a PID mismatch
    # on its next `_is_still_active_session` check and exit silently.
    try:
        (out / ".session.pid").write_text(str(os.getpid()))
    except OSError:
        pass

    rc = run_mint(output_dir, config)
    if rc != 0:
        return rc

    chat_id = resolve_chat_id(chat_id_flag)
    if not chat_id:
        sys.stderr.write(
            "No chat_id: pass --chat-id or ensure /root/.openclaw/agents/main/sessions/sessions.json has a telegram:direct entry.\n"
        )
        return 2

    # Two-party mode: push the invitation card and wait for the
    # counterparty to join before firing the stream. In demo mode both
    # paths are skipped and we go straight to streaming.
    try:
        mint = json.loads((out / "mint.json").read_text())
    except (OSError, json.JSONDecodeError):
        mint = {}
    if mint.get("mode") == "two_party":
        wait_rc = _founder_two_party_gate(
            out=out,
            chat_id=chat_id,
            mint=mint,
            constraints=config.get("constraints") or {},
        )
        if wait_rc != 0:
            return wait_rc

    bot_username = os.environ.get("TELEGRAM_BOT_USERNAME", "AgenticPOA_bot")
    stream_rc, signing_event = _stream_to_telegram(
        output_dir=out,
        chat_id=chat_id,
        constraints=config.get("constraints"),
        bot_username=bot_username,
    )
    if stream_rc != 0 or not signing_event:
        return stream_rc

    pending_id = signing_event.get("pending_id") or ""
    if not pending_id:
        return 0

    sshsign_host = os.environ.get("SSHSIGN_HOST", "sshsign.dev")
    return _await_sign_and_push(
        output_dir=out,
        chat_id=chat_id,
        sshsign_host=sshsign_host,
        pending_id=pending_id,
    )


def _founder_two_party_gate(
    out: Path,
    chat_id: str,
    mint: dict,
    constraints: dict,
    sender=send_telegram,
    wait_fn=_wait_for_counterparty,
) -> int:
    """Push the invitation card, wait for the counterparty to join, and
    return 0 to proceed with streaming or a non-zero rc if the session
    ended before negotiation could begin.

    rc=0    → counterparty joined; caller should run the stream
    rc=1    → invitation expired; user notified
    rc=2    → session was canceled; user notified (or stays silent if we
              canceled ourselves; the parser owns that path in J5)
    rc=3    → missing session_code or persistent sshsign error
    """
    session_code = mint.get("session_code")
    session_id = mint.get("negotiation_id")
    if not session_code or not session_id:
        sender(chat_id, message=(
            "\u26a0\ufe0f Internal error: two-party session was not registered. "  # ⚠️
            "Try again."
        ))
        return 3

    # Counterparty label for the card — prefer the user's own NL phrasing
    # (investor_name + firm) over a generic word.
    label_parts = [
        p for p in (
            constraints.get("investor_name"),
            constraints.get("investor_firm"),
        ) if p
    ]
    counterparty_label = ", ".join(label_parts) if label_parts else "your counterparty"

    invitation_body = format_event({
        "type": "invitation",
        "session_code": session_code,
        "expires_at": mint.get("session_expires_at") or "",
        "ttl_hours": 24,
        "counterparty_label": counterparty_label,
    })
    if invitation_body:
        sender(chat_id, message=invitation_body)

    status = wait_fn(
        session_id=session_id,
        session_code=session_code,
        chat_id=chat_id,
        counterparty_label=counterparty_label,
    )
    if status == "joined":
        return 0
    if status == "expired":
        body = format_event({"type": "invitation_expired"})
        if body:
            sender(chat_id, message=body)
        return 1
    if status == "canceled":
        # Cancel message content depends on who canceled — J5 owns the
        # user-facing copy. Keep this path silent for now; the stream
        # loop wouldn't run anyway.
        return 2
    # status == "error" or unknown
    sender(chat_id, message=(
        "\u26a0\ufe0f Lost connection to the signing service while waiting. "
        "Your session still exists; try the command again."
    ))
    return 3


_IDENTITY_TO_ENV: list[tuple[str, str]] = [
    # (identity-dict key, env var name we set via openclaw config set)
    # `role` is intentionally omitted — it's derived per-negotiation from the
    # NL, not pinned to the user's profile (they can play either side in demo
    # mode).
    ("name_founder", "FOUNDER_NAME"),
    ("title_founder", "FOUNDER_TITLE"),
    ("company", "COMPANY_NAME"),
    ("name_investor", "INVESTOR_NAME"),
    ("firm", "INVESTOR_FIRM"),
]


def _build_env_updates(identity: dict) -> dict[str, str]:
    """Map a parsed identity to the full set of env vars we need to persist.

    When the user self-identifies as founder, we seed FOUNDER_NAME with their
    name and leave INVESTOR_NAME at its current value (for the demo's AI
    counterparty). When they self-identify as investor, we seed INVESTOR_NAME
    and INVESTOR_FIRM. Either way COMPANY_NAME / title fields are set if the
    user mentioned them.
    """
    updates: dict[str, str] = {}
    name = (identity.get("name") or "").strip()
    title = (identity.get("title") or "").strip()
    company = (identity.get("company") or "").strip()
    firm = (identity.get("firm") or "").strip()
    role = identity.get("role", "founder")

    if role == "founder":
        if name:
            updates["FOUNDER_NAME"] = name
        if title:
            updates["FOUNDER_TITLE"] = title
        if company:
            updates["COMPANY_NAME"] = company
    else:
        if name:
            updates["INVESTOR_NAME"] = name
        if firm:
            updates["INVESTOR_FIRM"] = firm
        # Investors often self-identify with title too ("Partner", "MD")
        if title:
            updates["INVESTOR_FIRM"] = updates.get("INVESTOR_FIRM", firm)  # firm stays
    return updates


def _persist_env_updates(updates: dict[str, str], runner=subprocess.run) -> list[str]:
    """Apply the identity updates via `openclaw config set` — one call per
    env var so OpenClaw's config validator sees each change individually.

    Returns the list of env vars that failed to persist (empty on success).
    """
    failures: list[str] = []
    for key, value in updates.items():
        path = f"skills.entries.negotiate_safe.env.{key}"
        try:
            result = runner(
                ["openclaw", "config", "set", path, value],
                capture_output=True, text=True, timeout=10,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            failures.append(key)
            continue
        if result.returncode != 0:
            failures.append(key)
    return failures


def run_setup(
    message: str,
    chat_id_flag: str | None = None,
    sender=send_telegram,
    persister=_persist_env_updates,
) -> int:
    """Parse the user's self-intro, persist identity env vars, and — if a
    stashed negotiation message exists from a prior first-run attempt —
    hand off to prepare automatically so the user doesn't have to retype.
    """
    chat_id = resolve_chat_id(chat_id_flag)
    typing = TypingLoop(chat_id=chat_id or "", bot_token=get_bot_token())
    typing.start()

    try:
        try:
            identity = extract_identity(message)
        except (ValueError, RuntimeError) as e:
            sys.stderr.write(f"Identity parse error: {e}\n")
            if chat_id:
                sender(chat_id, message=(
                    "\u26a0\ufe0f I couldn't read that. Try: "  # ⚠️
                    "\"I'm Name, Title at Company\" or \"Name, Firm\"."
                ))
            return 1

        if not identity.get("name"):
            if chat_id:
                sender(chat_id, message=(
                    "\u26a0\ufe0f I need at least your name. "
                    "Try: \"I'm Juan Figuera, CEO of APOA Inc\"."
                ))
            return 1

        updates = _build_env_updates(identity)
        failures = persister(updates)
        if failures:
            sys.stderr.write(f"Failed to persist: {failures}\n")
            if chat_id:
                sender(chat_id, message=(
                    "\u26a0\ufe0f Saved partial profile. "
                    f"Couldn't write: {', '.join(failures)}. "
                    "The admin can fix via `openclaw config set`."
                ))

        if chat_id:
            who = identity.get("name", "you")
            where = identity.get("company") or identity.get("firm") or ""
            hello = f"\u2705 Got it, {who}"  # ✅
            if where:
                hello += f" at {where}"
            hello += ". I'll remember for next time."
            sender(chat_id, message=hello)
    finally:
        typing.stop()

    # If the user tried to negotiate BEFORE setup, we stashed their request —
    # pick it up now and hand off to prepare so they don't have to retype.
    if IDENTITY_SENTINEL_PATH.exists():
        try:
            pending = IDENTITY_SENTINEL_PATH.read_text().strip()
            IDENTITY_SENTINEL_PATH.unlink()
        except OSError:
            pending = ""
        if pending:
            # Env updates are hot-reloaded, but this Python process's
            # os.environ is a snapshot from spawn time. Apply the updates
            # in-process so the downstream prepare + mint see them.
            for key, value in updates.items():
                os.environ[key] = value
            if chat_id:
                sender(chat_id, message="Picking up your negotiation request\u2026")
            return run_prepare(
                message=pending,
                output_dir="/tmp/safe_negotiate",
                chat_id_flag=chat_id_flag,
                sender=sender,
            )

    return 0


def run_profile(chat_id_flag: str | None = None, sender=send_telegram) -> int:
    """Show the user's saved identity (read from env vars) as a chat card."""
    chat_id = resolve_chat_id(chat_id_flag)
    profile = {
        "founder_name": os.environ.get("FOUNDER_NAME", ""),
        "founder_title": os.environ.get("FOUNDER_TITLE", ""),
        "company_name": os.environ.get("COMPANY_NAME", ""),
        "investor_name": os.environ.get("INVESTOR_NAME", ""),
        "investor_firm": os.environ.get("INVESTOR_FIRM", ""),
    }
    body = format_event({"type": "profile", "profile": profile})
    if not body:
        return 2
    if chat_id:
        sender(chat_id, message=body)
    else:
        sys.stdout.write(body + "\n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="SAFE negotiation skill")
    sub = parser.add_subparsers(dest="command")

    prep = sub.add_parser("prepare", help="Parse NL message into constraints")
    prep.add_argument("--message", default="", help="The negotiation request text")
    prep.add_argument("--message-file", default="", help="Path to file containing the request (avoids shell $ expansion)")
    prep.add_argument("--output-dir", required=True)
    prep.add_argument("--founder-name", default=os.environ.get("FOUNDER_NAME", ""))
    prep.add_argument("--founder-title", default="CEO")
    prep.add_argument(
        "--chat-id",
        default="",
        help="Telegram chat id to push the confirm card to (falls back to openclaw sessions.json)",
    )

    neg = sub.add_parser("negotiate", help="Mint tokens and run negotiation")
    neg.add_argument("--output-dir", required=True)
    neg.add_argument(
        "--chat-id",
        default="",
        help="Telegram chat id to push events to (falls back to openclaw sessions.json)",
    )

    setup = sub.add_parser(
        "setup",
        help="Parse a self-introduction and persist identity env vars "
             "(also used for profile updates — any fields present in the "
             "message overwrite the previous values).",
    )
    setup.add_argument("--message", default="", help="Self-intro text")
    setup.add_argument("--message-file", default="")
    setup.add_argument("--chat-id", default="")

    profile = sub.add_parser(
        "profile",
        help="Show the user's currently saved identity",
    )
    profile.add_argument("--chat-id", default="")

    args = parser.parse_args()

    if args.command == "prepare":
        message = args.message
        if not message and args.message_file:
            message = Path(args.message_file).read_text().strip()
        if not message:
            sys.stderr.write("Provide --message or --message-file.\n")
            return 2
        return run_prepare(
            message,
            args.output_dir,
            args.founder_name,
            args.founder_title,
            chat_id_flag=args.chat_id or None,
        )
    elif args.command == "negotiate":
        return run_negotiate(args.output_dir, chat_id_flag=args.chat_id or None)
    elif args.command == "setup":
        message = args.message
        if not message and args.message_file:
            message = Path(args.message_file).read_text().strip()
        if not message:
            sys.stderr.write("Provide --message or --message-file for setup.\n")
            return 2
        return run_setup(message, chat_id_flag=args.chat_id or None)
    elif args.command == "profile":
        return run_profile(chat_id_flag=args.chat_id or None)
    else:
        parser.print_help()
        return 2


if __name__ == "__main__":
    sys.exit(main())
