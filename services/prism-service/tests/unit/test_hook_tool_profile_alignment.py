"""Regression guard: every tool a shipped hook calls must be granted by
the tool_profile that hook connects with (GH #99 part 3).

The shipped *_hook.py assets POST tools/call requests to the MCP server at
a URL carrying `tool_profile=<x>`. If a hook asks for a tool that the
server's `tool_names_for_profile(<x>)` does not grant, the dispatcher
rejects it — and (after the GH #99 part 3 fix) that rejection is an
isError=true drop. Before the fix the same drop was a SILENT false-success.
Either way the telemetry write never lands.

This guard statically parses each hook asset: the `tool_profile=<literal>`
out of its f-string URL(s) and every string tool name it passes into a
tools/call (via `_mcp_call(base, project, "<tool>", ...)` or an inline
`"name": "<tool>"`). It asserts each tool is in
`tool_names_for_profile(that_profile)`. This pins the stop / skill /
subagent hooks to the automation profile and would have gone red before
those hooks were switched to automation.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_ASSETS_DIR = _SERVICE_ROOT / "prism_service" / "assets"


def _hook_assets() -> list[Path]:
    return sorted(_ASSETS_DIR.glob("*_hook.py"))


def _profile_from_url_node(node: ast.AST) -> str | None:
    """Return the tool_profile literal from an f-string URL node, if any.

    Matches `f"{base}/?project={project}&tool_profile=automation"` by
    scanning the JoinedStr's literal string parts for `tool_profile=<word>`.
    """
    if not isinstance(node, ast.JoinedStr):
        return None
    text = "".join(
        v.value for v in node.values if isinstance(v, ast.Constant)
        and isinstance(v.value, str)
    )
    marker = "tool_profile="
    idx = text.find(marker)
    if idx == -1:
        return None
    rest = text[idx + len(marker):]
    token = ""
    for ch in rest:
        if ch.isalnum() or ch == "_":
            token += ch
        else:
            break
    return token or None


def _analyze_hook(path: Path) -> tuple[set[str], set[str]]:
    """Return (profiles_requested, tool_names_called) for one hook asset."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    profiles: set[str] = set()
    tools: set[str] = set()

    for node in ast.walk(tree):
        # tool_profile=<x> literals embedded in f-string URLs
        prof = _profile_from_url_node(node)
        if prof:
            profiles.add(prof)

        if not isinstance(node, ast.Call):
            continue

        # _mcp_call(base, project, "<tool>", {...})
        func = node.func
        is_mcp_call = (
            (isinstance(func, ast.Name) and func.id == "_mcp_call")
            or (isinstance(func, ast.Attribute) and func.attr == "_mcp_call")
        )
        if is_mcp_call and len(node.args) >= 3:
            third = node.args[2]
            if isinstance(third, ast.Constant) and isinstance(third.value, str):
                tools.add(third.value)

        # inline tools/call params dict: {"name": "<tool>", "arguments": ...}
        if isinstance(node, ast.Dict):
            pass  # Dicts are not Calls; handled below via separate walk.

    # Second pass for inline `{"name": "<tool>", ...}` params dicts that
    # carry an "arguments" sibling key (the tools/call params shape).
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = {
            k.value for k in node.keys
            if isinstance(k, ast.Constant) and isinstance(k.value, str)
        }
        if "name" in keys and "arguments" in keys:
            for k, v in zip(node.keys, node.values):
                if (
                    isinstance(k, ast.Constant) and k.value == "name"
                    and isinstance(v, ast.Constant) and isinstance(v.value, str)
                ):
                    tools.add(v.value)

    return profiles, tools


def test_each_hook_calls_only_tools_its_profile_grants():
    from prism_service.mcp.tools import tool_names_for_profile

    assets = _hook_assets()
    assert assets, "no *_hook.py assets discovered — guard would be vacuous"

    violations: list[str] = []
    for path in assets:
        profiles, tools = _analyze_hook(path)
        assert len(profiles) == 1, (
            f"{path.name}: expected exactly one tool_profile literal, "
            f"found {sorted(profiles)}"
        )
        (profile,) = tuple(profiles)
        granted = tool_names_for_profile(profile)
        for tool in sorted(tools):
            if tool not in granted:
                violations.append(
                    f"{path.name}: calls '{tool}' over tool_profile="
                    f"'{profile}' but that profile does not grant it"
                )

    assert not violations, (
        "shipped hooks call tools their tool_profile does not grant "
        "(silent telemetry drops):\n" + "\n".join(violations)
    )


def test_stop_skill_subagent_hooks_pinned_to_automation_recorders():
    """Explicitly pin the three outcome recorders to the automation
    profile — the original silent-drop trio."""
    from prism_service.mcp.tools import tool_names_for_profile

    expected = {
        "stop_record_hook.py": "record_session_outcome",
        "skill_usage_hook.py": "record_skill_usage",
        "subagent_record_hook.py": "record_subagent_outcome",
    }
    automation = tool_names_for_profile("automation")
    for asset_name, recorder in expected.items():
        path = _ASSETS_DIR / asset_name
        assert path.exists(), f"missing hook asset {asset_name}"
        profiles, tools = _analyze_hook(path)
        assert profiles == {"automation"}, (
            f"{asset_name}: profile {sorted(profiles)} != automation"
        )
        assert recorder in tools, (
            f"{asset_name}: does not call {recorder}"
        )
        assert recorder in automation, (
            f"{recorder} not granted by automation profile"
        )
