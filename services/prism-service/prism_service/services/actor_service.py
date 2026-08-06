"""Resolves the free-form actor strings on tasks/history to a real Actor.

`Task.assigned_agent` and `TaskHistory.actor` are plain strings today, so the
conductor's distinct-actor rule compares TEXT rather than identities. This
service is the join: machine seats resolve by name, real people resolve
against the existing WorkspaceService user store (by id or email — no second
table), and anything else lands on ActorKind.UNKNOWN rather than raising, so
an old/foreign history row can never turn a page load into a 500.
"""

from __future__ import annotations

import threading
from typing import Optional

from prism_service.models.actor import Actor, ActorKind

try:
    # Importing the constant (not retyping it) keeps this resolver from
    # silently desyncing if the seat name ever changes.
    from prism_service.services.conductor_service import ADJUDICATOR_SEAT
except Exception:  # pragma: no cover - defensive; see MACHINE_SEATS fallback
    ADJUDICATOR_SEAT = "conductor-adjudicator"

MACHINE_SEATS = frozenset({ADJUDICATOR_SEAT, "conductor-autoclear",
                            "gate-card-rerun"})


class ActorService:
    """Resolves a raw actor string to an `Actor`. `resolve` never raises —
    an unresolvable/blank string degrades to ActorKind.UNKNOWN rather than
    bubbling an error into a task-detail page render."""

    def __init__(self, workspaces=None) -> None:
        self._workspaces = workspaces

    def resolve(self, raw: Optional[str]) -> Actor:
        text = (raw or "").strip()
        if text in MACHINE_SEATS:
            return Actor(id=f"machine:{text}", kind=ActorKind.MACHINE,
                         display_name=text)
        if text and self._workspaces is not None:
            user = self._resolve_user(text)
            if user is not None:
                name = user.display_name or user.email
                return Actor(id=f"user:{user.id}", kind=ActorKind.HUMAN,
                             display_name=name, user_id=user.id)
        placeholder = text or "unknown"
        return Actor(id=f"unknown:{placeholder}", kind=ActorKind.UNKNOWN,
                     display_name=placeholder)

    def _resolve_user(self, text: str):
        try:
            user = self._workspaces.get_user(text)
            if user is not None:
                return user
            return self._workspaces.get_user_by_email(text)
        except Exception:
            return None


_actor_service: Optional[ActorService] = None
_actor_service_lock = threading.Lock()


def get_actor_service() -> ActorService:
    """Return the process registry, lazily rooted in the real workspace
    user store (mirrors get_workspace_service)."""

    global _actor_service
    if _actor_service is None:
        with _actor_service_lock:
            if _actor_service is None:
                from prism_service.services.workspace_service import (
                    get_workspace_service,
                )
                _actor_service = ActorService(workspaces=get_workspace_service())
    return _actor_service
