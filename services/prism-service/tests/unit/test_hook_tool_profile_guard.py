"""Regression guard: every telemetry tool a shipped hook fires must be
reachable under the profile that hook connects with (GH #99 part 3).

The original silent telemetry drop (38 lost record_session_outcome
writes) happened because a hook POSTed a tool the connected profile did
not expose, the dispatcher returned a false-success, and nobody noticed.
This guard parses each shipped hook asset for (a) the `tool_profile=<x>`
literal it requests in its _mcp_call URL and (b) the telemetry-recording
tool names it passes to _mcp_call(), then asserts every such tool is in
tool_names_for_profile(that_profile). It pins the stop / skill / subagent
hooks to the automation profile — it would have gone red BEFORE those
hooks were switched to automation, and goes red again the moment a hook
fires a record_* write the profile can't reach.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_ASSETS = _SERVICE_ROOT / "prism_service" / "assets"

# The telemetry-recording verbs whose silent drop this guard exists to
# catch. These are the writes that masquerade as success when rejected.
_RECORDING_TOOLS = {
    "record_session_outcome",
    "record_skill_usage",
    "record_subagent_outcome",
    "brain_search_feedback",
    "verifier_run",
}


_PROFILE_RE = re.compile(r"tool_profile=([A-Za-z_]+)")
_MCP_CALL_RE = re.compile(
    r"""_mcp_call\(\s*base,\s*project,\s*["']([a-zA-Z_]+)["']"""
)


def _hook_assets():
    return sorted(_ASSETS.glob("*_hook.py"))


def test_hook_assets_present():
    """Sanity: the shipped hook assets exist where we expect them."""
    names = {p.name for p in _hook_assets()}
    assert {
        "stop_record_hook.py",
        "skill_usage_hook.py",
        "subagent_record_hook.py",
    } <= names, f"missing telemetry hook assets; found {names}"


def test_every_recording_tool_is_reachable_under_its_hook_profile():
    """Per asset: parse the tool_profile literal + the recording tools it
    passes to _mcp_call(), and assert each is in that profile's tool set.

    Green today (stop/skill/subagent record_* are in automation); would
    have been red before those hooks were switched off the old 'hooks'
    profile, and goes red if a future edit fires a record_* write the
    connected profile can't reach."""
    from prism_service.mcp.tools import tool_names_for_profile

    checked = 0
    for asset in _hook_assets():
        src = asset.read_text(encoding="utf-8")
        profiles = set(_PROFILE_RE.findall(src))
        assert profiles, f"{asset.name}: no tool_profile= literal found"
        recording = {
            t for t in _MCP_CALL_RE.findall(src) if t in _RECORDING_TOOLS
        }
        for profile in profiles:
            allowed = tool_names_for_profile(profile)
            for tool in recording:
                checked += 1
                assert tool in allowed, (
                    f"{asset.name}: fires '{tool}' over tool_profile="
                    f"'{profile}' but that tool is NOT in "
                    f"tool_names_for_profile('{profile}') — a silent "
                    f"telemetry drop (GH #99)."
                )
    assert checked > 0, "guard asserted nothing — parser matched no calls"
