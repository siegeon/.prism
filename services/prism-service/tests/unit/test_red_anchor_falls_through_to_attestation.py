"""A REJECTED red anchor must not shadow the attestation path.

`_red_step_sha`'s docstring promises '' when neither a recorded
`red_step_sha` history row nor a resolved tests-only commit is usable. Its
tail did the opposite: `return recorded or tests_sha` handed back the very
row the two checks above had just rejected.

Consequence, observed live on task e4c631d7 (2026-08-30): a concurrent
drive stamped a `red_step_sha` row pointing at the SHIPPED commit, which is
not tests-only. `_red_step_sha` correctly refused it, then returned it
anyway. Because the value was non-empty, `adjudicate_test_red_gate`'s
`if not red_sha:` guard never fired, so the tier-2 `red-anchor-ref:`
attestation path was unreachable and a correctly attested pre-change ref
was silently ignored. red_gate parked as "NOT red ... will not retry"
against a commit that obviously passes.

Pinned here: once the recorded row fails the tests-only/reachable checks,
the resolver yields the resolved tests commit or '' -- never the rejected
row.
"""

from __future__ import annotations

import uuid


def _svc(project: str):
    from prism_service.project_context import get_project
    return get_project(project).conductor_svc


def test_a_rejected_recorded_anchor_does_not_shadow_the_attestation_path():
    project = "redanchor-" + uuid.uuid4().hex[:8]
    svc = _svc(project)
    task = svc._task_svc.create(title="tests and impl landed in one commit")

    # A recorded row pointing at a commit that is NOT tests-only -- exactly
    # what a concurrent drive stamped on e4c631d7.
    bogus = "5ea606e84d7bc3e905c24ec77039da20f9481a8d"
    svc._task_svc.record_history(task.id, actor="another-drive",
                                 action="red_step_sha", details=bogus)

    # No tests-only commit resolves for a task with no such commit.
    svc._red_tests_commit = lambda _tid: ("", "")

    # The driver attested a real pre-change ref, which is what makes tier 2
    # available -- and tier 2 is reachable ONLY when this resolver returns ''.
    svc._task_svc.update(
        task.id,
        completion_proof="red-anchor-ref: c55dd7633db23f55c6579331ed4fc6def09ff763\nran the pinned tests there: rc=1, 4 failed")

    got = svc._red_step_sha(task.id)
    assert got != bogus, (
        "the resolver rejected this row as not-tests-only and must not hand "
        "it back while an attestation is on file; returning it keeps "
        "`if not red_sha:` from firing, which silently forecloses the "
        f"red-anchor-ref path -- got {got!r}")
    assert got == "", (
        "with an attestation on file and no tests-only commit, the resolver "
        f"must yield '' so the caller reaches tier 2 -- got {got!r}")


def test_a_rejected_row_is_still_used_when_nothing_better_exists():
    """THE GUARD ON THE FIX. With no tests-only commit AND no attestation,
    the rejected row still beats nothing. Dropping it would make the machine
    seat abstain and route red_gate to a human, which owner rule 2026-08-04
    forbids: red_gate belongs to the machine seat."""
    project = "redanchor-" + uuid.uuid4().hex[:8]
    svc = _svc(project)
    task = svc._task_svc.create(title="no tests-only commit, no attestation")

    bogus = "3333333333333333333333333333333333333333"
    svc._task_svc.record_history(task.id, actor="conductor",
                                 action="red_step_sha", details=bogus)
    svc._red_tests_commit = lambda _tid: ("", "")

    got = svc._red_step_sha(task.id)
    assert got == bogus, (
        "with nothing better on file the seat must keep its anchor rather "
        f"than abstain and hand red_gate to a person -- got {got!r}")


def test_a_resolved_tests_commit_still_wins_over_a_rejected_row():
    """The fix must not lose the self-heal: a genuine tests-only commit is
    still preferred over a mis-stamped recorded row."""
    project = "redanchor-" + uuid.uuid4().hex[:8]
    svc = _svc(project)
    task = svc._task_svc.create(title="a lagging worktree mis-stamped the row")

    bogus = "1111111111111111111111111111111111111111"
    real = "2222222222222222222222222222222222222222"
    svc._task_svc.record_history(task.id, actor="lagging-worktree",
                                 action="red_step_sha", details=bogus)
    svc._red_tests_commit = lambda _tid: (real, "")

    got = svc._red_step_sha(task.id)
    assert got == real, (
        "a real tests-only commit must still self-heal a mis-stamped anchor "
        f"-- got {got!r}")
