"""sweep_once() must not let ONE persistently failing task starve every
other eligible task, and a task that keeps failing identically must stop
being retried automatically.

REAL, CONFIRMED-LIVE BUG this suite pins: `sweep_once()` iterated
`_awaiting_ship`/`_awaiting_ship_machine`, called `ship_task` on the FIRST
eligible task, and `return`ed immediately regardless of outcome. Observed
live via `aspire logs`: task 0e2c82f3's PR #2348 was genuinely
`mergeStateStatus=DIRTY`/`mergeable=CONFLICTING`, and dozens of consecutive
sweeps each re-selected 0e2c82f3, hit the identical
`{'ok': False, 'stage': 'merge', 'error': "...not mergeable..."}`, and
returned — while an unrelated, correctly green_gate-passed task (8b4e7cb6)
sat starved behind it for 20+ minutes.

Fix, two parts (see ship_worker.py `sweep_once`/`_note_ship_result`):
  (a) a failed attempt no longer stops the pass — the NEXT eligible task
      gets a real shot in the SAME sweep, bounded by
      MAX_SHIP_ATTEMPTS_PER_SWEEP so one pass cannot spiral into shipping
      the whole queue.
  (b) a task that fails at the SAME stage with the SAME error
      STALL_THRESHOLD times in a row is flipped to `status=blocked` with a
      concrete `blocked_reason`, and (because _awaiting_ship/_awaiting_ship_
      machine only ever scan `status="in_progress"` tasks) it is no longer
      selected on the next sweep. A task whose failures are NOT identical
      each time is never blocked.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _services(tmp_path):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService

    task_svc = TaskService(str(tmp_path / "tasks.db"))
    cond = ConductorService(str(tmp_path / "scores.db"), enable_engine=False,
                            task_svc=task_svc, verifier_svc=None)
    return task_svc, cond


def _wire_project(monkeypatch, task_svc, cond, project="default"):
    """Wire the real task_svc/cond into project_context.get_project(project)
    (the seam _note_ship_result's `_services()` helper falls back to), and
    pin get_all_projects() to just this one project."""
    from prism_service.project_context import get_project
    import prism_service.project_context as pc

    ctx = get_project(project)
    monkeypatch.setattr(ctx, "_task_svc", task_svc, raising=False)
    monkeypatch.setattr(ctx, "_conductor_svc", cond, raising=False)
    monkeypatch.setattr(pc, "get_all_projects", lambda: [project])


def _reset_streaks(monkeypatch):
    from prism_service.services import ship_worker
    monkeypatch.setattr(ship_worker, "_FAILURE_STREAKS", {})


CONFLICT = {"ok": False, "stage": "merge",
           "error": "X Pull request #2348 is not mergeable: the merge "
                    "commit cannot be cleanly created"}


# ---------------------------------------------------------------------------
# (a) a stuck task must not starve a healthy one in the SAME sweep
# ---------------------------------------------------------------------------


def test_a_stuck_task_does_not_starve_a_healthy_task_in_the_same_sweep(
        tmp_path, monkeypatch):
    from prism_service.services import ship_worker

    task_svc, cond = _services(tmp_path)
    _wire_project(monkeypatch, task_svc, cond)
    _reset_streaks(monkeypatch)

    stuck_id = "0e2c82f3-0000-0000-0000-000000000000"
    healthy_id = "8b4e7cb6-0000-0000-0000-000000000000"

    monkeypatch.setattr(ship_worker, "_awaiting_ship",
                        lambda pid: [stuck_id, healthy_id])
    monkeypatch.setattr(ship_worker, "_awaiting_ship_machine", lambda pid: [])

    calls: list[str] = []

    def _fake_ship_task(tid, pid, on_landed=None, **kw):
        calls.append(tid)
        if tid == stuck_id:
            return dict(CONFLICT)
        return {"ok": True, "stage": "merged", "error": "", "pr": 7}

    monkeypatch.setattr(ship_worker, "ship_task", _fake_ship_task)

    res = ship_worker.sweep_once()

    assert calls == [stuck_id, healthy_id], (
        "the healthy task must get a real attempt in the SAME sweep, not be "
        f"starved behind the stuck one; calls={calls!r}")
    assert res == {"ok": True, "stage": "merged", "error": "", "pr": 7}, (
        "sweep_once must report the outcome of the LAST attempt made, not "
        "silently swallow the healthy task's success behind the stuck "
        f"task's earlier failure; got {res!r}")


def test_sweep_bounds_attempts_at_max_ship_attempts_per_sweep(
        tmp_path, monkeypatch):
    """A pass never attempts more than MAX_SHIP_ATTEMPTS_PER_SWEEP tasks,
    even when more are eligible — the bound on blast radius."""
    from prism_service.services import ship_worker

    task_svc, cond = _services(tmp_path)
    _wire_project(monkeypatch, task_svc, cond)
    _reset_streaks(monkeypatch)

    ids = [f"task-{i}" for i in range(ship_worker.MAX_SHIP_ATTEMPTS_PER_SWEEP + 4)]
    monkeypatch.setattr(ship_worker, "_awaiting_ship", lambda pid: list(ids))
    monkeypatch.setattr(ship_worker, "_awaiting_ship_machine", lambda pid: [])

    calls: list[str] = []

    def _fake_ship_task(tid, pid, on_landed=None, **kw):
        calls.append(tid)
        return dict(CONFLICT)  # every attempt fails, so the loop never
                               # exits early for any other reason

    monkeypatch.setattr(ship_worker, "ship_task", _fake_ship_task)

    ship_worker.sweep_once()

    assert len(calls) == ship_worker.MAX_SHIP_ATTEMPTS_PER_SWEEP, (
        f"expected exactly {ship_worker.MAX_SHIP_ATTEMPTS_PER_SWEEP} "
        f"attempts, got {len(calls)}: {calls!r}")


# ---------------------------------------------------------------------------
# (b) a task that fails IDENTICALLY N times in a row gets blocked, and drops
# out of the next sweep's eligibility scan.
# ---------------------------------------------------------------------------


def test_task_is_blocked_after_n_consecutive_identical_failures(
        tmp_path, monkeypatch):
    from prism_service.services import ship_worker

    task_svc, cond = _services(tmp_path)
    _wire_project(monkeypatch, task_svc, cond)
    _reset_streaks(monkeypatch)

    t = task_svc.create(title="stuck ship", tags=["conductor"])
    task_svc.update(t.id, status="in_progress", workflow_step="green_gate",
                    gate_state="pending")

    monkeypatch.setattr(ship_worker, "_awaiting_ship", lambda pid: [t.id])
    monkeypatch.setattr(ship_worker, "_awaiting_ship_machine", lambda pid: [])
    monkeypatch.setattr(ship_worker, "ship_task",
                        lambda tid, pid, on_landed=None, **kw: dict(CONFLICT))

    for i in range(ship_worker.STALL_THRESHOLD - 1):
        ship_worker.sweep_once()
        still = task_svc.get(t.id)
        assert still.status == "in_progress", (
            f"must not block before STALL_THRESHOLD identical failures "
            f"(attempt {i + 1})")

    ship_worker.sweep_once()

    blocked = task_svc.get(t.id)
    assert blocked.status == "blocked", (
        f"expected the task blocked after {ship_worker.STALL_THRESHOLD} "
        f"identical failures; status={blocked.status!r}")
    assert blocked.blocked_reason, "a blocked task must carry a real reason"
    assert "merge" in blocked.blocked_reason.lower(), blocked.blocked_reason
    assert "2348" in blocked.blocked_reason or "not mergeable" in \
        blocked.blocked_reason.lower(), (
        "the blocked_reason must name the actual problem, not a generic "
        f"placeholder: {blocked.blocked_reason!r}")

    # No longer selected on the next sweep: _awaiting_ship/_awaiting_ship_
    # machine both scan status="in_progress" only, and this task is now
    # status="blocked".
    assert task_svc.list(status="in_progress") == [], (
        "a blocked task must not still read back as in_progress")


def test_a_healthy_looking_second_task_still_ships_after_the_first_blocks(
        tmp_path, monkeypatch):
    """Once the stuck task is blocked, a DIFFERENT eligible task must still
    ship normally in a later sweep — the guard targets the one stuck task,
    not the whole queue."""
    from prism_service.services import ship_worker

    task_svc, cond = _services(tmp_path)
    _wire_project(monkeypatch, task_svc, cond)
    _reset_streaks(monkeypatch)

    stuck = task_svc.create(title="stuck ship", tags=["conductor"])
    task_svc.update(stuck.id, status="in_progress",
                    workflow_step="green_gate", gate_state="pending")
    healthy = task_svc.create(title="healthy ship", tags=["conductor"])
    task_svc.update(healthy.id, status="in_progress",
                    workflow_step="green_gate", gate_state="pending")

    def _fake_ship_task(tid, pid, on_landed=None, **kw):
        if tid == stuck.id:
            return dict(CONFLICT)
        return {"ok": True, "stage": "merged", "error": "", "pr": 3}

    monkeypatch.setattr(ship_worker, "_awaiting_ship_machine", lambda pid: [])
    monkeypatch.setattr(ship_worker, "ship_task", _fake_ship_task)

    # Drive the stuck task to STALL_THRESHOLD failures (healthy stays
    # ineligible in these sweeps to isolate the guard being exercised).
    monkeypatch.setattr(ship_worker, "_awaiting_ship", lambda pid: [stuck.id])
    for _ in range(ship_worker.STALL_THRESHOLD):
        ship_worker.sweep_once()
    assert task_svc.get(stuck.id).status == "blocked"

    # Now both are nominally eligible; the real eligibility scan excludes
    # the blocked one, so only the healthy task ships.
    monkeypatch.setattr(ship_worker, "_awaiting_ship",
                        lambda pid: [stuck.id, healthy.id])
    res = ship_worker.sweep_once()

    assert res == {"ok": True, "stage": "merged", "error": "", "pr": 3}, res


# ---------------------------------------------------------------------------
# (c) failures that are NOT identical each time never trip the guard
# ---------------------------------------------------------------------------


def test_non_identical_failures_never_block(tmp_path, monkeypatch):
    from prism_service.services import ship_worker

    task_svc, cond = _services(tmp_path)
    _wire_project(monkeypatch, task_svc, cond)
    _reset_streaks(monkeypatch)

    t = task_svc.create(title="flaky ship", tags=["conductor"])
    task_svc.update(t.id, status="in_progress", workflow_step="green_gate",
                    gate_state="pending")

    varying = [
        {"ok": False, "stage": "push", "error": "! [rejected] non-fast-forward"},
        {"ok": False, "stage": "pr_create", "error": "GraphQL: rate limited"},
        {"ok": False, "stage": "ci_wait", "error": "check 'build' failed: exit 1"},
        {"ok": False, "stage": "ci_wait", "error": "check 'lint' failed: exit 3"},
    ]
    calls = {"n": 0}

    def _fake_ship_task(tid, pid, on_landed=None, **kw):
        res = varying[calls["n"] % len(varying)]
        calls["n"] += 1
        return dict(res)

    monkeypatch.setattr(ship_worker, "_awaiting_ship", lambda pid: [t.id])
    monkeypatch.setattr(ship_worker, "_awaiting_ship_machine", lambda pid: [])
    monkeypatch.setattr(ship_worker, "ship_task", _fake_ship_task)

    for _ in range(len(varying) * 2):
        ship_worker.sweep_once()

    still = task_svc.get(t.id)
    assert still.status == "in_progress", (
        "a task whose failures differ in stage/error each time must never "
        f"be auto-blocked; status={still.status!r}, "
        f"blocked_reason={still.blocked_reason!r}")


def test_success_resets_the_failure_streak(tmp_path, monkeypatch):
    """A task that fails, fails again identically, then SUCCEEDS must not
    carry any residual streak into a later, unrelated failure run."""
    from prism_service.services import ship_worker

    task_svc, cond = _services(tmp_path)
    _wire_project(monkeypatch, task_svc, cond)
    _reset_streaks(monkeypatch)

    t = task_svc.create(title="recovers then fails again", tags=["conductor"])
    task_svc.update(t.id, status="in_progress", workflow_step="green_gate",
                    gate_state="pending")

    monkeypatch.setattr(ship_worker, "_awaiting_ship", lambda pid: [t.id])
    monkeypatch.setattr(ship_worker, "_awaiting_ship_machine", lambda pid: [])

    sequence = [dict(CONFLICT), dict(CONFLICT),
               {"ok": True, "stage": "merged", "error": "", "pr": 1},
               dict(CONFLICT)]
    calls = {"n": 0}

    def _fake_ship_task(tid, pid, on_landed=None, **kw):
        res = sequence[calls["n"]]
        calls["n"] += 1
        return dict(res)

    monkeypatch.setattr(ship_worker, "ship_task", _fake_ship_task)

    for _ in range(len(sequence)):
        ship_worker.sweep_once()

    still = task_svc.get(t.id)
    assert still.status == "in_progress", (
        "the streak must reset on the intervening success, so a single "
        f"post-success failure must not have blocked the task yet; "
        f"status={still.status!r}")
