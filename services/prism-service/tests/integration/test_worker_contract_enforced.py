"""Worker contract enforcement at the conductor's report boundary (task
fb2846dc, slice 3 of epic cfa6300b). PRISM tasks carry a worker contract
(allowed_files/verify/stop_if, models/task.py:69-76) that used to be stored
and displayed but never enforced: conductor_flow._job() never surfaced it
structurally, and nothing checked a completed job against it. This is the
oracle for closing that gap:

  AC-1  the implement_tasks job payload carries job["contract"] as a
        STRUCTURED dict (allowed_files/verify/stop_if), not prose.
  AC-2  a dev-step (implement_tasks) PASS report whose REAL workspace diff
        touches a file OUTSIDE a non-empty allowed_files is refused, naming
        the offending path(s); the step does not advance.
  AC-3  re-sending the SAME report with acknowledged_out_of_contract naming
        every offender is accepted, advances, and is recorded in history.
  AC-4  an empty allowed_files stays unconstrained (today's behavior).
  AC-5  the same out-of-contract edit at a NON-dev step is unaffected.
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


@pytest.fixture()
def make_task():
    """A task under a fresh throwaway project. Tracks task ids so their
    REAL per-task git workspace (task_workspace's canonical resolution,
    created lazily by flow_start) is torn down after the test."""
    from prism_service.project_context import get_project
    from prism_service.services import task_workspace as tw

    created: list[str] = []

    def _make(**kwargs):
        project = "wce-" + uuid.uuid4().hex[:8]
        ctx = get_project(project)
        title = kwargs.pop("title", "contract task")
        task = ctx.task_svc.create(title=title, **kwargs)
        created.append(task.id)
        return ctx, task, project

    yield _make

    for task_id in created:
        tw.remove_workspace(task_id)


def _drive_to_step(cf, task_svc, task_id, project, target_step,
                   builder="builder"):
    """Walk the SDLC from flow_start up to (and stopping AT, unreported)
    target_step. Non-gate steps are reported by `builder`; gates by a fresh
    reviewer session each time with override=True (mirrors the proven walk
    in test_conductor_work_honest_green.py) — this test cares about the
    implement_tasks/write_failing_tests report boundary, not rubric/verifier
    fidelity on the steps ahead of it."""
    start = cf.flow_start(cf.Ident(task_id=task_id, session_id=builder),
                          project=project)
    assert start["ok"] is True, start
    seat = 0
    guard = 40
    while True:
        task = task_svc.get(task_id)
        if task.workflow_step == target_step:
            return
        assert guard > 0, "drive did not reach target step " + target_step
        guard -= 1
        job = cf.flow_next(task_id, project=project)["job"]
        assert job is not None, "no job on deck but target step not reached"
        step, kind = job["step"], job["kind"]
        if kind != "gate":
            rep = cf.flow_report(cf.Ident(
                task_id=task_id, session_id=builder, expected_step=step,
                outcome="pass"), project=project)
            assert rep.get("ok") is not False, rep
            continue
        seat += 1
        # red_gate's proof-carrying-artifact tooth is NOT skipped by
        # override — it needs a completion_proof shaped like a committed
        # failing-test trace (test runner + a failure signal) regardless.
        gate_reason = ("red trace observed: pytest tests/x.py -> 1 failed "
                       "(committed failing test)") if step == "red_gate" \
            else "reviewed and accepted by an independent steward"
        task_svc.update(task_id, completion_proof=gate_reason)
        rep = cf.flow_report(cf.Ident(
            task_id=task_id, session_id=f"reviewer-{seat}",
            expected_step=step, outcome=gate_reason, override=True),
            project=project)
        assert rep.get("ok") is not False, rep


def _touch(root: Path, rel: str, content: str = "x") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# AC-1 — structured contract on the job payload
# ---------------------------------------------------------------------------

def test_job_carries_structured_contract(make_task):
    from prism_service.api import conductor_flow as cf

    ctx, task, project = make_task(
        allowed_files=["a/allowed.py"], verify=["pytest tests/x.py"],
        stop_if=["need files outside allowed_files"])
    start = cf.flow_start(cf.Ident(task_id=task.id, session_id="builder"),
                          project=project)
    assert start["ok"] is True, start
    contract = start["job"]["contract"]
    assert contract == {
        "allowed_files": ["a/allowed.py"],
        "verify": ["pytest tests/x.py"],
        "stop_if": ["need files outside allowed_files"],
    }, contract


# ---------------------------------------------------------------------------
# AC-2 / AC-3 — out-of-contract refusal + audited acknowledgment
# ---------------------------------------------------------------------------

def test_out_of_contract_report_refused_then_acknowledged(make_task):
    from prism_service.api import conductor_flow as cf
    from prism_service.services import task_workspace as tw

    ctx, task, project = make_task(allowed_files=["a/allowed.py"])
    _drive_to_step(cf, ctx.task_svc, task.id, project, "implement_tasks")
    root = Path(tw.workspace_for(task.id)["path"])
    _touch(root, "b/rogue.py")

    refused = cf.flow_report(cf.Ident(
        task_id=task.id, session_id="builder", expected_step="implement_tasks",
        outcome="pass"), project=project)
    assert refused["ok"] is False, refused
    assert refused["offending_files"] == ["b/rogue.py"], refused
    assert "worker contract violated" in refused["reason"], refused
    still = ctx.task_svc.get(task.id)
    assert still.workflow_step == "implement_tasks", still.workflow_step

    accepted = cf.flow_report(cf.Ident(
        task_id=task.id, session_id="builder", expected_step="implement_tasks",
        outcome="pass",
        acknowledged_out_of_contract=["b/rogue.py"]), project=project)
    assert accepted.get("ok") is not False, accepted
    advanced = ctx.task_svc.get(task.id)
    assert advanced.workflow_step != "implement_tasks", advanced.workflow_step
    hist = ctx.task_svc.history(task.id)
    ack_rows = [h for h in hist if h.action == "worker_contract_ack"]
    assert ack_rows and "b/rogue.py" in ack_rows[-1].details, hist


def test_allowlisted_directory_prefix_is_not_an_offender(make_task):
    """allowed_files entries also cover everything UNDER a listed directory
    (the plan's prefix-match rule), not just an exact file path."""
    from prism_service.api import conductor_flow as cf
    from prism_service.services import task_workspace as tw

    ctx, task, project = make_task(allowed_files=["a/"])
    _drive_to_step(cf, ctx.task_svc, task.id, project, "implement_tasks")
    root = Path(tw.workspace_for(task.id)["path"])
    _touch(root, "a/nested/inside.py")

    rep = cf.flow_report(cf.Ident(
        task_id=task.id, session_id="builder", expected_step="implement_tasks",
        outcome="pass"), project=project)
    assert rep.get("ok") is not False, rep
    assert ctx.task_svc.get(task.id).workflow_step != "implement_tasks"


# ---------------------------------------------------------------------------
# AC-4 — empty allowed_files stays unconstrained
# ---------------------------------------------------------------------------

def test_empty_allowlist_unconstrained(make_task):
    from prism_service.api import conductor_flow as cf
    from prism_service.services import task_workspace as tw

    ctx, task, project = make_task(allowed_files=[])
    _drive_to_step(cf, ctx.task_svc, task.id, project, "implement_tasks")
    root = Path(tw.workspace_for(task.id)["path"])
    _touch(root, "anywhere/at/all.py")

    rep = cf.flow_report(cf.Ident(
        task_id=task.id, session_id="builder", expected_step="implement_tasks",
        outcome="pass"), project=project)
    assert rep.get("ok") is not False, rep
    assert ctx.task_svc.get(task.id).workflow_step != "implement_tasks"


# ---------------------------------------------------------------------------
# AC-5 — a NON-dev step's report is never subject to this check
# ---------------------------------------------------------------------------

def test_non_dev_step_unaffected_by_out_of_contract_change(make_task):
    from prism_service.api import conductor_flow as cf
    from prism_service.services import task_workspace as tw

    ctx, task, project = make_task(allowed_files=["a/allowed.py"])
    _drive_to_step(cf, ctx.task_svc, task.id, project, "write_failing_tests")
    root = Path(tw.workspace_for(task.id)["path"])
    _touch(root, "b/rogue.py")

    rep = cf.flow_report(cf.Ident(
        task_id=task.id, session_id="builder",
        expected_step="write_failing_tests", outcome="pass"), project=project)
    assert rep.get("ok") is not False, rep
    assert ctx.task_svc.get(task.id).workflow_step != "write_failing_tests"


# ---------------------------------------------------------------------------
# stop_if — an unresolvable workspace root refuses distinctly; never a
# silent fall-back to the daemon cwd.
# ---------------------------------------------------------------------------

def test_unresolvable_workspace_refuses_distinctly(make_task):
    from prism_service.api import conductor_flow as cf

    ctx, task, project = make_task(allowed_files=["a/allowed.py"])
    # Hand-place the task directly on implement_tasks with NO workspace ever
    # created (flow_start, the only place that calls ensure_workspace, was
    # never invoked) — the structural gap this guards against.
    ctx.task_svc.update(task.id, workflow_step="implement_tasks",
                        gate_state="none")

    rep = cf.flow_report(cf.Ident(
        task_id=task.id, session_id="builder", expected_step="implement_tasks",
        outcome="pass"), project=project)
    assert rep["ok"] is False, rep
    assert "workspace root" in rep["reason"], rep
    assert "offending_files" not in rep, rep
