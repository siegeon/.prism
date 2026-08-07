"""Domain model for a resolved identity behind a task actor string.

`Task.assigned_agent` and `TaskHistory.actor` are free strings that never
join to `User` (models/workspace.py) — the distinct-actor rule ends up
comparing TEXT. `Actor` is the real type those strings resolve to; see
services/actor_service.py for the resolver. Kept dependency-free from
services/api (ARC-PRISM-1): a models module never reaches outward.
"""

from __future__ import annotations

from dataclasses import dataclass


class ActorKind:
    """Plain string constants, not an enum — callers compare `actor.kind`
    to these directly, so `kind` must BE a str rather than wrap one."""

    HUMAN = "human"
    AGENT = "agent"
    MACHINE = "machine"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Actor:
    id: str
    kind: str
    display_name: str
    user_id: str = ""
    external_ref: str = ""
