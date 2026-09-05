"""Oracle for task 0097a8a8 ("Work finds the right teammate").

Two runners polling concurrently must never receive the same task, an
expired lease must requeue rather than strand the work, and a dispensing
call scoped to a workspace member must return only role-matching work.

Every fixture here is a REAL collaborator: a real file-backed TaskService,
a real file-backed WorkspaceService, and a real ClaimService against its
own file-backed sqlite claims store — AC-1's concurrency is driven by real
threads racing real sqlite connections, not two fakes agreeing with each
other.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass

import pytest

from prism_service.services.claim_service import ClaimService
from prism_service.services.task_service import TaskService
from prism_service.services.workspace_service import WorkspaceService


@dataclass
class Rig:
    claims: ClaimService
    tasks: TaskService
    workspaces: WorkspaceService
    workspace_id: str
    owner_id: str
    member_id: str
    viewer_id: str


@pytest.fixture
def rig(tmp_path):
    tasks = TaskService(str(tmp_path / "tasks.db"))
    workspaces = WorkspaceService(tmp_path / "workspace.db")
    claims = ClaimService(str(tmp_path / "claims.db"), tasks, workspaces)

    owner = workspaces.create_user("owner@example.test", user_id="user-owner")
    member = workspaces.create_user("member@example.test", user_id="user-member")
    viewer = workspaces.create_user("viewer@example.test", user_id="user-viewer")
    workspace = workspaces.create_workspace("Crew", owner.id, workspace_id="workspace-crew")
    workspaces.add_membership(workspace.id, member.id, "member")
    workspaces.add_membership(workspace.id, viewer.id, "viewer")

    yield Rig(
        claims=claims,
        tasks=tasks,
        workspaces=workspaces,
        workspace_id=workspace.id,
        owner_id=owner.id,
        member_id=member.id,
        viewer_id=viewer.id,
    )

    # AC-1 and AC-2: hand the store back with no live lease. This runs after
    # `yield`, so a body that fails or raises part way through still releases
    # what it took. It reads only this rig's own `tmp_path/claims.db` and frees
    # each row through `ClaimService.reclaim`, the service's named release —
    # never an ad-hoc UPDATE, and never a store a live seat holds.
    for _holder_id, task_id, _expires_at in live_leases(tmp_path / "claims.db"):
        claims.reclaim(task_id, reason="claim suite teardown")


def live_leases(store_path) -> list:
    """Rows in ONE claims store that are still held: not released, not expired.

    Reads the file directly, so it observes what the store hands to the next
    reader. It opens only the path it is given.
    """
    if not store_path.exists():
        return []
    conn = sqlite3.connect(str(store_path))
    try:
        return conn.execute(
            "SELECT holder_id, task_id, expires_at FROM claims "
            "WHERE released_at IS NULL AND expires_at > ?",
            (time.time(),),
        ).fetchall()
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def the_store_comes_back_clean(tmp_path):
    """AC-1 and AC-2: every lease this module takes is released before the
    test that took it ends.

    This fixture is autouse, so pytest builds it BEFORE `rig` and finalises it
    AFTER `rig`. That order is what lets it observe the state `rig` teardown
    hands back. It reads only `tmp_path/claims.db`, the store this test's own
    `rig` built, so it can never touch a lease that a live seat holds.
    """
    yield
    left = live_leases(tmp_path / "claims.db")
    assert left == [], (
        "this test handed its claims store back with a live lease still in it; "
        f"rows (holder_id, task_id, expires_at): {left}"
    )


def test_two_runners_polling_concurrently_never_double_claim(rig):
    """AC-1: N real threads race claim_next() for the SAME single pending
    task against one shared file-backed store; exactly one wins it."""
    task = rig.tasks.create(title="Only one task in the queue")

    holders = [f"runner-{i}" for i in range(8)]
    for holder_id in holders:
        rig.workspaces.create_user(f"{holder_id}@example.test", user_id=holder_id)
        rig.workspaces.add_membership(rig.workspace_id, holder_id, "member")

    winners: list = []
    lock = threading.Lock()

    def poll(holder_id: str) -> None:
        claim = rig.claims.claim_next(rig.workspace_id, holder_id, "dev", ttl_s=300)
        with lock:
            winners.append((holder_id, claim))

    threads = [threading.Thread(target=poll, args=(h,)) for h in holders]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    claimants = [
        holder for holder, claim in winners
        if claim is not None and claim.task_id == task.id
    ]
    assert len(claimants) == 1, f"expected exactly one winner, got {claimants}"


def test_expired_lease_requeues_instead_of_stranding(rig):
    """AC-2: a lease past its expires_at is not honored as active — the
    task it names is dispensed to a new holder instead of staying
    stranded behind a crashed runner."""
    task = rig.tasks.create(title="Held by a runner that then vanished")

    first = rig.claims.claim_next(rig.workspace_id, rig.member_id, "dev", ttl_s=0.05)
    assert first is not None
    assert first.task_id == task.id

    # No third-party release call exists — this genuinely stands in for a
    # crashed runner: the holder simply never comes back.
    time.sleep(0.15)

    second = rig.claims.claim_next(rig.workspace_id, "user-owner", "dev", ttl_s=300)
    assert second is not None
    assert second.task_id == task.id
    assert second.holder_id == "user-owner"


def test_lease_requires_a_positive_expiry(rig):
    """AC-3: ttl_s<=0 or None is refused outright — a lease that can
    never expire is exactly the strand this task exists to prevent."""
    rig.tasks.create(title="Never eligible for a zero-ttl lease")

    with pytest.raises(ValueError):
        rig.claims.claim_next(rig.workspace_id, rig.member_id, "dev", ttl_s=0)

    with pytest.raises(ValueError):
        rig.claims.claim_next(rig.workspace_id, rig.member_id, "dev", ttl_s=None)

    with pytest.raises(ValueError):
        rig.claims.claim_next(rig.workspace_id, rig.member_id, "dev", ttl_s=-5)


def test_member_scoped_dispatch_returns_role_matching_work(rig):
    """AC-4: dispensing scoped to a member's requested role returns only
    tasks whose current SDLC role (role_for_step) matches — never a task
    that belongs to a different role."""
    dev_task = rig.tasks.create(title="A builder-stage task")
    qa_task = rig.tasks.create(title="A verifier-stage task")
    rig.tasks.update(qa_task.id, workflow_step="write_failing_tests")
    # dev_task keeps workflow_step="" -> role_for_step defaults to "dev".

    qa_claim = rig.claims.claim_next(rig.workspace_id, rig.member_id, "qa", ttl_s=300)
    assert qa_claim is not None
    assert qa_claim.task_id == qa_task.id
    assert qa_claim.task_id != dev_task.id

    dev_claim = rig.claims.claim_next(rig.workspace_id, rig.member_id, "dev", ttl_s=300)
    assert dev_claim is not None
    assert dev_claim.task_id == dev_task.id


def test_non_member_and_viewer_are_fail_closed_from_dispensing(rig):
    """AC-5: a caller with no membership row, and a caller holding only
    `viewer`, are both fail-closed out of dispensing even though eligible
    role-matching work exists — never a task, never an exception leaking
    the task's existence."""
    rig.tasks.create(title="Eligible dev-role work sitting in the queue")

    stranger_claim = rig.claims.claim_next(
        rig.workspace_id, "user-does-not-exist", "dev", ttl_s=300
    )
    assert stranger_claim is None

    viewer_claim = rig.claims.claim_next(rig.workspace_id, rig.viewer_id, "dev", ttl_s=300)
    assert viewer_claim is None


def test_a_body_that_raises_still_hands_the_store_back_clean(rig):
    """AC-2: the release does not depend on the test body reaching its last
    line. A body takes a lease and then raises. Teardown, which runs after
    `rig`, must still find the store empty."""
    rig.tasks.create(title="Claimed by a body that then stops")

    def claim_then_raise() -> None:
        claim = rig.claims.claim_next(rig.workspace_id, rig.member_id, "dev", ttl_s=300)
        assert claim is not None
        raise RuntimeError("the body stops here, the lease is still held")

    with pytest.raises(RuntimeError):
        claim_then_raise()

    assert live_leases(tmp_path_of(rig)) != [], (
        "the body must really hold a lease at this point, or this test proves nothing"
    )


def tmp_path_of(rig) -> object:
    """The claims store that this rig built. `ClaimService` keeps the path it
    opened, so the test reads the same file the fixture wrote."""
    from pathlib import Path

    return Path(rig.claims._db_path)
