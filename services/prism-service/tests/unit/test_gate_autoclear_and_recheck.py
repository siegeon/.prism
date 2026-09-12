"""Ease-up fixes (2026-07-13): rubric gates auto-clear on a machine PASS,
and a FAILED gate recovers on an evidence-driven recheck — no override.

Pain under test (task 22ee4cb3 drive): a rubric-compliant story still
required a human click at story_gate; a failed gate's ONLY exit was
approve+override even after the evidence was fixed.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_STORY = """# Probe story

## Summary
Probe.

## Requirements
- R1. Probe requirement.

## Acceptance Criteria
- AC-1 — oracle: grep shows the probe marker.
- AC-2 — oracle: pytest -q passes the probe test.
"""


def _flow():
    from prism_service.api import conductor_flow
    return conductor_flow


def _task_at_draft_story(plan_doc: str = ""):
    """Fresh project + task advanced to draft_story, ready to report."""
    from prism_service.project_context import get_project
    project = "gac-" + uuid.uuid4().hex[:8]
    svc = get_project(project).conductor_svc
    task = svc._task_svc.create(title="gate autoclear probe")
    # task 3928b7ac (issue #222 continued): premise_grounded is now
    # unconditional on its own dedicated task.premise_notes field — seed it
    # once so this unrelated autoclear/recheck walk can leave
    # review_previous_notes.
    svc._task_svc.update(task.id, premise_notes=(
        "## Premises\n- fixture walk exercising gate autoclear, not a "
        "real premise claim - UNVERIFIED\n"))
    svc.advance_task(task.id, session_id="prep")      # -> review_previous_notes
    svc.advance_task(task.id, session_id="prep")      # -> draft_story
    if plan_doc:
        svc._task_svc.update(task.id, plan_doc=plan_doc)
    t = svc._task_svc.get(task.id)
    assert t.workflow_step == "draft_story", t.workflow_step
    return project, svc, task.id


def test_story_gate_autoclears_on_compliant_story():
    cf = _flow()
    project, svc, task_id = _task_at_draft_story(plan_doc=_STORY)
    res = cf.flow_report(
        cf.Ident(task_id=task_id, session_id="S1",
                 expected_step="draft_story", outcome="success"),
        project=project)
    t = svc._task_svc.get(task_id)
    assert t.workflow_step == "verify_plan", (t.workflow_step, t.gate_state, res)
    assert t.gate_state in ("", "none"), t.gate_state


def test_story_gate_rewinds_to_draft_story_when_rubric_fails():
    """SUPERSEDED 2026-09-12 by task 3feaf956 ("A gate never parks for a
    person while its own rubric fails"): a non-compliant story used to
    park the gate PENDING for a human to judge a design packet the
    machine already knew was incomplete (task 12029f92 reached plan_gate
    five times this way). It now REWINDS to draft_story with the scorer's
    own reason instead — the auto-clear must never approve it on the
    machine's behalf, and must never leave it for a human either. See
    test_gate_never_parks_on_failing_rubric.py for the full matrix."""
    cf = _flow()
    project, svc, task_id = _task_at_draft_story(plan_doc="")  # empty story
    cf.flow_report(
        cf.Ident(task_id=task_id, session_id="S1",
                 expected_step="draft_story", outcome="success"),
        project=project)
    t = svc._task_svc.get(task_id)
    assert t.workflow_step == "draft_story", t.workflow_step
    assert t.gate_state == "none", t.gate_state


def test_failed_gate_recovers_on_evidence_recheck_without_override():
    """SETUP adjusted 2026-09-12 for task 3feaf956: flow_report's own
    advance into story_gate now rewinds a genuinely rubric-failing story
    immediately (it never settles at story_gate/pending — see
    test_story_gate_rewinds_to_draft_story_when_rubric_fails above), so an
    empty-story task can no longer reach story_gate/pending through
    flow_report alone. This test's real subject is the manual reject ->
    evidence-recheck -> approve cycle at an ALREADY-parked gate — a
    different code path (gate_decide's own approve-time rubric check,
    unchanged by this ticket) — so it lands the task at story_gate/pending
    directly rather than relying on flow_report to produce that state."""
    cf = _flow()
    project, svc, task_id = _task_at_draft_story(plan_doc="")
    svc._task_svc.update(task_id, workflow_step="story_gate",
                         gate_state="pending")
    # Blind approve on the empty story flips the gate to failed (rubric).
    svc.gate_decide(task_id, "approve", reason="blind approve",
                    session_id="reviewer")
    assert svc._task_svc.get(task_id).gate_state == "failed"
    # Recheck with evidence still broken: refused, stays failed.
    refuse = svc.gate_decide(task_id, "approve", reason="retry, no fix",
                             session_id="reviewer")
    assert refuse["ok"] is False and "recheck" in refuse["reason"], refuse
    assert svc._task_svc.get(task_id).gate_state == "failed"
    # Fix the evidence, plain approve: released on merit, no override.
    svc._task_svc.update(task_id, plan_doc=_STORY)
    ok = svc.gate_decide(task_id, "approve",
                         reason="evidence fixed; rubric passes",
                         session_id="reviewer")
    assert ok["ok"] is True, ok
    assert svc._task_svc.get(task_id).workflow_step == "verify_plan"
