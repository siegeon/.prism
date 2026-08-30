"""One task worktree admits one driver (task 1bcb2b24).

LIVE INCIDENT, 2026-08-30, task 1edee95c. Two `claude -p` processes (PIDs
373033 and 373577) ran the SAME step with `--plugin-dir` pointed at the same
task workspace, while an operator-spawned agent worked in that directory too.
Same directory, same index, same HEAD. A test file was overwritten mid-write
and landed half-written; HEAD moved under a driver so a file it believed
untracked was in fact committed, and an `rm` nearly destroyed real work.

`task_runner._foreign_driver_on` was the only guard, and it only sees a driver
that posts a HEARTBEAT — a seat that posts none is invisible to it, and it
checks once at claim time rather than for the life of the run.

`ClaimService` already had the real answer and no caller: a UNIQUE partial
index (`idx_claims_active_task`, one unreleased row per task_id) makes a
second INSERT fail closed at the sqlite level rather than racing in Python,
and `_try_claim` reaps an expired lease first so a dead holder cannot wedge
the task for ever. These tests pin the public entry point and the wiring.
"""

from __future__ import annotations

import time

import pytest


def _svc(tmp_path):
    """A ClaimService over a throwaway db, with no workspace coupling.

    `claim_next` is member-scoped and role-routed; the per-task primitive
    deliberately is not, because a daemon seat is not a workspace member.
    """
    from prism_service.services.claim_service import ClaimService

    return ClaimService(db_path=str(tmp_path / "claims.db"))


# ----------------------------------------------------------------------
# The lease itself
# ----------------------------------------------------------------------

def test_a_second_driver_refuses_and_names_the_holder(tmp_path):
    """Two seats, one task: the second is refused and can see who holds it.

    The refusal must be informative. A driver that is merely told "no" logs
    a mystery; the live incident was diagnosable only because a human read
    `pgrep` output and matched PIDs by hand.
    """
    svc = _svc(tmp_path)

    first = svc.acquire("task-1", holder_id="prism-task-runner", ttl_s=900)
    second = svc.acquire("task-1", holder_id="prism-resume-actuator", ttl_s=900)

    assert first, "the first driver takes the lease"
    assert second is None, "the second driver must be refused"
    assert svc.holder_of("task-1") == "prism-task-runner"


def test_a_dead_holder_lease_expires(tmp_path):
    """A crashed holder must not wedge the task for ever.

    A lock that cannot expire is worse than the race it replaces — task
    1ecbd866 already records a stall count that never resets and blocks a
    fixed task permanently.
    """
    svc = _svc(tmp_path)

    assert svc.acquire("task-2", holder_id="dead-seat", ttl_s=0.05)
    time.sleep(0.2)

    assert svc.acquire("task-2", holder_id="live-seat", ttl_s=900), (
        "an expired lease must be reaped, not honoured for ever")
    assert svc.holder_of("task-2") == "live-seat"


def test_a_released_lease_frees_the_task(tmp_path):
    svc = _svc(tmp_path)

    cid = svc.acquire("task-3", holder_id="seat-a", ttl_s=900)
    assert svc.acquire("task-3", holder_id="seat-b", ttl_s=900) is None
    svc.release(cid)

    assert svc.acquire("task-3", holder_id="seat-b", ttl_s=900)
    assert svc.holder_of("task-3") == "seat-b"


def test_two_tasks_do_not_block_each_other(tmp_path):
    """The lease is per task, never a global lock on driving."""
    svc = _svc(tmp_path)

    assert svc.acquire("task-4", holder_id="seat-a", ttl_s=900)
    assert svc.acquire("task-5", holder_id="seat-b", ttl_s=900)


def test_a_free_task_reports_no_holder(tmp_path):
    assert _svc(tmp_path).holder_of("never-claimed") is None


# ----------------------------------------------------------------------
# Every seat takes the same lease — the wiring, not just the helper
# ----------------------------------------------------------------------

def test_every_seat_takes_the_same_lease():
    """`task_runner` must actually CALL the lease before it invokes.

    A lock with no caller is exactly the state that produced the incident:
    `ClaimService` already existed, fully atomic, and nothing used it. This
    project has shipped four such built-but-unwired mechanisms; pin the
    call site, never only the helper.
    """
    import inspect

    from prism_service.services import task_runner

    src = inspect.getsource(task_runner)
    assert "acquire(" in src, (
        "task_runner must take a lease before driving a step")
    assert "release(" in src, (
        "task_runner must release the lease when the step ends")

    run = inspect.getsource(task_runner._run_one_step)
    assert run.index("acquire(") < run.index("claude_cli.invoke"), (
        "the lease must be taken BEFORE the model is invoked, not after")


def test_a_refused_lease_skips_the_task_without_failing_it():
    """Losing the race is not an error — it means somebody else is driving.

    A refusal must not mark the task failed or advance its step; the other
    holder is doing the work.
    """
    import inspect

    from prism_service.services import task_runner

    run = inspect.getsource(task_runner._run_one_step)
    assert "already driving" in run or "held by" in run, (
        "a refused claim must say who holds the task")
