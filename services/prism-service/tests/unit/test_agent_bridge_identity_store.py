"""Unit tests for services/agent_bridge_identity.py -- the durable
(user_id, project_id) -> stable session id mapping, and the hard security
constraint that the live bearer TOKEN never has anywhere to go in this
store (there is no column for it at all).
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _fresh_store(tmp_path):
    from prism_service.services.agent_bridge_identity import AgentBridgeIdentityStore
    return AgentBridgeIdentityStore(str(tmp_path / "agent_bridge_identity.db"))


def test_stable_id_is_the_same_across_repeated_calls(tmp_path):
    store = _fresh_store(tmp_path)
    first = store.stable_id("alice", "prism")
    second = store.stable_id("alice", "prism")
    third = store.stable_id("alice", "prism")
    assert first == second == third


def test_stable_id_differs_across_users(tmp_path):
    store = _fresh_store(tmp_path)
    alice = store.stable_id("alice", "prism")
    mallory = store.stable_id("mallory", "prism")
    assert alice != mallory


def test_stable_id_differs_across_projects_for_the_same_user(tmp_path):
    store = _fresh_store(tmp_path)
    a = store.stable_id("alice", "prism")
    b = store.stable_id("alice", "other-project")
    assert a != b


def test_stable_id_survives_a_fresh_store_instance_against_the_same_path(tmp_path):
    """Simulates a daemon restart: a brand new AgentBridgeIdentityStore
    object, same underlying sqlite file, must still hand back the SAME id
    -- this is exactly what lets the id be discoverable/reconnectable
    across a real restart while the token (never stored here) rotates."""
    path = str(tmp_path / "agent_bridge_identity.db")
    from prism_service.services.agent_bridge_identity import AgentBridgeIdentityStore

    first_id = AgentBridgeIdentityStore(path).stable_id("alice", "prism")
    second_id = AgentBridgeIdentityStore(path).stable_id("alice", "prism")
    assert first_id == second_id


def test_the_schema_has_no_column_that_could_hold_a_token(tmp_path):
    """Hard security constraint, checked structurally rather than just by
    convention: this table must be physically incapable of storing a
    bearer token, hashed or otherwise -- id + bookkeeping timestamps only."""
    store = _fresh_store(tmp_path)
    store.stable_id("alice", "prism")  # ensure the table exists
    conn = sqlite3.connect(str(tmp_path / "agent_bridge_identity.db"))
    try:
        cols = {row[1] for row in conn.execute(
            "PRAGMA table_info(agent_bridge_identities)")}
    finally:
        conn.close()
    assert cols == {"user_id", "project_id", "session_id",
                     "created_at", "last_minted_at"}
    for forbidden in ("token", "secret", "credential", "bearer"):
        assert not any(forbidden in c.lower() for c in cols), (
            f"a column resembling a credential ({forbidden!r}) must never "
            f"exist in this table -- found columns: {cols}")


def test_get_and_set_agent_bridge_identity_store_singleton_seam(tmp_path):
    """Mirrors services.agent_bridge's set_agent_bridge_service test seam."""
    from prism_service.services import agent_bridge_identity as mod

    store = mod.AgentBridgeIdentityStore(str(tmp_path / "x.db"))
    mod.set_agent_bridge_identity_store(store)
    try:
        assert mod.get_agent_bridge_identity_store() is store
    finally:
        mod.set_agent_bridge_identity_store(None)
