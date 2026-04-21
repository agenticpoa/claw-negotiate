"""Telegram push helpers for the negotiate_safe skill.

These shell out to `openclaw message send`, which is the channel-agnostic CLI
primitive OpenClaw exposes for skills to send messages and file attachments
back to the user's chat.

Unused until PR 5.3 wires streaming into run_safe.py. This module is pure
(side effects only via subprocess.run) and exhaustively unit-tested.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path


DEFAULT_SESSIONS_PATH = Path("/root/.openclaw/agents/main/sessions/sessions.json")


@dataclass(frozen=True)
class SendResult:
    ok: bool
    message_id: str | None
    error: str | None


def resolve_chat_id(
    flag_value: str | None,
    sessions_path: Path = DEFAULT_SESSIONS_PATH,
) -> str | None:
    """Resolve a Telegram chat_id.

    Primary source: explicit --chat-id flag value passed by the caller.
    Fallback: the most recently updated `agent:main:telegram:direct:*` entry
    in OpenClaw's sessions.json (keyed by `updatedAt`).

    Returns None if neither source yields a value.
    """
    if flag_value:
        return str(flag_value).strip() or None

    try:
        data = json.loads(sessions_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None

    if not isinstance(data, dict):
        return None

    best: tuple[int, str] | None = None
    for key, entry in data.items():
        if not isinstance(entry, dict):
            continue
        if not key.startswith("agent:main:telegram:direct:"):
            continue
        chat_id = key.rsplit(":", 1)[-1]
        if not chat_id:
            continue
        updated = entry.get("updatedAt")
        if not isinstance(updated, (int, float)):
            continue
        if best is None or updated > best[0]:
            best = (int(updated), chat_id)

    return best[1] if best else None


def send_telegram(
    chat_id: str,
    message: str | None = None,
    media_path: str | Path | None = None,
    force_document: bool = False,
    openclaw_bin: str = "openclaw",
    runner=subprocess.run,
) -> SendResult:
    """Send a message and/or file attachment to a Telegram chat via openclaw CLI.

    At least one of `message` or `media_path` must be provided.
    `force_document` sends images/GIFs uncompressed (Telegram-only flag);
    PDFs are already treated as documents, so the flag is a no-op for them
    but harmless to set.

    `runner` is injectable for testing (default: subprocess.run).
    """
    if not message and not media_path:
        raise ValueError("send_telegram requires message and/or media_path")

    cmd = [
        openclaw_bin, "message", "send",
        "--channel", "telegram",
        "--target", str(chat_id),
        "--json",
    ]
    if message:
        cmd.extend(["--message", message])
    if media_path:
        cmd.extend(["--media", str(media_path)])
    if force_document:
        cmd.append("--force-document")

    try:
        result = runner(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return SendResult(ok=False, message_id=None, error="timeout")
    except FileNotFoundError:
        return SendResult(ok=False, message_id=None, error=f"{openclaw_bin} not found on PATH")

    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip() or f"exit {result.returncode}"
        return SendResult(ok=False, message_id=None, error=err)

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return SendResult(ok=False, message_id=None, error="non-JSON response from openclaw")

    inner = payload.get("payload") if isinstance(payload, dict) else None
    if isinstance(inner, dict) and inner.get("ok"):
        mid = inner.get("messageId")
        return SendResult(ok=True, message_id=str(mid) if mid is not None else None, error=None)

    return SendResult(ok=False, message_id=None, error=json.dumps(payload))
