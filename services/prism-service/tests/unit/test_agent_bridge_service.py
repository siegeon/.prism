"""Unit tests for services/agent_bridge.py -- session lifecycle + the
command/result round trip the agent_bridge_command MCP tool blocks on.

Plan: /home/siegeon/.claude/plans/peaceful-seeking-octopus.md. Drives the
REAL AgentBridgeService and the REAL prism_service.events.bus (same pattern
as tests/unit/test_sse_work_route.py), never a mock of either.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _fresh_service():
    from prism_service.services.agent_bridge import AgentBridgeService
    return AgentBridgeService()


def test_mint_session_returns_a_distinct_short_lived_token():
    svc = _fresh_service()
    a = svc.mint_session(user_id="alice", project_id="prism")
    b = svc.mint_session(user_id="alice", project_id="prism")
    assert a.id != b.id
    assert a.token != b.token
    assert a.expires_at > time.time()


def test_validate_token_accepts_only_the_right_session_and_token():
    svc = _fresh_service()
    s = svc.mint_session(user_id="alice", project_id="prism")

    assert svc.validate_token(s.id, s.token) is not None
    assert svc.validate_token(s.id, "wrong-token") is None
    assert svc.validate_token("unknown-session-id", s.token) is None
    assert svc.validate_token(s.id, "") is None


def test_expired_session_is_rejected():
    svc = _fresh_service()
    s = svc.mint_session(user_id="alice", project_id="prism")
    # Force expiry rather than sleeping through the real TTL.
    with svc._lock:
        svc._sessions[s.id].expires_at = time.time() - 1
    assert svc.validate_token(s.id, s.token) is None
    assert svc.session_owned_by(s.id, "alice") is None


def test_session_outlives_the_old_20_minute_ttl_while_never_revoked():
    """Owner correction, 2026-08-21: a bridge session is 'tied to the
    machine and good until it's not -- e.g. I turn the feature off', NOT a
    fixed 20-minute clock. This reproduces the live regression (session
    7ecf377a...) where a real, still-open, never-disabled remote-assist
    session was rejected mid-use because the old 20-minute TTL had lapsed --
    a freshly minted session's expiry must sit far beyond that window, and a
    session simulated to be 20+ minutes old (but never revoked) must still
    validate."""
    svc = _fresh_service()
    s = svc.mint_session(user_id="alice", project_id="prism")

    # A fresh session must not be anywhere near the old 20-minute cliff.
    assert s.expires_at - time.time() > 24 * 60 * 60

    with svc._lock:
        # Simulate "20+ minutes have passed, tab never closed, feature
        # never disabled" -- the exact live scenario that regressed.
        svc._sessions[s.id].created_at = time.time() - (25 * 60)

    assert svc.validate_token(s.id, s.token) is not None
    assert svc.session_owned_by(s.id, "alice") is not None


def test_revoke_ends_the_session_immediately():
    svc = _fresh_service()
    s = svc.mint_session(user_id="alice", project_id="prism")
    assert svc.validate_token(s.id, s.token) is not None

    assert svc.revoke(s.id) is True
    assert svc.validate_token(s.id, s.token) is None
    # Revoking an already-gone session reports it cleanly rather than
    # raising -- a second DELETE (or a race with expiry) must fail clean.
    assert svc.revoke(s.id) is False


def test_session_owned_by_enforces_the_owning_user():
    svc = _fresh_service()
    s = svc.mint_session(user_id="alice", project_id="prism")
    assert svc.session_owned_by(s.id, "alice") is not None
    assert svc.session_owned_by(s.id, "mallory") is None


def test_publish_command_and_submit_result_round_trip_via_the_real_bus():
    import asyncio

    from prism_service.events import bus

    svc = _fresh_service()
    s = svc.mint_session(user_id="alice", project_id="prism")

    # bus.subscribe() binds the queue to the CALLING coroutine's running
    # loop (events.py), so this must run inside an actual asyncio loop --
    # exactly like every sse_* consumer does in production.
    async def _run():
        q = bus.subscribe()
        try:
            command_id = svc.publish_command(s, "navigate", {"path": "/tasks"})

            # The command really went out on the shared bus, scoped to this
            # session -- exactly what sse_agent_bridge (routes/sse.py) reads.
            event = await asyncio.wait_for(q.get(), timeout=2.0)
            assert event["type"] == "agent_bridge.command"
            assert event["session_id"] == s.id
            assert event["command_id"] == command_id
            assert event["action"] == "navigate"
            assert event["path"] == "/tasks"
            return command_id
        finally:
            bus.unsubscribe(q)

    command_id = asyncio.run(_run())

    # A background thread stands in for the browser tab POSTing its result
    # back, so wait_for_result exercises the real blocking wait a worker
    # thread performs while dispatching the MCP tool.
    def _deliver_result():
        time.sleep(0.05)
        svc.submit_result(s.id, command_id, {"ok": True, "data": {"url": "/tasks"}})

    threading.Thread(target=_deliver_result, daemon=True).start()
    result = svc.wait_for_result(command_id, timeout=2.0)
    assert result == {"ok": True, "data": {"url": "/tasks"}}


def test_wait_for_result_times_out_cleanly_when_nothing_answers():
    svc = _fresh_service()
    s = svc.mint_session(user_id="alice", project_id="prism")
    command_id = svc.publish_command(s, "click", {"selector": "#go"})
    result = svc.wait_for_result(command_id, timeout=0.2)
    assert result["ok"] is False
    assert "timed out" in result["error"]


def test_submit_result_for_unknown_command_id_is_rejected():
    svc = _fresh_service()
    s = svc.mint_session(user_id="alice", project_id="prism")
    assert svc.submit_result(s.id, "not-a-real-command-id", {"ok": True}) is False
