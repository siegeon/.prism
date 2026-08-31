"""The handoff: a finished piece of work triggers the next one (task cdb8e365).

Owner direction, 2026-08-30: "The whole point of having these bots is that
they work a task. When they are done with the task, they add it to the next
flow. The next flow picks it up, and then when they are done, they hand it to
the next flow ... there is no reason to have a system like this where we have
background processes trying way to do stuff. That gets confusing." And: "The
task needs to be able to complete on its own."

BOTH HANDOFFS WERE ALREADY COMPUTED AND THEN DROPPED.

Step to step: ``ConductorService.advance_task`` says so in its own docstring
-- "Move a task to the next entry in WORKFLOW_STEPS". It works out the next
step, writes it to the row, and returns without telling anybody. Four
pollers then rediscover, on their own timers, a fact that was known at that
instant.

Task to task: ``TaskService.next_task`` filters pending tasks by
``all(dep in done_ids)``. So ``dependencies`` is a filter on a PULL, and
nothing performs that pull when a task completes. Worse,
``task_runner.eligible_task`` only scans tasks already ``in_progress``, so a
pending dependent never starts on its own at all. Measured on epic
4c9b39e5: three slices carried real dependency edges and an operator
hand-launched every one of them.

This module is CODIFIED -- deterministic Python, no model call. Orchestration
is the phase being made free, so it must not itself reach a model. A test
asserts that by name.

WHY THIS IS SAFE TO FIRE MORE THAN ONCE: every driver takes the per-task
lease (services/claim_service.py, wired into task_runner and
resume_actuator). A duplicate trigger loses the race and skips; it cannot
produce the two-drivers-one-worktree incident of 2026-08-30.

WHY THE TIMERS STAY, FOR NOW: a dropped handoff must not strand a task
forever, which would be worse than a slow sweep. The existing intervals are
the reconciler until they can be demoted deliberately -- removing them in
the same change would leave no backstop.
"""

from __future__ import annotations

from typing import Optional



def _task_svc_for(project: str):
    """The task service for `project`. Seam so a test can inject one."""
    from prism_service.project_context import get_project

    return get_project(project).task_svc


def _is_agent_step(step_id: str) -> bool:
    """Is `step_id` a step a DRIVER owns?

    A gate belongs to a distinct seat, never to the driver that just
    reported -- waking the runner on a gate would hand a task straight back
    to its own producer, which the no-self-review rule exists to prevent.
    """
    try:
        from prism_service.services.conductor_service import ConductorService

        step = ConductorService._step_by_id(step_id)
        return bool(step) and step.get("type") == "agent"
    except Exception:
        return False


def _drive_now(task_id: str, project: str) -> None:
    """Drive `task_id`'s current step immediately, off the request thread.

    This is the latency the handoff exists to remove: the next owner is
    known the instant the step advances, and without this the task sits
    until task_runner's interval finds it (900 s on this instance).

    The driver takes the per-task lease, so firing this while another seat
    is mid-step loses the race and returns -- it cannot reproduce the
    two-drivers-one-worktree incident of 2026-08-30.
    """
    import threading

    def _run() -> None:
        try:
            from prism_service.services import task_runner

            task_runner._run_one_step(project, task_id)
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True,
                     name=f"handoff-{task_id[:8]}").start()


def unblocked_by(task_id: str, task_svc) -> list[str]:
    """Ids of the PENDING tasks that `task_id` completing has just freed.

    Only returns a dependent when EVERY one of its edges is closed. Closing
    one edge is not enough: a naive "a dependency finished, therefore start
    it" fires a task whose other dependencies are still open, which on epic
    4c9b39e5 would have launched a slice while its sibling was mid-drive.

    A dependent that is already running is never returned -- re-firing a
    live task is the two-drivers-one-worktree collision under a new name.
    """
    try:
        done = task_svc.get(task_id)
        if done is None or getattr(done, "status", "") != "done":
            return []
        done_ids = {t.id for t in task_svc.list(status="done")}
        out = []
        for t in task_svc.list(status="pending"):
            deps = list(getattr(t, "dependencies", None) or [])
            if not deps or task_id not in deps:
                continue
            if all(d in done_ids for d in deps):
                out.append(t.id)
        return out
    except Exception:
        # A handoff that raises must never fail the report that triggered
        # it. The reconciler still catches the work.
        return []


def parent_ready_to_assemble(task_id: str, task_svc) -> Optional[str]:
    """The parent id to hand back for assembly, or None.

    This is the SEW half of "slice, then shuffle them back together". When
    the last child of a parent lands, the parent is handed back so it can
    demonstrate its OWN oracle.

    A parent is never satisfied by green children. Epic 0784729f shipped 13
    of them while the feature could not run at all, so the trigger returns
    the parent for real work, never a roll-up that closes it.
    """
    try:
        child = task_svc.get(task_id)
        if child is None:
            return None
        parent_id = str(getattr(child, "parent_id", "") or "")
        if not parent_id:
            return None
        siblings = task_svc.list(parent_id=parent_id)
        if not siblings:
            return None
        for s in siblings:
            if getattr(s, "status", "") not in ("done", "cancelled"):
                return None
        return parent_id
    except Exception:
        return None


def _start(task_id: str, project: str) -> None:
    """Put one task in front of a driver, without waiting for a timer."""
    from prism_service.project_context import get_project

    svc = get_project(project).task_svc
    t = svc.get(task_id)
    if t is None or getattr(t, "status", "") != "pending":
        return
    # in_progress is what makes a task eligible to a driver seat
    # (task_runner.eligible_task). The seat still takes the lease, so two
    # triggers for one task cannot both drive it.
    svc.update(task_id, status="in_progress")


def after_task_done(task_id: str, project: str) -> dict:
    """Hand off everything that `task_id` completing has just enabled.

    Returns what it triggered, so a caller can record the handoff rather
    than guess at it. Never raises: a failed handoff must not fail the
    report that triggered it.
    """
    started: list[str] = []
    parent = None
    try:
        from prism_service.project_context import get_project

        svc = get_project(project).task_svc
        for dep_id in unblocked_by(task_id, svc):
            _start(dep_id, project)
            started.append(dep_id)
        parent = parent_ready_to_assemble(task_id, svc)
        if parent:
            _start(parent, project)
    except Exception:
        pass
    return {"kind": "conductor.handoff", "from": task_id,
            "started": started, "assemble": parent}


def after_step(task_id: str, project: str) -> dict:
    """Hand a task on after one of its steps concluded.

    The next step's owner is deterministic -- the FSM names it -- so there
    is nothing to discover. When the task itself finished, the handoff
    continues outward to whatever that completion unblocked.
    """
    try:
        t = _task_svc_for(project).get(task_id)
    except Exception:
        return {"kind": "conductor.handoff", "from": task_id, "started": []}
    if t is None:
        return {"kind": "conductor.handoff", "from": task_id, "started": []}
    if getattr(t, "status", "") == "done":
        return after_task_done(task_id, project)

    # On an AGENT step: wake a driver now rather than leaving it for the
    # next sweep. On a GATE: leave it -- a gate belongs to a distinct seat,
    # and waking the runner would hand the task back to its own producer.
    step = str(getattr(t, "workflow_step", "") or "")
    if getattr(t, "status", "") == "in_progress" and _is_agent_step(step):
        _drive_now(task_id, project)
        return {"kind": "conductor.handoff", "from": task_id,
                "started": [task_id], "step": step, "drove": True}
    return {"kind": "conductor.handoff", "from": task_id, "started": [],
            "step": step, "drove": False}

def on_started(task_id: str, project: str) -> dict:
    """Drive a task that has just become startable.

    THE CHAIN HAD NO BEGINNING. step-to-step and task-to-task were both
    wired, but nothing fired when a task first went in_progress -- so on
    2026-08-30 a task marked ready sat pending for ten minutes untouched,
    because the only thing that would have found it scans tasks already
    in_progress, on a 900 s timer.

    Idempotent by construction: the driver takes the per-task lease, so
    calling this on a task somebody is already driving loses the race.
    """
    try:
        t = _task_svc_for(project).get(task_id)
    except Exception:
        return {"kind": "conductor.handoff", "from": task_id, "started": []}
    if t is None or getattr(t, "status", "") != "in_progress":
        return {"kind": "conductor.handoff", "from": task_id, "started": []}
    _drive_now(task_id, project)
    return {"kind": "conductor.handoff", "from": task_id,
            "started": [task_id], "drove": True}
