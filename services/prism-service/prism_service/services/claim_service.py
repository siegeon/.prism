"""Atomic, role-routed task dispensing (task 0097a8a8, "Work finds the
right teammate").

``TaskService.next_task()`` (services/task_service.py:748) is a pure read
with no mutation, so two runners polling it concurrently receive the
identical top-priority task. ``ClaimService.claim_next`` replaces that
race with a real lease: it reaps any expired claim on a candidate task and
inserts a fresh one inside the SAME sqlite transaction that a UNIQUE
partial index (``idx_claims_active_task``, one un-released row per
``task_id``) enforces — a second caller's INSERT for the same task fails
closed with ``sqlite3.IntegrityError`` rather than racing in Python.

Dispensing is member-scoped: a holder must be a workspace member at or
above the ``member`` permission rank (``models.workspace.role_allows``,
fail-closed on unknown roles) and only receives tasks whose current SDLC
role (``models.roles.role_for_step``) matches the role they asked to work
as. Deliberately does NOT touch services/conductor_service.py — that file
is a control_plane.POLICY_FILES entry owned by a concurrent sibling task.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from typing import Optional
from uuid import uuid4

from prism_service.models.claim import Claim
from prism_service.models.roles import normalize_role, role_for_step
from prism_service.models.workspace import role_allows
from prism_service.services import sqlite_db
from prism_service.services.task_service import TaskService
from prism_service.services.workspace_service import WorkspaceService

_CREATE_CLAIMS_SQL = """
CREATE TABLE IF NOT EXISTS claims (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    holder_id TEXT NOT NULL,
    role TEXT NOT NULL,
    leased_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    released_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_active_task
    ON claims(task_id) WHERE released_at IS NULL;
"""


class ClaimService:
    """Dispenses tasks as leased ``Claim`` rows, scoped to one project's
    ``TaskService`` and the shared cross-project ``WorkspaceService``.

    One sqlite connection per calling thread (mirrors TaskService's own
    per-thread factory, services/task_service.py:266) so real concurrent
    callers — real threads, real separate connections to the same
    file-backed database — race through actual sqlite locking rather than
    an in-process lock standing in for it.
    """

    def __init__(
        self,
        db_path: str,
        task_svc: Optional[TaskService] = None,
        workspace_service: Optional[WorkspaceService] = None,
    ) -> None:
        # task_svc/workspace_service are required only by claim_next, which
        # DISPENSES work and so must check membership and route by role. The
        # per-task lease below (acquire/release/holder_of) is used by daemon
        # SEATS -- task_runner, resume_actuator, ship_worker -- which are not
        # workspace members and already know which task they intend to drive.
        # Making them optional lets a seat take the same lease without
        # inventing a fake membership (task 1bcb2b24).
        self._db_path = db_path
        self._task_svc = task_svc
        self._workspace_service = workspace_service
        self._tlocal = threading.local()
        self._db.executescript(_CREATE_CLAIMS_SQL)

    @property
    def _db(self) -> sqlite3.Connection:
        conn = getattr(self._tlocal, "conn", None)
        if conn is None:
            conn = sqlite_db.connect(self._db_path)
            self._tlocal.conn = conn
        return conn

    def claim_next(
        self,
        workspace_id: str,
        holder_id: str,
        role: str,
        ttl_s: Optional[float],
        project: str = "",
    ) -> Optional[Claim]:
        """Atomically dispense the next eligible pending task to
        ``holder_id``, scoped to their role in ``workspace_id``.

        Returns ``None`` when there is no eligible task, when the caller
        is not a workspace member, or when the caller's membership role
        is below ``member`` (fail closed — a viewer is never dispensed
        actionable work). Raises ``ValueError`` for a non-positive
        ``ttl_s``: a lease that never expires can strand a task forever
        behind a crashed runner, so it is refused outright rather than
        silently accepted. ``project`` is recorded for audit/multi-project
        callers; task selection itself is scoped by which TaskService
        this ClaimService was constructed with.
        """
        if ttl_s is None or ttl_s <= 0:
            raise ValueError(
                "ttl_s must be a positive number of seconds — a lease "
                "with no expiry can strand a task behind a crashed runner"
            )

        membership = self._workspace_service.membership_for(workspace_id, holder_id)
        if membership is None or not role_allows(membership.role, "member"):
            return None

        wanted_role = normalize_role(role)
        now = time.time()

        done_ids = {t.id for t in self._task_svc.list(status="done")}
        pending = self._task_svc.list(status="pending")
        candidates = [
            t
            for t in pending
            if all(dep in done_ids for dep in t.dependencies)
            and role_for_step(t.workflow_step) == wanted_role
        ]

        for task in candidates:
            claim_id = self._try_claim(
                task.id, workspace_id, holder_id, wanted_role, now, ttl_s
            )
            if claim_id is not None:
                return Claim(
                    id=claim_id,
                    task_id=task.id,
                    workspace_id=workspace_id,
                    holder_id=holder_id,
                    role=wanted_role,
                    leased_at=now,
                    expires_at=now + ttl_s,
                    released_at=None,
                )
        return None

    # ------------------------------------------------------------------
    # Per-task lease for daemon seats (task 1bcb2b24)
    # ------------------------------------------------------------------
    # LIVE INCIDENT 2026-08-30, task 1edee95c: two `claude -p` processes ran
    # the same step against the same task workspace -- same directory, same
    # index, same HEAD -- while an operator agent worked there too. A test
    # file was overwritten mid-write; HEAD moved under a driver and an `rm`
    # nearly destroyed committed work. The only guard was
    # task_runner._foreign_driver_on, which sees a driver ONLY if it posts a
    # heartbeat, and checks once at claim time rather than for the life of
    # the run. This class already held the real answer -- the partial unique
    # index below makes a second INSERT fail closed at the sqlite level --
    # and had no caller at all.

    def acquire(
        self,
        task_id: str,
        holder_id: str,
        ttl_s: float,
        role: str = "",
        workspace_id: str = "local",
    ) -> Optional[str]:
        """Take the lease on ONE task, or return None if somebody holds it.

        Returns the claim id. A None return is not an error: it means another
        driver is already working the task, and the caller should skip it.
        Reaps an expired lease first, so a crashed holder never wedges the
        task -- a lock that cannot expire is worse than the race it replaces.
        """
        return self._try_claim(
            task_id=task_id, workspace_id=workspace_id, holder_id=holder_id,
            role=role, now=time.time(),
            # Guard only against zero/negative, never impose a floor: a
            # caller may legitimately want a short lease, and a floor
            # would silently extend it past what the caller asked for.
            ttl_s=max(0.01, float(ttl_s)),
        )

    def release(self, claim_id: Optional[str]) -> None:
        """Free the task. Safe to call with None or an unknown id."""
        if not claim_id:
            return
        try:
            conn = self._db
            conn.execute(
                "UPDATE claims SET released_at=? WHERE id=? "
                "AND released_at IS NULL", (time.time(), claim_id))
            conn.commit()
        except Exception:
            pass

    def holder_of(self, task_id: str) -> Optional[str]:
        """Who holds a live lease on `task_id`, or None when it is free.

        An expired lease reads as free, matching what acquire() would do.
        A refused driver uses this to say WHO holds the task -- the live
        incident was diagnosable only by matching PIDs by hand.
        """
        try:
            row = self._db.execute(
                "SELECT holder_id FROM claims WHERE task_id=? "
                "AND released_at IS NULL AND expires_at>? LIMIT 1",
                (task_id, time.time()),
            ).fetchone()
        except Exception:
            return None
        return row[0] if row else None

    def _try_claim(
        self,
        task_id: str,
        workspace_id: str,
        holder_id: str,
        role: str,
        now: float,
        ttl_s: float,
    ) -> Optional[str]:
        """Reap any expired claim on ``task_id`` and insert a fresh one in
        one transaction. The partial unique index is the real safety net:
        a still-active claim on ``task_id`` (whether held by this caller's
        own race or a concurrent one) makes the INSERT fail closed."""
        conn = self._db
        claim_id = str(uuid4())
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            # Another writer holds the file lock right now — treat this
            # poll as a lost race for this task rather than blocking.
            return None
        try:
            conn.execute(
                "DELETE FROM claims WHERE task_id=? AND released_at IS NULL "
                "AND expires_at<=?",
                (task_id, now),
            )
            conn.execute(
                "INSERT INTO claims "
                "(id, task_id, workspace_id, holder_id, role, leased_at, "
                "expires_at, released_at) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
                (claim_id, task_id, workspace_id, holder_id, role, now, now + ttl_s),
            )
        except sqlite3.IntegrityError:
            conn.rollback()
            return None
        conn.commit()
        return claim_id
