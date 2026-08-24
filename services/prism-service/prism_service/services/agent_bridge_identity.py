"""Durable identity for agent-bridge sessions — a STABLE id, never a token.

Owner ask (2026-08-23): an already-authenticated caller should be able to
discover "what is my current active bridge session id" without a human
manually copying it out of Settings and pasting it into chat every time.
That only works if the session id itself is something an agent can look up
again after a reload/restart — but services/agent_bridge.py's session
registry is (deliberately, see its own module docstring) pure in-memory
state that a daemon restart wipes clean, and the plan explicitly forbids
ever writing the live bearer TOKEN to disk.

The split this module implements, per the owner's own suggested shape:
  - The session ID is durable — one stable id per (user_id, project_id),
    persisted here in a small sqlite table under this service's existing
    data_dir (the same `sqlite_db.connect()` chokepoint every other small
    persistent table in this codebase uses — see sync_prefs.py for the
    sibling pattern this file mirrors almost line for line).
  - The bearer TOKEN stays exactly where it already lived: nowhere but
    `AgentBridgeService._sessions` (RAM only). It is generated fresh by
    `mint_session` on every call and is NEVER passed into this module, not
    even hashed. There is nothing here to leak, because there is nothing
    here AT ALL for the token.

SECURITY REASONING (read this before touching the schema):
The natural-looking alternative — store a hash of the token here so a
future "is this token still valid" check could run against disk — was
considered and rejected. It buys nothing: the in-memory dict in
AgentBridgeService is already the single source of truth for whether a
session is live (a restart correctly invalidates everything, which the
sibling module's docstring calls out as correct behavior, not a gap to
paper over), so a durable verifier would only ever be consulted in cases
where the real answer is "no, because the process restarted" — meaning it
would either (a) sit unused, or (b) tempt someone into wiring a codepath
that treats a stale on-disk hash as a live credential, reintroducing a
real forever-lived-secret bug to solve a problem (id discovery) that
doesn't need it. So: this table carries ONLY `(user_id, project_id) ->
session_id` plus timestamps for rotation bookkeeping. Reading every row
back out of this file at rest hands an attacker nothing more than "user
X's stable bridge id is Y" — which is also exactly what the caller can
already ask for themselves via `GET /api/agent-bridge/sessions`, using
their own real credential. An id alone authorizes NOTHING: every bridge-
authenticated surface either checks the live in-memory token
(`validate_token`, browser-facing) or the caller's OWN separately-issued
access key against the session's owning user (`session_owned_by`, the
`agent_bridge_command` MCP tool's path) — never the id by itself.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Optional

from prism_service.services import sqlite_db

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_bridge_identities (
    user_id        TEXT NOT NULL,
    project_id     TEXT NOT NULL,
    session_id     TEXT NOT NULL,
    created_at     REAL NOT NULL,
    last_minted_at REAL NOT NULL,
    PRIMARY KEY (user_id, project_id)
)
"""


class AgentBridgeIdentityStore:
    """Maps (user_id, project_id) -> a stable bridge session id.

    Deliberately does NOT know about tokens at all — see this module's
    docstring. `stable_id` is the only real operation: idempotent, so
    calling it again for the same (user_id, project_id) — e.g. on every
    `mint_session`, including after a daemon restart — returns the SAME id
    every time, only ever recording that a mint happened (for rotation
    bookkeeping/diagnostics), never anything about the credential itself.
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._local = threading.local()
        with self._conn() as conn:
            conn.execute(_SCHEMA)

    def _conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite_db.connect(self._path, check_same_thread=False,
                                      timeout=5.0)
            self._local.conn = conn
        return conn

    def stable_id(self, user_id: str, project_id: str) -> str:
        """Return the durable session id for (user_id, project_id),
        minting and persisting a brand new one on first use. Race-safe: two
        concurrent first-uses both attempt the same INSERT, sqlite's own
        write-serialization plus ON CONFLICT DO UPDATE means exactly one
        `session_id` value ever wins and is what every caller reads back."""
        conn = self._conn()
        candidate = uuid.uuid4().hex
        now = time.time()
        with conn:
            conn.execute(
                "INSERT INTO agent_bridge_identities "
                "(user_id, project_id, session_id, created_at, last_minted_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id, project_id) "
                "DO UPDATE SET last_minted_at=excluded.last_minted_at",
                (user_id, project_id, candidate, now, now),
            )
        row = conn.execute(
            "SELECT session_id FROM agent_bridge_identities "
            "WHERE user_id=? AND project_id=?",
            (user_id, project_id),
        ).fetchone()
        return row[0]


_store: Optional[AgentBridgeIdentityStore] = None
_store_lock = threading.Lock()


def get_agent_bridge_identity_store() -> AgentBridgeIdentityStore:
    global _store
    with _store_lock:
        if _store is None:
            from prism_service.data_dir import resolve_data_dir
            _store = AgentBridgeIdentityStore(
                str(resolve_data_dir() / "agent_bridge_identity.db"))
        return _store


def set_agent_bridge_identity_store(store: Optional[AgentBridgeIdentityStore]) -> None:
    """Test seam — mirrors services.agent_bridge.set_agent_bridge_service."""
    global _store
    with _store_lock:
        _store = store
