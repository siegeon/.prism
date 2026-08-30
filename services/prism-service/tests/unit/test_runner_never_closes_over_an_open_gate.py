"""The stall handler must not close a task whose gate is undecided.

OBSERVED LIVE on task 8fbd5cf0 (2026-08-30). The row ended as:

    status = done | workflow_step = implement_tasks | gate_state = pending
    gate_reason = "Rewind 1/3: green receipt FAILED ... customer_observable_holds"

i.e. auto-closed as DONE while its green_gate had never been decided.

WHY THE EXISTING GUARD MISSED IT. TaskService refuses status=done over an
open gate, but that check is `is_open_gate_step(workflow_step, gate_state)`,
which requires the workflow_step ITSELF to be a gate. A rewind moves the task
back to an AGENT step (implement_tasks) and leaves gate_state='pending'
behind, so the row carries an open gate on a non-gate step -- invisible to
the guard. task_runner's stall handler then called task_svc.update directly,
past the route-level check entirely.

Shipped-ness is not a verdict. A branch reaching origin/main says the work
exists, never that anybody adjudicated it.
"""

from __future__ import annotations

import uuid


def _svc(tmp_path):
    from prism_service.services.task_service import TaskService
    return TaskService(str(tmp_path / f"tasks-{uuid.uuid4().hex[:8]}.db"))


def test_a_shipped_task_with_a_pending_gate_is_not_closed(tmp_path, monkeypatch):
    from prism_service.services import task_runner as tr

    svc = _svc(tmp_path)
    t = svc.create(title="shipped, but its gate never got decided")
    # EXACTLY the shape a rewind leaves behind: an AGENT step carrying a
    # gate_state the guard's is_open_gate_step() cannot see.
    svc.update(t.id, status="in_progress", workflow_step="implement_tasks",
               gate_state="pending")
    monkeypatch.setattr(tr, "_stall_work_is_shipped", lambda _tid: True)
    monkeypatch.setattr(tr, "_shipped_sha_for_stall",
                        lambda _tid: "6189234b4f167c27feb57f304a65eb22d55de035")

    res = tr._handle_stall(svc, t.id, "implement_tasks")

    after = svc.get(t.id)
    assert after.status != "done", (
        "a task whose gate is still pending must NOT be auto-closed; shipped "
        f"is not adjudicated -- got status={after.status!r}")
    assert res.get("ok") is False, f"the handler must report a refusal; got {res!r}"
    assert "undecided" in str(res.get("reason", "")), (
        f"the refusal must name WHY, so a driver can self-diagnose; got {res!r}")


def test_a_shipped_task_with_a_settled_gate_still_closes(tmp_path, monkeypatch):
    """THE GUARD ON THE GUARD. The original behaviour must survive: a task
    whose work is on main and whose gate is settled still closes, rather
    than being split into children that cannot change anything."""
    from prism_service.services import task_runner as tr

    svc = _svc(tmp_path)
    t = svc.create(title="shipped and adjudicated")
    svc.update(t.id, status="in_progress", workflow_step="implement_tasks",
               gate_state="none")
    monkeypatch.setattr(tr, "_stall_work_is_shipped", lambda _tid: True)
    monkeypatch.setattr(tr, "_shipped_sha_for_stall",
                        lambda _tid: "6189234b4f167c27feb57f304a65eb22d55de035")

    res = tr._handle_stall(svc, t.id, "implement_tasks")

    assert svc.get(t.id).status == "done", (
        "a shipped task with no open gate must still close -- the fix must "
        "not strand the case the shipped-close path exists for")
    assert res.get("ok") is True, res
