"""RED scaffold — prism_install emits a malformed SessionStart hook (#168).

Claude Code's settings.json schema maps each hook EVENT to a LIST of groups,
where each group is `{"matcher": "", "hooks": [ {"type": "command",
"command": "...", "timeout"?: N} ]}`. The current _install_manifest emits the
SessionStart command dict DIRECTLY in the event list (no `hooks` array
wrapper), so Claude Code /doctor errors and the SessionStart sync never fires.

Pins, RED-first:
  * AC-1 — the generated SessionStart entry is a list of groups, each with a
    `hooks` array wrapper holding the command dict.
  * AC-2 — EVERY emitted hook event is well-formed (iterate + assert the
    same group shape).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _hooks_map(host_platform="linux"):
    from prism_service.mcp.tools import _install_manifest
    manifest = _install_manifest("proj-168", host_platform)
    settings = next(
        f for f in manifest["install_files"]
        if f["path"] == ".claude/settings.json")
    return json.loads(settings["content"])["hooks"]


def _assert_well_formed_event(groups):
    assert isinstance(groups, list) and groups, groups
    for grp in groups:
        assert isinstance(grp, dict), grp
        assert "hooks" in grp, f"group missing hooks[] wrapper: {grp}"
        assert isinstance(grp["hooks"], list) and grp["hooks"], grp
        for hook in grp["hooks"]:
            assert hook.get("type") == "command", hook
            assert isinstance(hook.get("command"), str) and hook["command"], hook


def test_session_start_has_hooks_array_wrapper():
    hooks = _hooks_map()
    assert "SessionStart" in hooks
    _assert_well_formed_event(hooks["SessionStart"])


def test_all_emitted_hook_events_are_well_formed():
    hooks = _hooks_map()
    assert hooks, "manifest must emit at least one hook event"
    for event, groups in hooks.items():
        _assert_well_formed_event(groups)
