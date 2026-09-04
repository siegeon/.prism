"""A rewind opens a NEW pass, so the stall budget starts over with it
(task ce471e06, 2026-09-04).

`_stall_count` counted every `runner_attempt; step=<id>; advanced=false`
row for the WHOLE life of the task and never reset. That made every gate
REJECTION terminal: a reject rewinds the task to its producing step, and
when that step had already spent its STALL_ATTEMPTS budget on the earlier
pass, the guard fired on the very FIRST tick of the new pass and blocked
the task again — with a reason ("step implement_tasks did not advance
after 3 attempts") describing work the driver had not been allowed to
attempt.

LIVE REGRESSION: ce471e06 reached green_gate, CI found a real defect in
its own diff (an inline `text-[<11px]` size tripping the design-system
guard), the gate was rejected with that exact test id, the conductor
rewound to `implement_tasks` — and the runner immediately re-blocked the
task without ever invoking a driver. The reject mechanism itself was
unusable: a rejected gate could not be worked.

A rewind carries new direction, so it starts a fresh budget. Attempts
WITHIN one pass still stall exactly as before — this narrows when the
count starts, it never raises the ceiling.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

STEP = "implement_tasks"


def _task(project: str):
    from prism_service.project_context import get_project

    ctx = get_project(project)
    task = ctx.task_svc.create(title="rewind budget task", priority=5)
    ctx.task_svc.update(task.id, status="in_progress", workflow_step=STEP)
    return ctx, task.id


def _attempt(ctx, task_id: str) -> None:
    from prism_service.services import task_runner as tr

    ctx.task_svc.record_history(
        task_id, action=tr.ATTEMPT_ACTION,
        details=f"step={STEP}; advanced=false", actor="prism-task-runner")


# The LITERAL action ConductorService's rewind path writes. Spelled out
# rather than read from task_runner.REWIND_ACTION on purpose: at the base
# commit that constant does not exist, and a test that errors on a missing
# attribute proves only that the fix is absent — it never demonstrates the
# BEHAVIOUR. With the literal, the base commit fails on the assertion
# itself (the count stays at STALL_ATTEMPTS instead of resetting), which is
# the real red.
REWIND_HISTORY_ACTION = "auto_rewind"


def _rewind(ctx, task_id: str) -> None:
    ctx.task_svc.record_history(
        task_id, action=REWIND_HISTORY_ACTION,
        details=f"green_gate -> {STEP}; manual reject; reason=CI is red",
        actor="conductor-adjudicator")


def test_the_constant_names_the_action_the_conductor_actually_writes():
    """The fix keys off a history action written by ANOTHER module, so pin
    the string itself — a rename there would silently restore the bug."""
    from prism_service.services import task_runner as tr

    assert tr.REWIND_ACTION == REWIND_HISTORY_ACTION


def test_attempts_before_a_rewind_do_not_count_against_the_new_pass():
    """The live shape: a spent budget then a reject/rewind. The next pass
    starts at zero, so the driver actually gets to work the direction the
    rejection gave it."""
    from prism_service.services import task_runner as tr

    ctx, task_id = _task("rewind-budget-" + uuid.uuid4().hex[:8])
    for _ in range(tr.STALL_ATTEMPTS):
        _attempt(ctx, task_id)
    assert tr._stall_count(ctx.task_svc, task_id, STEP) == tr.STALL_ATTEMPTS

    _rewind(ctx, task_id)

    assert tr._stall_count(ctx.task_svc, task_id, STEP) == 0, (
        "a rewind opens a new pass — the previous pass's attempts must "
        "not spend the new budget before the driver runs once")


def test_attempts_after_a_rewind_still_stall():
    """The guard is narrowed, never lifted: a step that keeps failing
    within ONE pass still reaches the stall threshold."""
    from prism_service.services import task_runner as tr

    ctx, task_id = _task("rewind-budget-" + uuid.uuid4().hex[:8])
    for _ in range(tr.STALL_ATTEMPTS):
        _attempt(ctx, task_id)
    _rewind(ctx, task_id)
    for _ in range(tr.STALL_ATTEMPTS):
        _attempt(ctx, task_id)

    assert tr._stall_count(ctx.task_svc, task_id, STEP) == tr.STALL_ATTEMPTS


def test_only_the_latest_rewind_starts_the_count():
    """Two rewinds: the count starts at the most recent one, so an older
    pass can never spend the current budget."""
    from prism_service.services import task_runner as tr

    ctx, task_id = _task("rewind-budget-" + uuid.uuid4().hex[:8])
    _attempt(ctx, task_id)
    _rewind(ctx, task_id)
    _attempt(ctx, task_id)
    _attempt(ctx, task_id)
    _rewind(ctx, task_id)
    _attempt(ctx, task_id)

    assert tr._stall_count(ctx.task_svc, task_id, STEP) == 1


def test_a_rewind_does_not_clear_another_steps_count():
    """The budget is per-step: a rewind landing on this step must not be
    read as absolution for attempts recorded against a different one."""
    from prism_service.services import task_runner as tr

    ctx, task_id = _task("rewind-budget-" + uuid.uuid4().hex[:8])
    other = "verify_green_state"
    ctx.task_svc.record_history(
        task_id, action=tr.ATTEMPT_ACTION,
        details=f"step={other}; advanced=false", actor="prism-task-runner")

    assert tr._stall_count(ctx.task_svc, task_id, other) == 1
    assert tr._stall_count(ctx.task_svc, task_id, STEP) == 0


def test_no_rewind_counts_the_whole_history():
    """Unchanged behaviour when nothing rewound the task."""
    from prism_service.services import task_runner as tr

    ctx, task_id = _task("rewind-budget-" + uuid.uuid4().hex[:8])
    _attempt(ctx, task_id)
    _attempt(ctx, task_id)

    assert tr._stall_count(ctx.task_svc, task_id, STEP) == 2
