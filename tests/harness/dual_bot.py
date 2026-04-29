"""P7-5 Day 3.5 dual-bot harness.

Not a full subprocess simulator — that's what the Day 4 live dry-run
is for. This harness provides in-memory fakes for sshsign and a
shared "group chat" buffer, plus a fake session client that both
founder-side and investor-side calls operate against. The goal is
to verify cross-side state propagation: founder writes
``founder_streaming_at``, investor's poll sees it; investor joins,
founder's scan picks it up on the next tick; etc.

Scope: state-flow correctness. Stream/finalize paths are mocked
since upstream's ``run_distributed`` requires a real Anthropic key
and is better exercised on live droplets.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeMember:
    role: str
    user_id: str
    founder_resumed_at: int | None = None
    founder_streaming_at: int | None = None

    def to_dict(self) -> dict:
        return {
            "role": self.role,
            "user_id": self.user_id,
            "founder_resumed_at": self.founder_resumed_at,
            "founder_streaming_at": self.founder_streaming_at,
        }


@dataclass
class FakeSession:
    session_id: str
    session_code: str
    status: str = "created"
    members: list[FakeMember] = field(default_factory=list)
    group_chat_id: int = 0
    created_by: str = "u_founder"
    executed_artifact: str = ""
    leases: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "session_code": self.session_code,
            "status": self.status,
            "members": [m.to_dict() for m in self.members],
            "group_chat_id": self.group_chat_id,
            "executed_artifact": self.executed_artifact,
        }


class FakeSshsign:
    """In-memory sshsign. Shared by both founder-side and investor-
    side invocations in tests, so cross-bot state propagation is
    observable without real network I/O.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, FakeSession] = {}

    # ---- fixture helpers --------------------------------------------------

    def seed_session(
        self, session_id: str, session_code: str,
        created_by: str = "u_founder",
    ) -> FakeSession:
        sess = FakeSession(
            session_id=session_id,
            session_code=session_code,
            status="created",
            created_by=created_by,
            members=[FakeMember(role="founder", user_id=created_by)],
        )
        self._sessions[session_id] = sess
        return sess

    def simulate_investor_joined(self, session_id: str, user_id: str = "u_investor") -> None:
        sess = self._sessions[session_id]
        sess.members.append(FakeMember(role="investor", user_id=user_id))
        sess.status = "joined"

    def simulate_bind(self, session_id: str, group_chat_id: int) -> None:
        self._sessions[session_id].group_chat_id = group_chat_id

    def simulate_cancel(self, session_id: str) -> None:
        self._sessions[session_id].status = "canceled"

    def lookup(self, session_id: str) -> FakeSession:
        return self._sessions[session_id]

    # ---- client-compatible surface ---------------------------------------
    #
    # Mirrors the subset of SshsignSession methods invoked by run_safe.py's
    # P7-5 paths. Anything not listed here raises AttributeError (fail
    # loud — tests should exercise only the paths we've claimed support).

    def get_session(
        self, session_id: str | None = None, session_code: str | None = None,
    ) -> dict:
        if session_id:
            return self._sessions[session_id].to_dict()
        for sess in self._sessions.values():
            if sess.session_code == session_code:
                return sess.to_dict()
        from sshsign_session import SessionNotFoundError
        raise SessionNotFoundError("not found")

    def bind_group(self, session_id: str, group_chat_id: int) -> dict:
        self._sessions[session_id].group_chat_id = group_chat_id
        return self._sessions[session_id].to_dict()

    def update_session_member(
        self, session_id: str, field: str, value: int,
    ) -> dict:
        sess = self._sessions[session_id]
        # Creator-only, so the update goes on the founder row.
        for m in sess.members:
            if m.role == "founder":
                setattr(m, field, value)
                return {"ok": True}
        from sshsign_session import SshsignSessionError
        raise SshsignSessionError("no founder member row")

    def complete_session(self, session_id: str, executed_artifact: str) -> dict:
        sess = self._sessions[session_id]
        sess.status = "completed"
        sess.executed_artifact = executed_artifact
        return sess.to_dict()

    def acquire_lease(
        self,
        session_id: str,
        role: str,
        action: str,
        holder: str,
        ttl_seconds: int | None = None,
    ) -> dict:
        sess = self._sessions[session_id]
        key = (role, action)
        existing = sess.leases.get(key)
        generation = int(existing["generation"]) + 1 if existing else 1
        lease = {
            "session_id": session_id,
            "role": role,
            "action": action,
            "holder": holder,
            "generation": generation,
        }
        sess.leases[key] = lease
        return lease

    def check_lease(
        self,
        session_id: str,
        role: str,
        action: str,
        holder: str,
        generation: int,
    ) -> dict:
        lease = self._sessions[session_id].leases[(role, action)]
        if lease["holder"] != holder or int(lease["generation"]) != int(generation):
            from sshsign_session import LeaseHolderMismatchError
            raise LeaseHolderMismatchError("lease_holder_mismatch")
        return lease

    def release_lease(
        self,
        session_id: str,
        role: str,
        action: str,
        holder: str,
        generation: int,
    ) -> dict:
        sess = self._sessions[session_id]
        key = (role, action)
        lease = sess.leases.get(key)
        if lease and lease["holder"] == holder and int(lease["generation"]) == int(generation):
            sess.leases.pop(key, None)
        return {"ok": True}


class GroupBus:
    """A shared message log for the 'Telegram group'. Either bot
    appending here is observable by tests. Critically: nothing in
    this harness simulates Telegram's bot-filter (the Day-0 probe
    failure) — if code under test ever *reads* another bot's message
    here, that's a regression.
    """

    def __init__(self) -> None:
        self.messages: list[tuple[str, str, str]] = []  # (sender_label, target, body)

    def make_sender(self, label: str):
        def sender(target, message=None, **kwargs):
            if message is not None:
                body = message
            elif kwargs.get("media_path"):
                body = f"[media] {kwargs['media_path']}"
            else:
                body = ""
            self.messages.append((label, str(target), body))
        return sender

    def group_messages(self, group_chat_id: int | str) -> list[str]:
        tgt = str(group_chat_id)
        return [body for _, t, body in self.messages if t == tgt]

    def dm_messages(self, dm_chat_id: int | str) -> list[str]:
        tgt = str(dm_chat_id)
        return [body for _, t, body in self.messages if t == tgt]

    def all_from(self, label: str) -> list[tuple[str, str]]:
        return [(t, b) for lbl, t, b in self.messages if lbl == label]
