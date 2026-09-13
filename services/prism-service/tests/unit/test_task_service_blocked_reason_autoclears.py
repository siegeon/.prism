"""blocked_reason must not outlive the blocked status it describes
(task a65c66e5, 2026-09-13, ops incident).

Live regression: task a65c66e5 sat blocked with
"resume-actuator: retry budget spent (3/3) -- parked for a human", then a
raw status PATCH (status="in_progress", no blocked_reason kwarg -- NOT
resume_actuator.release(), which already clears the field deliberately)
flipped it back to in_progress while blocked_reason kept the stale park
text forever, reading as "still parked" on the card even though the task
was actively driving again.

resume_actuator.release() already clears blocked_reason on its own unpark
path -- so this is not a resume_actuator-specific fix. Any OTHER caller
that unblocks a task via TaskService.update without naming a fresh
blocked_reason hit the exact same staleness. Fixed at the one place every
caller shares: TaskService.update auto-clears a stale blocked_reason
whenever status leaves "blocked" and the caller does not supply a
replacement in the same call.
"""

from __future__ import annotations

import tempfile

from prism_service.services.task_service import TaskService

_LEGACY_PARK = ("resume-actuator: retry budget spent (3/3) — parked for a "
               "human")


def test_leaving_blocked_status_clears_a_stale_blocked_reason():
    with tempfile.TemporaryDirectory() as tmpdir:
        svc = TaskService(db_path=f"{tmpdir}/test.db")
        task = svc.create(title="stale park probe", priority=10)
        svc.update(task.id, status="blocked", blocked_reason=_LEGACY_PARK)
        assert svc.get(task.id).blocked_reason == _LEGACY_PARK

        # Exactly the raw operator/API PATCH that flipped a65c66e5: a bare
        # status transition, no blocked_reason kwarg at all.
        svc.update(task.id, status="in_progress")

        after = svc.get(task.id)
        assert after.status == "in_progress"
        assert after.blocked_reason == "", (
            f"blocked_reason must clear once status leaves blocked, but "
            f"still reads {after.blocked_reason!r}")


def test_a_caller_re_parking_in_the_same_call_is_never_overridden():
    """A caller that sets status='blocked' WITH a fresh blocked_reason in
    the same call must see that reason, never a clear."""
    with tempfile.TemporaryDirectory() as tmpdir:
        svc = TaskService(db_path=f"{tmpdir}/test.db")
        task = svc.create(title="re-park probe", priority=10)
        svc.update(task.id, status="blocked", blocked_reason=_LEGACY_PARK)

        svc.update(task.id, status="blocked", blocked_reason="fresh reason")

        assert svc.get(task.id).blocked_reason == "fresh reason"


def test_an_unrelated_field_update_while_blocked_leaves_the_reason_alone():
    """Updating some other field on an already-blocked task (no status
    kwarg at all) must never touch blocked_reason."""
    with tempfile.TemporaryDirectory() as tmpdir:
        svc = TaskService(db_path=f"{tmpdir}/test.db")
        task = svc.create(title="untouched probe", priority=10)
        svc.update(task.id, status="blocked", blocked_reason=_LEGACY_PARK)

        svc.update(task.id, priority=20)

        assert svc.get(task.id).blocked_reason == _LEGACY_PARK
        assert svc.get(task.id).status == "blocked"


def test_a_caller_explicitly_clearing_blocked_reason_is_unaffected():
    """A caller that already passes blocked_reason="" alongside the status
    change (e.g. resume_actuator.release()) hits the same end state via its
    own explicit write, not this auto-clear -- pinning that the two paths
    never conflict."""
    with tempfile.TemporaryDirectory() as tmpdir:
        svc = TaskService(db_path=f"{tmpdir}/test.db")
        task = svc.create(title="explicit clear probe", priority=10)
        svc.update(task.id, status="blocked", blocked_reason=_LEGACY_PARK)

        svc.update(task.id, status="in_progress", blocked_reason="")

        assert svc.get(task.id).blocked_reason == ""
