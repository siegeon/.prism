"""Soundness of the inverted-queue transition contract (inverted-flow #1).

The report/advance handler used to discard the reported outcome and
UNCONDITIONALLY advance — a reported FAILURE still advanced the step, a
late/duplicate report advanced whatever step was now current, and a report
with no session identity was accepted (making distinct-actor gate
enforcement untrustworthy). And flow_start created an EMPTY stub git repo
(and fell back to the shared branch on failure) instead of a real worktree
of the PRISM product source.

These tests drive the REAL flow path (api.conductor_flow over a real
ConductorService/TaskService) and the REAL task_workspace helper.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _flow():
    from prism_service.api import conductor_flow
    return conductor_flow


def _fresh_task_on_first_step():
    """A brand-new project + task entered into the flow, sitting on the
    first (agent) step review_previous_notes. Returns (project, svc, task_id,
    first_step_id)."""
    from prism_service.project_context import get_project

    project = "qtc-" + uuid.uuid4().hex[:8]
    svc = get_project(project).conductor_svc
    task = svc._task_svc.create(title="queue transition contract probe")
    # Enter the flow exactly as flow_start would, WITHOUT touching the real
    # git worktree machinery (that is exercised separately below).
    svc.advance_task(task.id, session_id="prep")
    task = svc._task_svc.get(task.id)
    assert task.workflow_step == "review_previous_notes"
    # task 3928b7ac (issue #222 continued): premise_grounded is now
    # unconditional on its own dedicated task.premise_notes field — seed it
    # once so a "success"/"pass" report on this queue-transition probe
    # (unrelated to premise content) can leave review_previous_notes.
    svc._task_svc.update(task.id, premise_notes=(
        "## Premises\n- fixture probe exercising queue transition "
        "contracts, not a real premise claim - UNVERIFIED\n"))
    return project, svc, task.id, "review_previous_notes"


# ---------------------------------------------------------------------------
# (1) OUTCOME-AWARE: a reported FAILURE does NOT advance the step.
# ---------------------------------------------------------------------------

def test_reported_failure_does_not_advance():
    cf = _flow()
    project, svc, task_id, step = _fresh_task_on_first_step()

    res = cf.flow_report(
        cf.Ident(task_id=task_id, session_id="S1", expected_step=step,
                 outcome="failure"),
        project=project)

    assert res["ok"] is False, res
    assert res.get("advanced") is False, res
    still = svc._task_svc.get(task_id)
    assert still.workflow_step == step, "a reported failure must NOT advance"


def test_structured_failure_outcome_does_not_advance():
    cf = _flow()
    project, svc, task_id, step = _fresh_task_on_first_step()

    res = cf.flow_report(
        cf.Ident(task_id=task_id, session_id="S1", expected_step=step,
                 outcome={"ok": False, "detail": "pytest exploded"}),
        project=project)

    assert res["ok"] is False, res
    assert svc._task_svc.get(task_id).workflow_step == step


# ---------------------------------------------------------------------------
# (2) IDEMPOTENT + STALE-SAFE
# ---------------------------------------------------------------------------

def test_stale_expected_step_is_noop():
    cf = _flow()
    project, svc, task_id, step = _fresh_task_on_first_step()

    res = cf.flow_report(
        cf.Ident(task_id=task_id, session_id="S1",
                 expected_step="some_earlier_step", outcome="success"),
        project=project)

    assert res.get("ok") is False and res.get("noop") is True, res
    assert svc._task_svc.get(task_id).workflow_step == step, \
        "a report for the wrong step must not advance the current step"


def test_missing_expected_step_is_rejected():
    cf = _flow()
    project, svc, task_id, step = _fresh_task_on_first_step()

    res = cf.flow_report(
        cf.Ident(task_id=task_id, session_id="S1", outcome="success"),
        project=project)

    assert res["ok"] is False, res
    assert "expected_step" in (res.get("error") or ""), res
    assert svc._task_svc.get(task_id).workflow_step == step


def test_duplicate_success_report_is_idempotent():
    cf = _flow()
    project, svc, task_id, step = _fresh_task_on_first_step()

    first = cf.flow_report(
        cf.Ident(task_id=task_id, session_id="S1", expected_step=step,
                 outcome="success"),
        project=project)
    assert first["ok"] is True, first
    advanced_to = svc._task_svc.get(task_id).workflow_step
    assert advanced_to != step, "first success must advance exactly once"

    # The worker (or a retry) fires the SAME report again — same expected_step.
    second = cf.flow_report(
        cf.Ident(task_id=task_id, session_id="S1", expected_step=step,
                 outcome="success"),
        project=project)
    assert second.get("ok") is False and second.get("noop") is True, second
    assert svc._task_svc.get(task_id).workflow_step == advanced_to, \
        "a duplicate report must not advance a second time"


# ---------------------------------------------------------------------------
# (3) SESSION IDENTITY REQUIRED
# ---------------------------------------------------------------------------

def test_report_without_session_is_rejected():
    cf = _flow()
    project, svc, task_id, step = _fresh_task_on_first_step()

    res = cf.flow_report(
        cf.Ident(task_id=task_id, expected_step=step, outcome="success"),
        project=project)

    assert res["ok"] is False, res
    assert "session" in (res.get("error") or "").lower(), res
    assert svc._task_svc.get(task_id).workflow_step == step, \
        "a session-less report must not advance"


def test_blank_session_is_rejected():
    cf = _flow()
    project, svc, task_id, step = _fresh_task_on_first_step()

    res = cf.flow_report(
        cf.Ident(task_id=task_id, session_id="   ", expected_step=step,
                 outcome="success"),
        project=project)
    assert res["ok"] is False, res
    assert svc._task_svc.get(task_id).workflow_step == step


# ---------------------------------------------------------------------------
# (4) REAL WORKTREE + FAIL CLOSED
# ---------------------------------------------------------------------------

def test_workspace_is_a_real_prism_worktree():
    from prism_service.services import task_workspace

    task_id = "wt-" + uuid.uuid4().hex[:10]
    try:
        rec = task_workspace.ensure_workspace(task_id)
        ws = Path(rec["path"])
        assert ws.exists(), rec

        # It is a real CHECKOUT of the PRISM product source — the service
        # package and its pyproject are present, NOT the old empty stub.
        pkg_init = ws / "services" / "prism-service" / "prism_service" / "__init__.py"
        pyproject = ws / "services" / "prism-service" / "pyproject.toml"
        assert pkg_init.exists(), f"real prism_service package missing: {ws}"
        assert pyproject.exists(), f"real pyproject missing: {ws}"
        assert "prism-service" in pyproject.read_text(encoding="utf-8"), \
            "worktree pyproject is not the real PRISM project"

        # And NOT the discarded stub (a top-level pyproject named 'task').
        stub = ws / "pyproject.toml"
        if stub.exists():
            assert "name = 'task'" not in stub.read_text(encoding="utf-8")

        # It is a genuine git worktree (has HEAD wired to the parent repo).
        assert rec.get("baseline"), rec
        assert (ws / ".git").exists(), "a real worktree carries a .git link"
    finally:
        task_workspace.remove_workspace(task_id)


def test_workspace_fails_closed_when_no_real_repo(tmp_path):
    from prism_service.services import task_workspace

    not_a_repo = tmp_path / "empty"
    not_a_repo.mkdir()
    task_id = "wt-" + uuid.uuid4().hex[:10]

    with pytest.raises(Exception):
        task_workspace.ensure_workspace(task_id, repo_root=str(not_a_repo))
    # Nothing was recorded — no silent fallback workspace.
    assert task_workspace.workspace_for(task_id) is None


def test_flow_start_fails_closed_when_workspace_unavailable(monkeypatch):
    cf = _flow()
    from prism_service.project_context import get_project
    from prism_service.services import task_workspace

    project = "qtc-" + uuid.uuid4().hex[:8]
    svc = get_project(project).conductor_svc
    task = svc._task_svc.create(title="fail-closed start")

    def _boom(_task_id):
        raise RuntimeError("no real worktree available")

    monkeypatch.setattr(task_workspace, "ensure_workspace", _boom)

    res = cf.flow_start(cf.Ident(task_id=task.id, session_id="S1"),
                        project=project)

    assert res["ok"] is False, res
    assert "fail closed" in (res.get("error") or "").lower(), res
    # The task must NOT have entered the flow on a shared branch.
    assert svc._task_svc.get(task.id).workflow_step in ("", None), \
        "flow_start must not advance the task when the worktree is unavailable"
