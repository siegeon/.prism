"""The bridge must be drivable without a particular MCP tool profile.

A session that connected on the `interactive` tool profile can SEE a live
bridge session via GET /sessions and had no way at all to drive it:
agent_bridge_command is not in that profile and there was no route to fall
back to. Measured 2026-08-29: GET /api/agent-bridge/sessions returned a live
session id while ToolSearch for agent_bridge_command found nothing.
"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service.api import security  # noqa: E402
from prism_service.services.agent_bridge import KNOWN_ACTIONS  # noqa: E402


def test_the_commands_route_stays_inside_the_general_auth_gate():
    """The token carve-out is for calls the BROWSER makes. /commands is the
    agent side and authenticates on the caller's real principal, so it must
    NOT be waved through."""
    assert security._is_agent_bridge_session_path(
        "/api/agent-bridge/sessions/abc/commands") is False, (
        "the agent-side command route was carved out of the auth gate")


def test_the_browser_side_routes_keep_their_carve_out():
    """Regression guard: the fix must not close the door the browser needs."""
    assert security._is_agent_bridge_session_path(
        "/api/agent-bridge/sessions/abc/results") is True
    assert security._is_agent_bridge_session_path(
        "/api/agent-bridge/sessions/abc") is True
    assert security._is_agent_bridge_session_path(
        "/api/agent-bridge/sessions") is False


def test_the_known_actions_cover_the_parity_set():
    """The REST seam validates against the same action set the MCP tool
    dispatches, so the two cannot disagree about what is drivable."""
    for a in ("navigate", "click", "read", "screenshot", "console",
              "network", "hover", "wait_for", "find"):
        assert a in KNOWN_ACTIONS, a


def test_the_route_is_registered_on_the_bridge_router():
    from prism_service.api.agent_bridge import router
    paths = {getattr(r, "path", "") for r in router.routes}
    assert "/sessions/{session_id}/commands" in paths, sorted(paths)
