"""Idempotent Telegram projector for shared-orchestrator events."""
from __future__ import annotations

from typing import Callable

import delivery_store
from format_event import format_event
from telegram import route_stream_message, should_publish_stream_event
from telegram_push import send_signing_url_to_dm, send_telegram
from upstream import synthesize_offer_event


def delivery_key(event: dict) -> str:
    etype = event.get("type") or "event"
    if etype in ("offer", "counter", "accept"):
        return "offer:{party}:{round}:{etype}".format(
            party=event.get("party") or "",
            round=event.get("round"),
            etype=etype,
        )
    if etype == "signing":
        return f"signing:{event.get('pending_id') or ''}"
    return f"{etype}:{event.get('id') or event.get('round') or ''}"


def project_event(
    *,
    session_id: str,
    event: dict,
    constraints: dict | None,
    dm_chat_id: str,
    group_chat_id: str | None,
    sender: Callable = send_telegram,
    dm_sender: Callable = send_signing_url_to_dm,
) -> bool:
    key = delivery_key(event)
    if delivery_store.has_delivery(session_id, key):
        return False
    if not should_publish_stream_event(event, constraints, group_chat_id):
        return False

    message = format_event(event, constraints=constraints)
    if not message:
        return False
    route_stream_message(
        event=event,
        message=message,
        chat_id=str(dm_chat_id),
        group_chat_id=str(group_chat_id) if group_chat_id else None,
        constraints=constraints,
        sender=sender,
        dm_sender=dm_sender,
    )
    if event.get("type") == "signing" and group_chat_id:
        target = f"dm:{dm_chat_id};group:{group_chat_id}"
    else:
        target = str(group_chat_id or dm_chat_id)
    delivery_store.mark_delivery(session_id, key, target)
    return True


def project_history(
    *,
    session_id: str,
    history_rows: list[dict],
    constraints: dict | None,
    dm_chat_id: str,
    group_chat_id: str | None,
    sender: Callable = send_telegram,
    dm_sender: Callable = send_signing_url_to_dm,
) -> int:
    count = 0
    for row in history_rows:
        event = synthesize_offer_event(row)
        if not event:
            continue
        if project_event(
            session_id=session_id,
            event=event,
            constraints=constraints,
            dm_chat_id=dm_chat_id,
            group_chat_id=group_chat_id,
            sender=sender,
            dm_sender=dm_sender,
        ):
            count += 1
    return count
