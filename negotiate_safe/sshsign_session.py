"""SSH client for sshsign's signing_sessions API.

Thin wrapper around `ssh sshsign.dev (create|join|get|cancel|complete|audit)-session`.
Handles the subprocess call, JSON response parsing, and defensive error
mapping. Callers get typed dicts or raise specific exceptions.

Kept separate from telegram_push.py and run_safe.py so it stays easy to
unit-test by injecting the `runner` callable.
"""
from __future__ import annotations

import json
import subprocess
from typing import Any, Callable, Optional


class SshsignSessionError(Exception):
    """Base for all sshsign session errors."""


class SessionNotFoundError(SshsignSessionError):
    """`get-session` returned not_found, or `join-session` saw an unknown code."""


class SessionTerminalError(SshsignSessionError):
    """Session is in a terminal state (completed, canceled, expired)."""


class SessionExpiredError(SshsignSessionError):
    """Session passed its TTL and has been marked expired."""


class SessionRoleConflictError(SshsignSessionError):
    """Another member already has the role you're trying to claim."""


class SessionNotCreatorError(SshsignSessionError):
    """Operation requires the caller to be the session creator."""


class SessionNotMemberError(SshsignSessionError):
    """Operation requires the caller to be a member of the session."""


class GroupAlreadyBoundError(SshsignSessionError):
    """bind-group: this session is already bound to a different group_chat_id.
    Write-once: the caller must cancel and start a new session if they want
    to change the binding."""


# Map sshsign's error strings back to Python exception classes. sshsign
# returns plain-text error strings via JSON {"error": "..."}, so we match
# on substrings. Brittle-ish but gives callers useful types.
def _error_from_message(msg: str) -> SshsignSessionError:
    m = msg.lower()
    if "not found" in m or "session not found" in m:
        return SessionNotFoundError(msg)
    if "expired" in m:
        return SessionExpiredError(msg)
    if "already has a member" in m or "role" in m and "already" in m:
        return SessionRoleConflictError(msg)
    if "only the session creator" in m or "not creator" in m:
        return SessionNotCreatorError(msg)
    if "not a member" in m:
        return SessionNotMemberError(msg)
    if "terminal" in m or "already completed" in m or "already canceled" in m:
        return SessionTerminalError(msg)
    if "group_already_bound" in m:
        return GroupAlreadyBoundError(msg)
    return SshsignSessionError(msg)


SshsignRunner = Callable[..., subprocess.CompletedProcess]


class SshsignSession:
    """Client for sshsign's signing_sessions commands.

    Parameters
    ----------
    host : str
        The sshsign host, e.g. "sshsign.dev". Passed straight to ssh.
    runner : Callable, optional
        Injection point for tests. Defaults to `subprocess.run`. The
        runner receives the full argv list + keyword args and must
        return a `subprocess.CompletedProcess`-compatible object
        (attrs: returncode, stdout, stderr).
    ssh_bin : str, optional
        Name/path of the ssh binary. Defaults to "ssh".
    """

    def __init__(
        self,
        host: str = "sshsign.dev",
        runner: SshsignRunner = subprocess.run,
        ssh_bin: str = "ssh",
    ):
        self.host = host
        self.runner = runner
        self.ssh_bin = ssh_bin

    def _run(self, command: str, *flags: str, timeout: int = 20) -> dict[str, Any]:
        argv = [self.ssh_bin, self.host, command, *flags]
        try:
            result = self.runner(
                argv, capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            raise SshsignSessionError(f"ssh timed out: {e}") from e
        except FileNotFoundError as e:
            raise SshsignSessionError(f"ssh not found: {e}") from e

        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            raise SshsignSessionError(
                f"ssh exited {result.returncode}: {stderr or '(no stderr)'}"
            )

        stdout = (result.stdout or "").strip()
        if not stdout:
            raise SshsignSessionError("ssh returned empty response")

        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as e:
            raise SshsignSessionError(
                f"ssh returned non-JSON: {stdout!r}"
            ) from e

        if isinstance(payload, dict) and payload.get("error"):
            raise _error_from_message(str(payload["error"]))
        return payload

    # ------------------------------------------------------------------
    # Session commands
    # ------------------------------------------------------------------

    def create_session(
        self,
        session_id: str,
        role: str,
        apoa_pubkey_pem: str,
        party_did: Optional[str] = None,
        metadata_public: Optional[dict] = None,
        metadata_member: Optional[dict] = None,
        ttl_seconds: Optional[int] = None,
    ) -> dict[str, Any]:
        """Create a new signing session; return the session dict including session_code."""
        flags = [
            "--session-id", session_id,
            "--role", role,
            "--apoa-pubkey", apoa_pubkey_pem,
        ]
        if party_did:
            flags += ["--party-did", party_did]
        if metadata_public is not None:
            flags += ["--metadata-public", json.dumps(metadata_public)]
        if metadata_member is not None:
            flags += ["--metadata-member", json.dumps(metadata_member)]
        if ttl_seconds is not None:
            flags += ["--ttl", str(ttl_seconds)]
        return self._run("create-session", *flags)

    def join_session(
        self,
        session_code: str,
        role: str,
        apoa_pubkey_pem: str,
        party_did: Optional[str] = None,
    ) -> dict[str, Any]:
        """Join an existing session by session_code."""
        flags = [
            "--session-code", session_code,
            "--role", role,
            "--apoa-pubkey", apoa_pubkey_pem,
        ]
        if party_did:
            flags += ["--party-did", party_did]
        return self._run("join-session", *flags)

    def get_session(
        self,
        session_code: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Fetch session state by code OR id. Caller is member ⇒ full response;
        otherwise metadata_member and members list are omitted."""
        if session_code:
            return self._run("get-session", "--session-code", session_code)
        if session_id:
            return self._run("get-session", "--session-id", session_id)
        raise ValueError("session_code or session_id is required")

    def cancel_session(self, session_id: str, rescind: bool = False) -> dict[str, Any]:
        """Cancel a session. `rescind=True` produces the rescinded_after_sign
        terminal state (distinct from ordinary cancellation)."""
        flags = ["--session-id", session_id]
        if rescind:
            flags.append("--rescind")
        return self._run("cancel-session", *flags)

    def complete_session(
        self, session_id: str, executed_artifact: str,
    ) -> dict[str, Any]:
        """Creator-only. Idempotent for the same args."""
        return self._run(
            "complete-session",
            "--session-id", session_id,
            "--executed-artifact", executed_artifact,
        )

    def bind_group(self, session_id: str, group_chat_id: int) -> dict[str, Any]:
        """Bind a session to a chat venue (Telegram group chat_id or equivalent).

        Write-once: if the session already has a binding that matches, this
        is an idempotent no-op and returns the current session. If the
        existing binding differs, raises GroupAlreadyBoundError — the caller
        must cancel and start a new session to re-bind.

        Any member may bind; rejected if the session is in a terminal state.
        """
        return self._run(
            "bind-group",
            "--session-id", session_id,
            "--group-chat-id", str(int(group_chat_id)),
        )

    def audit_session(self, session_id: str) -> list[dict[str, Any]]:
        """Member-only. Returns list of transition events."""
        result = self._run("audit-session", "--session-id", session_id)
        if isinstance(result, list):
            return result
        raise SshsignSessionError(
            f"expected audit list, got: {type(result).__name__}"
        )
