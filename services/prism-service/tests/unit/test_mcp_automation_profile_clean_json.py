"""tool_profile=automation must never receive the PRISM_REFLECTION_PENDING
banner — it corrupts json.loads() for machine callers such as the shipped
.claude/hooks/prism-sync.py SessionStart hook.

Parent task: 01ec3894. See task.plan_doc for the full design (D1-D6) and
the AC-1..AC-9 acceptance criteria this suite pins (T1-T7 cover AC-1..AC-7;
AC-8/AC-9 are commands, not pytest cases).

_maybe_augment_with_nudge (tools.py:3045-3106) prepends a plain-text header
to result[0].text whenever a pending, un-nudged consolidation_candidates row
exists, regardless of who is asking. handle_tool (tools.py:3019-3042) never
learns which MCP tool_profile the caller connected with, and the sole
production call site (server.py:230) never threads ctx.tool_profile through
even though it is already in scope. This suite seeds a REAL pending
candidate row (never a mock, never the no-candidate early-return path) and
asserts the automation profile stays clean while interactive/all keep the
nudge.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import sqlite3
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


@pytest.fixture
def project(tmp_path, monkeypatch):
    """Fresh, isolated project per test — each seeded candidate can only
    carry the banner on ONE nudging call (5-min rate limit), so tests must
    not share state."""
    from prism_service import config as cfg
    from prism_service import project_context as pc
    monkeypatch.setattr(cfg, "PROJECTS_DIR", tmp_path / "projects")
    cfg.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    pc._contexts.clear()
    monkeypatch.delenv("PRISM_MCP_AUGMENT_NUDGES", raising=False)
    yield "test-automation-clean-json"
    pc._contexts.clear()


def _call(tool_name, arguments=None, *, project_id, tool_profile=None):
    """Drive handle_tool directly. `tool_profile` is only forwarded when
    the (possibly pre-fix) handle_tool signature actually accepts it, so
    this helper works unchanged before and after the fix lands."""
    from prism_service.mcp.tools import handle_tool
    kwargs = {"project_id": project_id}
    if tool_profile is not None:
        if "tool_profile" in inspect.signature(handle_tool).parameters:
            kwargs["tool_profile"] = tool_profile
    return asyncio.run(handle_tool(tool_name, arguments or {}, **kwargs))


def _text(result):
    assert len(result) == 1
    return result[0].text


def _seed_pending(project_id, task_id="T-automation"):
    """Stamp a real pending candidate through the real janitor_enqueue
    tool — never a mock — so consolidation_candidates genuinely holds a
    pending, un-nudged row."""
    _call("janitor_enqueue", {
        "task_id": task_id,
        "trigger": "task_done",
        "scope": {"task_ids": [task_id]},
    }, project_id=project_id)


def _last_nudged_at(project_id):
    from prism_service.project_context import get_project
    ctx = get_project(project_id)
    scores_path = str(ctx._data_dir / "scores.db")
    conn = sqlite3.connect(scores_path)
    try:
        row = conn.execute(
            "SELECT last_nudged_at FROM consolidation_candidates "
            "ORDER BY queued_at ASC LIMIT 1"
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


# ----------------------------------------------------------------------
# T1 -> AC-1
# ----------------------------------------------------------------------

def test_automation_profile_has_no_banner_and_stays_json_loadable(project):
    _seed_pending(project)
    text = _text(_call(
        "prism_status", {}, project_id=project, tool_profile="automation",
    ))
    assert "PRISM_REFLECTION_PENDING" not in text, (
        "automation-profile response must never carry the reflection "
        "nudge banner — it breaks json.loads() for machine callers"
    )
    parsed = json.loads(text)
    assert isinstance(parsed, dict)


# ----------------------------------------------------------------------
# T2 -> AC-2
# ----------------------------------------------------------------------

def test_interactive_profile_still_gets_the_banner(project):
    _seed_pending(project)
    text = _text(_call(
        "prism_status", {}, project_id=project, tool_profile="interactive",
    ))
    assert "PRISM_REFLECTION_PENDING" in text, (
        "this is a scoping fix, not a removal — interactive callers must "
        "still be nudged toward the prism-reflect sub-agent"
    )


# ----------------------------------------------------------------------
# T3 -> AC-3
# ----------------------------------------------------------------------

def test_all_profile_still_gets_the_banner(project):
    _seed_pending(project)
    text = _text(_call(
        "prism_status", {}, project_id=project, tool_profile="all",
    ))
    assert "PRISM_REFLECTION_PENDING" in text


# ----------------------------------------------------------------------
# T4 -> AC-4
# ----------------------------------------------------------------------

def test_automation_call_does_not_consume_the_nudge_window(project):
    _seed_pending(project)
    before = _last_nudged_at(project)
    assert before is None

    _call("prism_status", {}, project_id=project, tool_profile="automation")

    after = _last_nudged_at(project)
    assert after == before, (
        "the profile check must happen BEFORE the SELECT/UPDATE "
        "last_nudged_at write — an automation call must not consume the "
        "5-minute nudge window a human session was owed"
    )

    # The window is still intact: the next interactive call gets nudged.
    text = _text(_call(
        "prism_status", {}, project_id=project, tool_profile="interactive",
    ))
    assert "PRISM_REFLECTION_PENDING" in text


# ----------------------------------------------------------------------
# T5 -> AC-5
# ----------------------------------------------------------------------

def test_tool_profile_threaded_at_the_real_call_site(project):
    from prism_service.mcp.tools import handle_tool

    assert (
        inspect.signature(handle_tool).parameters["tool_profile"].default
        == "interactive"
    ), (
        "handle_tool must accept a tool_profile kw-arg defaulting to "
        "'interactive' (NFR-1: backward compatible)"
    )

    from prism_service.mcp.request_context import (
        PrismRequestContext, use_request_context,
    )
    from prism_service.mcp.server import call_tool

    _seed_pending(project)
    with use_request_context(
        PrismRequestContext(project_id=project, tool_profile="automation"),
    ):
        result = asyncio.run(call_tool("prism_status", {}))

    # Shape-agnostic: call_tool may return a bare list[TextContent] or a
    # CallToolResult depending on SDK version.
    content = getattr(result, "content", result)
    if not isinstance(content, (list, tuple)):
        content = [content]
    text = "".join(getattr(b, "text", "") or "" for b in content)

    assert "PRISM_REFLECTION_PENDING" not in text, (
        "server.py:230 must thread ctx.tool_profile into handle_tool — a "
        "signature default alone does not make the fix real in production"
    )
    parsed = json.loads(text)
    assert isinstance(parsed, dict)


# ----------------------------------------------------------------------
# T6 -> AC-6
# ----------------------------------------------------------------------

def test_no_augment_tools_stay_unaugmented_on_every_profile(project):
    _seed_pending(project)
    for profile in ("automation", "interactive", "all"):
        text = _text(_call(
            "janitor_check", {"session_id": f"S-{profile}"},
            project_id=project, tool_profile=profile,
        ))
        assert "PRISM_REFLECTION_PENDING" not in text, (
            f"janitor_check is in _NO_AUGMENT_TOOLS and must stay "
            f"unaugmented on tool_profile={profile!r}, unchanged by "
            f"this work"
        )


# ----------------------------------------------------------------------
# T7 -> AC-7
# ----------------------------------------------------------------------

def test_shipped_hook_survives_a_pending_candidate_on_automation(project):
    """Exec the actual shipped hook template (tools.py:2317) and feed a
    REAL automation-profile response through its own _parse_result, then
    run the exact (result or {}).get(...) sequence from main()
    (tools.py:2490-2491). Reproduces the AttributeError this task exists
    to fix when the banner leaks through."""
    from prism_service.mcp.tools import _HOOK_SCRIPT

    _seed_pending(project)
    real_result = _call(
        "prism_status", {"file_hashes": {}},
        project_id=project, tool_profile="automation",
    )
    resp = {"result": {"content": [{"text": _text(real_result)}]}}

    ns = {"__name__": "prism_sync_under_test"}
    exec(compile(_HOOK_SCRIPT, "prism-sync.py", "exec"), ns)  # noqa: S102
    parse_result = ns["_parse_result"]

    parsed = parse_result(resp)
    assert isinstance(parsed, dict), (
        f"hook's own _parse_result returned {type(parsed).__name__}, not "
        "a dict — the banner corrupted json.loads() exactly as it does "
        "in the shipped .claude/hooks/prism-sync.py SessionStart hook"
    )
    # The exact sequence from main() (tools.py:2490-2491) — must not raise
    # AttributeError. A missing key is fine (falls back to "?"); a corrupt
    # (non-dict) `parsed` is what used to blow this line up.
    status = (parsed or {}).get("prism_version") or "?"
    assert isinstance(status, str)
