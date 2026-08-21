"""Agent bridge: session lifecycle for the live remote-assist feature.

Plan: an authorized agent (holding a user's own PRISM access key — task
6cef97ec's "holding the key means act as this user, full stop") drives that
SAME user's own already-open browser tab, live, via structured commands
(navigate/click/fill/read). No separate browser, no screen relay — see
`/home/siegeon/.claude/plans/peaceful-seeking-octopus.md` for the full design
and the build-vs-buy reasoning (co-browsing SaaS rejected: third-party
hosting conflicts with this repo's data-stays-in-PRISM posture, and they're
built for human-to-human, not agent-issued structured commands).

This module owns exactly the session/command bookkeeping:
  - mint / validate_token / revoke  — the bridge session's own short-lived,
    narrowly-scoped credential (distinct from the user's real access key).
  - publish_command / submit_result / wait_for_result — commands flow out
    over the SAME event bus (prism_service.events.bus) every other live-push
    feature already uses (routes/sse.py); results flow back in-process via a
    plain threading.Event, because `_dispatch_tool` (the MCP tool's caller)
    already runs on a worker thread (asyncio.to_thread), so blocking there is
    the correct, simple implementation — no new async machinery needed.

SECURITY POSTURE (see plan): sessions are held ONLY in this process's memory
— never written to sqlite, never logged. A daemon restart drops every live
bridge session, which is the correct behavior (the tab re-enables remote
assist and gets a fresh token), not a bug. The token is compared with
`secrets.compare_digest` to avoid a timing side-channel identical in shape to
the general access-key comparison in services/auth_service.py.
"""

from __future__ import annotations

import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from prism_service.events import bus

# Short-lived like this repo's other single-purpose tokens (security posture:
# "it should expire ... rather than persist"). 20 minutes is enough for a
# support session without leaving a stale credential live for hours.
SESSION_TTL_SECONDS = 20 * 60

# How long the MCP tool call (agent_bridge_command) blocks waiting for the
# browser's result before giving up and reporting a timeout. Short enough
# that a dead/closed tab fails an agent's call promptly rather than hanging
# it indefinitely.
COMMAND_TIMEOUT_SECONDS = 20.0


@dataclass
class BridgeSession:
    id: str
    token: str
    user_id: str
    project_id: str
    created_at: float
    expires_at: float
    revoked: bool = False


@dataclass
class _PendingCommand:
    event: threading.Event = field(default_factory=threading.Event)
    result: Optional[dict] = None


class AgentBridgeService:
    """Process-wide in-memory session + command registry.

    Deliberately NOT backed by sqlite (see module docstring) — the whole
    store lives in `self._sessions` / `self._pending` and disappears with
    the process, by design.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, BridgeSession] = {}
        self._pending: dict[str, _PendingCommand] = {}

    # -- session lifecycle --------------------------------------------------

    def mint_session(self, *, user_id: str, project_id: str) -> BridgeSession:
        self._purge_expired_locked()
        now = time.time()
        session = BridgeSession(
            id=uuid.uuid4().hex,
            token=secrets.token_urlsafe(32),
            user_id=user_id,
            project_id=project_id or "default",
            created_at=now,
            expires_at=now + SESSION_TTL_SECONDS,
        )
        with self._lock:
            self._sessions[session.id] = session
        return session

    def _get_live(self, session_id: str) -> Optional[BridgeSession]:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            if session.revoked or session.expires_at < time.time():
                return None
            return session

    def validate_token(self, session_id: str, token: str) -> Optional[BridgeSession]:
        """The narrow bridge-session credential check every browser-facing
        agent-bridge route performs itself (SSE, results, delete) instead of
        going through the general bearer/access-key path — see
        api/security.py's carve-out for these item paths."""
        session = self._get_live(session_id)
        if session is None:
            return None
        if not token or not secrets.compare_digest(session.token, token):
            return None
        return session

    def session_owned_by(self, session_id: str, user_id: str) -> Optional[BridgeSession]:
        """Command-submission check (MCP tool side): the caller's OWN
        access key must already resolve to the session's owning user — this
        is task 6cef97ec's existing 'holding the key = acting as them' model,
        not a new authorization concept."""
        session = self._get_live(session_id)
        if session is None or session.user_id != user_id:
            return None
        return session

    def revoke(self, session_id: str) -> bool:
        with self._lock:
            session = self._sessions.pop(session_id, None)
        return session is not None

    # -- commands -------------------------------------------------------

    def publish_command(self, session: BridgeSession, action: str, fields: dict) -> str:
        """Publish one command onto the bus, scoped to this session, and
        register a waiter for its result. Returns the command_id."""
        command_id = uuid.uuid4().hex
        with self._lock:
            self._pending[command_id] = _PendingCommand()
        event = {
            "project": session.project_id,
            "type": "agent_bridge.command",
            "session_id": session.id,
            "command_id": command_id,
            "action": action,
            **fields,
        }
        bus.publish(event)
        return command_id

    def submit_result(self, session_id: str, command_id: str, result: dict) -> bool:
        """Browser -> server result delivery. Returns False when no MCP
        call is (or ever was) waiting on this command_id — a stale/duplicate
        /forged result, reported to the caller as 404 rather than silently
        accepted."""
        with self._lock:
            pending = self._pending.get(command_id)
            if pending is None:
                return False
            pending.result = result
        pending.event.set()
        return True

    def wait_for_result(
        self, command_id: str, timeout: float = COMMAND_TIMEOUT_SECONDS,
    ) -> dict:
        with self._lock:
            pending = self._pending.get(command_id)
        if pending is None:
            return {"ok": False, "error": "unknown command_id"}
        got = pending.event.wait(timeout)
        with self._lock:
            self._pending.pop(command_id, None)
        if not got:
            return {"ok": False, "error": "timed out waiting for the browser tab to respond"}
        return pending.result or {"ok": False, "error": "empty result"}

    # -- housekeeping -----------------------------------------------------

    def _purge_expired_locked(self) -> None:
        now = time.time()
        with self._lock:
            expired = [sid for sid, s in self._sessions.items() if s.expires_at < now]
            for sid in expired:
                self._sessions.pop(sid, None)


_service: Optional[AgentBridgeService] = None
_service_lock = threading.Lock()


def get_agent_bridge_service() -> AgentBridgeService:
    global _service
    with _service_lock:
        if _service is None:
            _service = AgentBridgeService()
        return _service


def set_agent_bridge_service(service: Optional[AgentBridgeService]) -> None:
    """Test seam — mirrors services.workspace_service.set_workspace_service."""
    global _service
    with _service_lock:
        _service = service
