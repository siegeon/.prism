"""Task 811fcce0 follow-on: conductor_work's REPORT branch must resolve the
CURRENT step against the task's OWN workflow, not always "implement".

Found live while driving 811fcce0 through the new "quickfix" workflow via
the conductor_work MCP tool (see models/workflow.py QUICKFIX_STEPS):
mcp/tools.py's `_dispatch_tool` REPORT branch called
`ConductorService._step_by_id(step_id)` with NO `workflow` argument, so it
always defaulted to "implement" (`_step_by_id`'s own default). A
non-implement task's own step id ("intake", "classify", "apply_fix", ...)
never matches any of the 10 implement step ids, so `_cur` was always
`None` and every REPORT silently fell into the "task had not entered the
flow; started it" restart branch instead of ever advancing past its first
step -- a task on any of triage/align_language/promote_to_law/quickfix
could START but never actually be DRIVEN through this tool.

Pins the fix: reporting against a "triage" task's `classify` step
advances it to `decide`, and NEVER re-enters at `intake` (the symptom
this defect produced) nor returns the restart-branch "note" field.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_PID = "test-conductor-work-report-workflow"


@pytest.fixture
def project(tmp_path, monkeypatch):
    from prism_service import config as cfg
    original = cfg.PROJECTS_DIR
    cfg.PROJECTS_DIR = tmp_path / "projects"
    cfg.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    from prism_service import project_context as pc
    pc._contexts.clear()

    # ensure_workspace() would clone a REAL git worktree of this repo --
    # unrelated to the step-resolution defect this test pins, and slow.
    # Stub it the same way test_ship_worker.py's _wire_ws bypasses the
    # real worktree for its own, unrelated seam.
    from prism_service.services import task_workspace as tw
    monkeypatch.setattr(
        tw, "ensure_workspace",
        lambda task_id, repo_root=None, base_ref=None: {
            "task_id": task_id, "path": "/tmp/fake-" + task_id,
            "baseline": "deadbeef", "branch": f"prism/ws/{task_id}",
            "repo_root": "/tmp/fake-" + task_id})

    yield _PID
    cfg.PROJECTS_DIR = original
    pc._contexts.clear()


def _call(tool_name, arguments, project_id=_PID):
    from prism_service.mcp.tools import handle_tool
    return asyncio.run(
        handle_tool(tool_name, arguments, project_id=project_id))


def _body(result):
    assert len(result) == 1
    return json.loads(result[0].text)


def test_report_on_a_triage_task_advances_past_its_first_step(project):
    from prism_service.project_context import get_project

    ctx = get_project(project)
    t = ctx.task_svc.create(title="A triaged inbox item")
    ctx.task_svc.update(t.id, workflow="triage")

    session_id = "test-driver-session"
    started = _body(_call(
        "conductor_work", {"id": t.id, "session_id": session_id}, project))
    assert started["ok"] is True, started
    assert started["job"]["step"] == "intake", started

    # REPORT intake -> classify.
    reported = _body(_call(
        "conductor_work",
        {"id": t.id, "session_id": session_id, "outcome": "pass",
         "proof": "registered"}, project))
    assert "note" not in reported, (
        "a REPORT must advance, not fall back to the restart branch -- "
        f"got: {reported}")
    assert reported["ok"] is True, reported
    assert reported["job"]["step"] == "classify", reported

    # REPORT classify -> decide. "classify" is the step this defect made
    # unreachable: it is not any implement-workflow step id, so
    # _step_by_id(step_id) with no workflow arg always returned None here.
    reported2 = _body(_call(
        "conductor_work",
        {"id": t.id, "session_id": session_id, "outcome": "pass",
         "proof": "bucket=Open, needs a reply from support"}, project))
    assert "note" not in reported2, reported2
    assert reported2["ok"] is True, reported2
    assert reported2["job"]["step"] == "decide", reported2


def test_a_quickfix_task_reaches_done_true_and_status_done(project):
    """Companion defect in the SAME code block: `_terminal()` hardcoded
    "implement"'s own last step id (green_gate), so a quickfix/triage/
    align_language/promote_to_law task reaching its OWN last step ("done",
    never a gate) was never recognized as terminal -- conductor_work never
    returned done=true and never flipped task.status to "done". Drives a
    "quickfix" task (no gates at all) all the way through via
    conductor_work and confirms both."""
    from prism_service.project_context import get_project

    ctx = get_project(project)
    t = ctx.task_svc.create(title="A small, already-diagnosed fix")
    ctx.task_svc.update(t.id, workflow="quickfix")

    session_id = "test-driver-session"
    started = _body(_call(
        "conductor_work", {"id": t.id, "session_id": session_id}, project))
    assert started["job"]["step"] == "intake", started

    reported = _body(_call(
        "conductor_work",
        {"id": t.id, "session_id": session_id, "outcome": "pass",
         "proof": "registered"}, project))
    assert reported["job"]["step"] == "apply_fix", reported
    assert reported["done"] is False, reported

    reported2 = _body(_call(
        "conductor_work",
        {"id": t.id, "session_id": session_id, "outcome": "pass",
         "proof": "made the exact change the oracle describes; pinned "
                  "test passes"}, project))
    assert reported2["job"]["step"] == "verify_fix", reported2
    assert reported2["done"] is False, reported2

    reported3 = _body(_call(
        "conductor_work",
        {"id": t.id, "session_id": session_id, "outcome": "pass",
         "proof": "full pinned suite green; committed and pushed"},
        project))
    assert reported3["ok"] is True, reported3
    assert reported3["done"] is True, (
        "reaching the workflow's own terminal 'done' step must report "
        f"done=true -- got: {reported3}")

    refreshed = ctx.task_svc.get(t.id)
    assert refreshed.workflow_step == "done", refreshed
    assert refreshed.status == "done", (
        "conductor_work must flip a terminal quickfix task's status to "
        f"'done' on its own -- got status={refreshed.status!r}")
