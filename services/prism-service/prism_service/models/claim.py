"""Domain model for a task claim (task 0097a8a8, "Work finds the right
teammate").

``Task.assigned_agent`` (models/task.py:20) is a bare display string with
no holder identity and no expiry, so two runners polling the same pending
queue can both walk away thinking they own the same task. A ``Claim`` is
the missing lease: who holds a task, until when, and as which SDLC role.
See services/claim_service.py for the atomic claim/reap/route logic that
mints and retires these rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Claim:
    """A time-boxed lease on one task, held by one identity.

    ``role`` is the SDLC role (sm/qa/dev — models.roles.ROLES) the holder
    is dispensing work *as*, not the workspace permission role
    (models.workspace.Role, owner/admin/member/viewer). ``leased_at`` and
    ``expires_at`` are epoch seconds so lease math never depends on
    wall-clock string parsing or timezone-aware datetime comparisons.
    ``released_at`` is ``None`` while the lease is live; a non-``None``
    value (whether from an explicit release or from being reaped past its
    ``expires_at``) means the task is free again.
    """

    id: str
    task_id: str
    workspace_id: str
    holder_id: str
    role: str
    leased_at: float
    expires_at: float
    released_at: Optional[float] = None
