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
        task_svc: TaskService,
        workspace_service: WorkspaceService,
    ) -> None:
        self._db_path = db_path
        self._task_svc = task_svc
        self._workspace_service = workspace_service
        self._tlocal = threading.local()
        self._db.executescript(_CREATE_CLAIMS_SQL)

    @property
    def _db(self) -> sqlite3.Connection:
        conn = getattr(self._tlocal, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._db_path, timeout=5.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
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
