"""GH #173 — the reflection janitor_* tools must be SERVED over the MCP
profile the `prism-reflect` sub-agent connects through.

SessionStart advertises ``PRISM_REFLECTION_PENDING`` and the
``prism-reflect`` agent spec (assets/prism_reflect_agent.md) lists
``mcp__prism__janitor_check / janitor_submit / janitor_abandon /
memory_invalidate`` in its allow-list. The tools were registered in
TOOLS + dispatch but excluded from the default ``interactive`` profile
the MCP server serves to Claude Code (and its sub-agents), so the
candidates were un-actionable. These tests pin them into that profile.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

# The reflect agent's allow-list reflection tooth (assets/prism_reflect_agent.md).
REFLECT_JANITOR_TOOLS = (
    "janitor_check", "janitor_submit", "janitor_abandon", "memory_invalidate",
)


@pytest.fixture
def project(tmp_path):
    from prism_service import config as cfg
    from prism_service import project_context as pc

    original = cfg.PROJECTS_DIR
    cfg.PROJECTS_DIR = tmp_path / "projects"
    cfg.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    # get_project no longer creates on miss (d37193da):
    # seed the sandboxed project dir explicitly.
    cfg.project_data_dir("test-issue-173")
    pc._contexts.clear()
    yield "test-issue-173"
    cfg.PROJECTS_DIR = original
    pc._contexts.clear()


def _call(tool_name, arguments=None, project_id="test-issue-173"):
    from prism_service.mcp.tools import handle_tool
    return asyncio.run(handle_tool(tool_name, arguments or {}, project_id=project_id))


def _text(result):
    assert len(result) == 1
    return result[0].text


# AC-1: registered in TOOLS *and* in the reflect (interactive) profile.
def test_janitor_reflect_tools_registered_and_in_reflect_profile():
    from prism_service.mcp.tools import TOOLS, tool_names_for_profile

    tool_names = {t.name for t in TOOLS}
    interactive = tool_names_for_profile("interactive")
    for n in REFLECT_JANITOR_TOOLS:
        assert n in tool_names, f"{n} missing from TOOLS"
        assert n in interactive, (
            f"{n} not served in the interactive profile — prism-reflect "
            f"sub-agent cannot satisfy its allow-list (GH #173)"
        )


# AC-2: janitor_check dispatches through the real handler → dict with `ready`.
def test_janitor_check_dispatches_no_exception(project):
    chk = json.loads(_text(_call("janitor_check", {"session_id": "S-empty"})))
    assert isinstance(chk, dict)
    assert "ready" in chk
    assert chk["ready"] in (True, False)


# AC-3: janitor_submit dispatches and records a verdict for a seeded candidate.
def test_janitor_submit_records_verdict(project):
    from prism_service.project_context import get_project

    _call("janitor_enqueue", {
        "task_id": "T-173",
        "trigger": "task_done",
        "scope": {"task_ids": ["T-173"]},
    })
    ctx = get_project(project)
    ctx.janitor_svc._clock = lambda: datetime.now(timezone.utc) + timedelta(hours=2)

    chk = json.loads(_text(_call("janitor_check", {"session_id": "S-173"})))
    assert chk["ready"] is True
    cid = chk["brief"]["candidate_id"]

    res = json.loads(_text(_call("janitor_submit", {
        "candidate_id": cid,
        "output_json": {
            "qualitative_score": 0.6,
            "narrative": "Verdict recorded via MCP dispatch.",
            "new_memories": [],
            "invalidate_memory_ids": [],
            "confidence": 0.7,
        },
    })))
    assert res["accepted"] is True
