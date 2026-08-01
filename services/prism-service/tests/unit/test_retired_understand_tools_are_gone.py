"""The six legacy understand_* tools are retired from the MCP registry.

Task 4899173a acts on the RETIRE rows of docs/mcp-tool-usage-ledger.md.
They are superseded by the okf_* Understand wiki (okf_index / okf_get /
okf_graph); tools.py has carried a comment saying so without anyone
performing the deletion.

Every assertion here runs against the LIVE registry (the imported module),
never a source grep — a comment mentioning a tool name must not be able to
satisfy or break these.
"""
from __future__ import annotations

from prism_service.mcp.tools import (
    ADMIN_TOOL_NAMES,
    AUTOMATION_TOOL_NAMES,
    HOOK_TOOL_NAMES,
    LEARNING_TOOL_NAMES,
    TOOLS,
    tool_names_for_profile,
)

# The six retired by this task.
RETIRED = (
    "understand_bootstrap",
    "understand_configure",
    "understand_get_domains",
    "understand_get_layers",
    "understand_get_onboarding",
    "understand_get_tour",
)

# The four that MUST survive. The shared understand_ prefix is NOT the
# retirement criterion — the ledger's consumer column is. refresh/status are
# called by assets/stop_record_hook.py over the automation profile, and
# nothing else in the unit suite exercises that hook, so sweeping them in on
# the prefix would break it silently.
KEPT = (
    "understand_refresh",
    "understand_status",
    "understand_drain_queue",
    "understand_store_result",
)

# Superseded drive verbs: already tool_profile=all only. No code change is
# expected — pinned so they cannot drift back into a default surface.
DEMOTED = (
    "conductor_advance",
    "conductor_gate",
    "workflow_advance",
    "workflow_state",
)

EXPECTED_TOTAL = 67
EXPECTED_INTERACTIVE = 28


def _registry() -> set[str]:
    return {tool.name for tool in TOOLS}


def test_retired_tools_are_absent_from_the_registry():
    """AC-1: none of the six resolves to a Tool any more."""
    still_here = sorted(name for name in RETIRED if name in _registry())
    assert not still_here, (
        f"retired tools still registered: {still_here} — superseded by the "
        "okf_* Understand wiki (okf_index / okf_get / okf_graph)"
    )


def test_no_profile_can_resolve_a_retired_tool():
    """AC-2: absent from every profile, not merely from the default one."""
    for profile in ("all", "interactive", "automation"):
        names = tool_names_for_profile(profile)
        leaked = sorted(name for name in RETIRED if name in names)
        assert not leaked, f"profile {profile!r} still offers {leaked}"


def test_no_profile_set_names_a_phantom_tool():
    """AC-2 (other direction): a name in a profile set with no backing Tool
    would make the server advertise or reject something that cannot run."""
    registry = _registry()
    for label, names in (
        ("ADMIN", ADMIN_TOOL_NAMES),
        ("HOOK", HOOK_TOOL_NAMES),
        ("LEARNING", LEARNING_TOOL_NAMES),
        ("AUTOMATION", AUTOMATION_TOOL_NAMES),
    ):
        phantom = sorted(names - registry)
        assert not phantom, f"{label}_TOOL_NAMES names unregistered {phantom}"


def test_load_bearing_understand_tools_survive():
    """AC-3: the retirement must not sweep siblings in on the name prefix."""
    missing = sorted(name for name in KEPT if name not in _registry())
    assert not missing, f"retirement over-reached and removed {missing}"


def test_stop_hook_keeps_its_automation_tools():
    """AC-4: stop_record_hook.py connects on the automation profile and calls
    understand_refresh / understand_status. No unit test drives that hook, so
    this membership check is the only thing standing between a prefix-based
    deletion and a silently broken hook."""
    automation = tool_names_for_profile("automation")
    for name in ("understand_refresh", "understand_status"):
        assert name in automation, f"{name} left the automation profile"


def test_exactly_six_tools_were_removed():
    """AC-5: an equality, never an inequality — a loose bound would let an
    accidental extra deletion pass silently."""
    assert len(_registry()) == EXPECTED_TOTAL


def test_default_surface_is_unchanged():
    """AC-6: all six were already all-only, so no client connecting on the
    default profile loses anything it could reach."""
    assert len(tool_names_for_profile("interactive")) == EXPECTED_INTERACTIVE


def test_superseded_drive_verbs_stay_out_of_reachable_profiles():
    """AC-7: conductor_work is the drive verb; these stay behind
    tool_profile=all so they cannot drift back into a default surface."""
    for profile in ("interactive", "automation"):
        names = tool_names_for_profile(profile)
        leaked = sorted(name for name in DEMOTED if name in names)
        assert not leaked, f"profile {profile!r} re-exposed {leaked}"
