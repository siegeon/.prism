"""A finished step hands off; nothing scans for the next one (task cdb8e365).

Owner direction 2026-08-30: "The whole point of having these bots is that they
work a task. When they are done with the task, they add it to the next flow.
The next flow picks it up ... there is no reason to have a system like this
where we have background processes trying way to do stuff. That gets
confusing." And: "The task needs to be able to complete on its own."

BOTH HANDOFFS ARE ALREADY COMPUTED AND THEN DROPPED.

Step to step: `ConductorService.advance_task` says it in its own docstring --
"Move a task to the next entry in WORKFLOW_STEPS". It computes the next step,
writes it to the row, and returns without telling anyone.

Task to task: `TaskService.next_task` filters pending tasks by
`all(dep in done_ids)`, so `dependencies` is a filter on a PULL that nothing
performs on completion. `task_runner.eligible_task` scans only in_progress
tasks, so a pending dependent never starts on its own at all. Measured on
epic 4c9b39e5: three slices carried real dependency edges and an operator
hand-launched every one.

These tests pin the dispatcher: it fires, it fans out to N, it never fires a
dependent whose other edges are open, and one item reaches one consumer.
"""

from __future__ import annotations

import pytest

from prism_service.services import dispatch


class _Task:
    def __init__(self, tid, status="pending", deps=None, step="", parent=""):
        self.id = tid
        self.status = status
        self.dependencies = deps or []
        self.workflow_step = step
        self.parent_id = parent


class _Svc:
    def __init__(self, rows):
        self._rows = {t.id: t for t in rows}

    def get(self, tid):
        return self._rows.get(tid)

    def list(self, status="", parent_id=None, **_):
        out = list(self._rows.values())
        if status:
            out = [t for t in out if t.status == status]
        if parent_id is not None:
            out = [t for t in out if t.parent_id == parent_id]
        return out


# ----------------------------------------------------------------------
# Task to task: a finished task starts what it unblocked
# ----------------------------------------------------------------------

def test_a_finished_task_starts_its_unblocked_dependents():
    """The dependent must be named WITHOUT anyone scanning for it."""
    svc = _Svc([
        _Task("a", status="done"),
        _Task("b", status="pending", deps=["a"]),
    ])

    assert dispatch.unblocked_by("a", svc) == ["b"]


def test_a_dependent_with_an_open_edge_does_not_start():
    """Closing ONE edge is not enough.

    A naive "a dependency closed, therefore start it" fires a task whose
    other edges are still open. Slice 0ee4dc98 depended on one slice while
    another was still running -- exactly this shape.
    """
    svc = _Svc([
        _Task("a", status="done"),
        _Task("open", status="in_progress"),
        _Task("b", status="pending", deps=["a", "open"]),
    ])

    assert dispatch.unblocked_by("a", svc) == []


def test_a_finished_task_can_unblock_several_at_once():
    """Fan-out width is however many the completion actually freed.

    A slice exists to run N claude -p instances at once; the dispatcher must
    hand out all of them, not the first.
    """
    svc = _Svc([
        _Task("a", status="done"),
        _Task("b", status="pending", deps=["a"]),
        _Task("c", status="pending", deps=["a"]),
        _Task("d", status="pending", deps=["a"]),
    ])

    assert sorted(dispatch.unblocked_by("a", svc)) == ["b", "c", "d"]


def test_a_task_that_is_not_done_unblocks_nobody():
    svc = _Svc([
        _Task("a", status="in_progress"),
        _Task("b", status="pending", deps=["a"]),
    ])

    assert dispatch.unblocked_by("a", svc) == []


def test_an_already_running_dependent_is_not_started_again():
    """Only a PENDING dependent is a candidate; re-firing a live one is the
    two-drivers-one-worktree incident under a new name."""
    svc = _Svc([
        _Task("a", status="done"),
        _Task("b", status="in_progress", deps=["a"]),
    ])

    assert dispatch.unblocked_by("a", svc) == []


# ----------------------------------------------------------------------
# The fan-in: an epic assembles when its last child lands
# ----------------------------------------------------------------------

def test_the_last_child_landing_names_the_parent_for_assembly():
    """Sewing back together is a real trigger, not a person noticing.

    A parent is never satisfied by green children -- epic 0784729f shipped
    13 of them with a feature that could not run -- so the parent must be
    handed back for its own oracle when the last child completes.
    """
    svc = _Svc([
        _Task("epic", status="in_progress"),
        _Task("c1", status="done", parent="epic"),
        _Task("c2", status="done", parent="epic"),
    ])

    assert dispatch.parent_ready_to_assemble("c2", svc) == "epic"


def test_a_parent_with_an_unfinished_child_does_not_assemble():
    svc = _Svc([
        _Task("epic", status="in_progress"),
        _Task("c1", status="done", parent="epic"),
        _Task("c2", status="in_progress", parent="epic"),
    ])

    assert dispatch.parent_ready_to_assemble("c1", svc) is None


def test_a_childless_task_has_no_parent_to_assemble():
    svc = _Svc([_Task("solo", status="done")])

    assert dispatch.parent_ready_to_assemble("solo", svc) is None


# ----------------------------------------------------------------------
# One item, one consumer
# ----------------------------------------------------------------------

def test_one_item_reaches_one_consumer(tmp_path):
    """The handoff must not hand the same task to two drivers.

    That is the 2026-08-30 incident: two seats in one worktree, a test file
    overwritten mid-write. The lease is what makes a handoff safe to fire
    more than once.
    """
    from prism_service.services.claim_service import ClaimService

    svc = ClaimService(db_path=str(tmp_path / "c.db"))
    first = svc.acquire("t", holder_id="consumer-1", ttl_s=60)
    second = svc.acquire("t", holder_id="consumer-2", ttl_s=60)

    assert first and second is None


# ----------------------------------------------------------------------
# The wiring: a helper nobody calls is this project's recurring defect
# ----------------------------------------------------------------------

def test_the_report_path_calls_the_dispatcher():
    """Pin the CALL SITE, not only the helper.

    premise_gather, align_language, ClaimService and the memory indexer all
    shipped fully built with no production caller. A dispatcher nobody calls
    would leave every handoff exactly as dropped as it is today.
    """
    import inspect

    from prism_service.api import conductor_flow

    src = inspect.getsource(conductor_flow)
    assert "dispatch" in src, (
        "conductor_flow must hand off after it advances a step")


def test_the_dispatcher_makes_no_model_call():
    """Orchestration is codified. It costs no tokens."""
    import inspect

    src = inspect.getsource(dispatch)
    assert "claude_cli" not in src and "inference" not in src, (
        "the dispatcher must not reach a model -- the handoff is the phase "
        "this exists to make free")


# ----------------------------------------------------------------------
# Step to step: the advance must WAKE a driver, not just return
# ----------------------------------------------------------------------

def test_a_step_completion_enqueues_the_next_step(monkeypatch):
    """An advance must hand the task to a driver NOW.

    The first cut of this dispatcher handled task COMPLETION and returned
    quietly for an ordinary step advance -- so the task still sat until
    task_runner's 900s tick found it. That is the polling latency the whole
    handoff exists to remove: the next owner is already known the instant
    the step advances.
    """
    woken = []
    monkeypatch.setattr(dispatch, "_drive_now",
                        lambda tid, project: woken.append(tid))

    svc = _Svc([_Task("t", status="in_progress", step="implement_tasks")])
    monkeypatch.setattr(dispatch, "_task_svc_for", lambda project: svc)

    dispatch.after_step("t", "proj")

    assert woken == ["t"], (
        "an in-progress task on an agent step must be driven immediately, "
        "not left for the next sweep")


def test_a_task_parked_on_a_gate_is_not_driven():
    """A gate belongs to a distinct seat, never to the driver that just
    reported. Waking the runner on a gate step would hand a task back to
    its own producer."""
    assert not dispatch._is_agent_step("green_gate")
    assert dispatch._is_agent_step("implement_tasks")


def test_a_finished_task_is_never_driven_again(monkeypatch):
    woken = []
    monkeypatch.setattr(dispatch, "_drive_now",
                        lambda tid, project: woken.append(tid))
    svc = _Svc([_Task("t", status="done", step="green_gate")])
    monkeypatch.setattr(dispatch, "_task_svc_for", lambda project: svc)

    dispatch.after_step("t", "proj")

    assert woken == []


# ----------------------------------------------------------------------
# The START: a task becoming startable must be driven
# ----------------------------------------------------------------------

def test_a_task_that_becomes_in_progress_is_driven(monkeypatch):
    """The chain had no beginning.

    LIVE, 2026-08-30: a task was marked ready and sat `pending` for ten
    minutes with nothing touching it. step-to-step and task-to-task were
    both wired, but NOTHING fired when a task first became startable, so
    the handoff chain had no first link and the task waited for a sweep
    that only scans tasks already in_progress.
    """
    woken = []
    monkeypatch.setattr(dispatch, "_drive_now",
                        lambda tid, project: woken.append(tid))
    svc = _Svc([_Task("t", status="in_progress", step="")])
    monkeypatch.setattr(dispatch, "_task_svc_for", lambda project: svc)

    dispatch.on_started("t", "proj")

    assert woken == ["t"], (
        "a task that just became in_progress must be driven, not left for "
        "the next sweep")


def test_a_still_pending_task_is_not_driven(monkeypatch):
    woken = []
    monkeypatch.setattr(dispatch, "_drive_now",
                        lambda tid, project: woken.append(tid))
    svc = _Svc([_Task("t", status="pending")])
    monkeypatch.setattr(dispatch, "_task_svc_for", lambda project: svc)

    dispatch.on_started("t", "proj")

    assert woken == []


def test_the_update_route_starts_the_task():
    """Pin the CALL SITE. A start trigger nobody calls is the same hole."""
    import inspect

    from prism_service.api import tasks as tasks_api

    src = inspect.getsource(tasks_api.update_task)
    assert "dispatch" in src, (
        "the update route must hand a newly-started task to a driver")


# ----------------------------------------------------------------------
# A non-advancing report must NOT be re-driven
# ----------------------------------------------------------------------

def test_a_report_that_did_not_advance_is_not_re_driven(monkeypatch):
    """RUNAWAY, 2026-08-31. 7.13.221 fired the handoff on every report,
    advanced or not. A step whose report FAILED validation therefore got
    re-driven instantly, and a slow retry became a hot loop: premise-gather
    ran 7 times on one step with 3 concurrent claude -p processes before an
    operator halted it.

    A non-advance is a signal to back off. The interval reconciler will
    retry it on its own schedule, which is what that schedule is for.
    """
    woken = []
    monkeypatch.setattr(dispatch, "_drive_now",
                        lambda tid, project: woken.append(tid))
    svc = _Svc([_Task("t", status="in_progress", step="review_previous_notes")])
    monkeypatch.setattr(dispatch, "_task_svc_for", lambda project: svc)

    dispatch.after_step("t", "proj", advanced=False)

    assert woken == [], (
        "a report that did not advance the step must not re-drive it -- "
        "that turns a failing step into a hot loop")


def test_an_advancing_report_still_drives(monkeypatch):
    woken = []
    monkeypatch.setattr(dispatch, "_drive_now",
                        lambda tid, project: woken.append(tid))
    svc = _Svc([_Task("t", status="in_progress", step="implement_tasks")])
    monkeypatch.setattr(dispatch, "_task_svc_for", lambda project: svc)

    dispatch.after_step("t", "proj", advanced=True)

    assert woken == ["t"]


def test_the_report_path_passes_whether_it_advanced():
    """Pin the call site: conductor_flow must tell the dispatcher."""
    import inspect

    from prism_service.api import conductor_flow

    src = inspect.getsource(conductor_flow)
    assert "advanced=" in src, (
        "conductor_flow must tell the handoff whether the step advanced")
