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

SUPERSEDED 2026-08-21 (owner, live correction against a real dropped session):
the plan's original call was a short (20-minute) clock-based TTL. The owner's
actual model is "tied to the machine and good until it's not — e.g. I turn
the feature off": a session must stay valid for as long as it was never
explicitly ended (disable(), tab-close via beforeunload) or the process
restarted, NOT expire on a fixed wall-clock timer while the tab is still
sitting there with the feature on. `SESSION_TTL_SECONDS` below is now a long
hygiene backstop against a truly-abandoned session (tab killed hard enough
that beforeunload never fired) rather than a real "support session" duration.

EXTENDED 2026-08-23 (owner: no more manual copy/paste of a session id into
chat, and the frontend toggle should survive a reload/restart): the id half
of a session can now be STABLE per (user_id, project_id) — backed by
services/agent_bridge_identity.py's small durable sqlite table — even though
the TOKEN keeps rotating exactly as before (fresh every `mint_session` call,
RAM only, never touches that table in any form). This is additive and
opt-in: pass an `identity_store` to get stable ids (production wiring, via
`get_agent_bridge_service()`, always does); omit it and `mint_session` falls
back to the old random-uuid-per-mint behavior untouched (every existing
direct `AgentBridgeService()` test keeps its original meaning). A stable id
is what makes `GET /api/agent-bridge/sessions` (api/agent_bridge.py) useful:
an already-authenticated caller can look up their OWN currently-live
session id(s) — authenticated the exact same way as `POST /sessions` — with
no id/token ever needing to be pasted by a human. See
agent_bridge_identity.py's docstring for the full security reasoning on why
the token itself still never reaches disk.
"""

from __future__ import annotations

import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from prism_service.events import bus

# Only imported for the type hint (constructor param) -- kept as a
# `TYPE_CHECKING`-free plain import since it's a tiny, dependency-free module
# and every existing caller already imports prism_service.services.* eagerly.
from prism_service.services.agent_bridge_identity import AgentBridgeIdentityStore

# A hygiene backstop only, NOT the session's real lifetime (see SUPERSEDED
# note above) -- a session ends via explicit revoke (disable / tab close) or
# a daemon restart; this just bounds a session whose tab vanished without
# ever signaling that (crash, force-quit) so it doesn't linger forever.
SESSION_TTL_SECONDS = 30 * 24 * 60 * 60

# How long the MCP tool call (agent_bridge_command) blocks waiting for the
# browser's result before giving up and reporting a timeout. Short enough
# that a dead/closed tab fails an agent's call promptly rather than hanging
# it indefinitely.
COMMAND_TIMEOUT_SECONDS = 20.0

# The actions a bridge client understands. Lives HERE, on the service both
# callers already import, so the MCP tool and the REST route cannot drift
# apart about what is drivable. (mcp/tools.py still carries its own literal
# copy for its arg-shaping; unifying that is follow-up, not this seam.)
KNOWN_ACTIONS: frozenset = frozenset({
    "navigate", "click", "fill", "read", "screenshot",
    "console", "network", "hover", "drag", "select_option",
    "file_upload", "press_key", "handle_dialog", "wait_for",
    "tabs", "navigate_back", "find",
})


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

    def __init__(self, identity_store: Optional[AgentBridgeIdentityStore] = None) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, BridgeSession] = {}
        self._pending: dict[str, _PendingCommand] = {}
        # Opt-in: None (the default) preserves the original random-uuid id
        # every mint -- every pre-existing test/caller that constructs this
        # class directly keeps its exact old behavior. Only
        # get_agent_bridge_service()'s production wiring passes the real
        # store, which is what makes ids stable across restarts/reloads.
        self._identity = identity_store

    # -- session lifecycle --------------------------------------------------

    def mint_session(self, *, user_id: str, project_id: str) -> BridgeSession:
        self._purge_expired_locked()
        project_id = project_id or "default"
        session_id = (
            self._identity.stable_id(user_id, project_id)
            if self._identity is not None
            else uuid.uuid4().hex
        )
        now = time.time()
        session = BridgeSession(
            id=session_id,
            token=secrets.token_urlsafe(32),
            user_id=user_id,
            project_id=project_id,
            created_at=now,
            expires_at=now + SESSION_TTL_SECONDS,
        )
        with self._lock:
            # Re-minting the SAME stable id (daemon restart, periodic
            # rotation, a duplicate enable() call) intentionally OVERWRITES
            # whatever token/expiry was there -- that IS the rotation: the
            # prior token stops validating the instant this replaces it,
            # even though the id a human/agent already has stays correct.
            self._sessions[session.id] = session
        return session

    def sessions_for_user(
        self, user_id: str, project_id: Optional[str] = None,
    ) -> list[BridgeSession]:
        """Every session currently LIVE in THIS process's memory for one
        user -- the data source behind `GET /api/agent-bridge/sessions`
        (api/agent_bridge.py), which lets an already-authenticated caller
        discover their own active session id(s) with no human pasting
        required. Deliberately reads live in-memory state only: there is
        nothing durable to consult about "is this live right now" (a
        restart correctly drops every session, per this module's SECURITY
        POSTURE note above), so "live" can only ever mean "in this dict,
        this process, right now" -- optionally narrowed to one project."""
        self._purge_expired_locked()
        with self._lock:
            sessions = [
                s for s in self._sessions.values()
                if s.user_id == user_id and not s.revoked
                and (project_id is None or s.project_id == project_id)
            ]
        return sessions

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
            from prism_service.services.agent_bridge_identity import (
                get_agent_bridge_identity_store)
            # PRODUCTION wiring only -- this is what makes a real user's
            # session id stable across a daemon restart/reload (see the
            # module docstring's 2026-08-23 note). Direct
            # `AgentBridgeService()` construction (every existing unit test)
            # deliberately does NOT go through this path.
            _service = AgentBridgeService(
                identity_store=get_agent_bridge_identity_store())
        return _service


def set_agent_bridge_service(service: Optional[AgentBridgeService]) -> None:
    """Test seam — mirrors services.workspace_service.set_workspace_service."""
    global _service
    with _service_lock:
        _service = service
