"""PRISM's collaboration port (epic 0784729f).

Nothing in the service reaches a person who is not sitting at this machine.
A gate decision only ever happens here, over SSE, or by clicking the web UI -
there is no way for a teammate on a phone, or watching a GitHub issue, to
approve or reject one. This module is the port that closes that gap: PRISM
publishes a DecisionCard onto some external surface (a GitHub issue thread
today, chat/email later) and polls that surface back for DecisionSignals.

PRISM VOCABULARY ONLY. Names here are task / gate / decision / actor -
never a provider word (issue, comment, npub, event). Each adapter translates
between this vocabulary and its own provider's shape; the port never learns
the provider's words.

Registration mirrors api/integrations.py's adapter registry exactly, for the
same reason that registry exists (task f4dd3687): this epic already shipped
seven green children whose feature could not run in production because every
test injected its own collaborator and nothing in production code ever
constructed one. A surface only counts if something registers it at import
time - see api/collaboration.py's `register_builtin_collaboration_adapters`.

ARCHITECTURE RULE: this module must never import from prism_service.api -
the port is the lower layer; routes depend on it, not the other way round.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class DecisionCard:
    """What PRISM asks an external surface to show a person: a gate on a
    task, waiting on a decision, with a link back to the real page."""

    task_id: str
    task_title: str
    gate_step: str
    summary: str
    url: str


@dataclass(frozen=True)
class DecisionSignal:
    """What an external surface hands back: someone decided a gate.

    ``actor`` is an identity string (e.g. ``github:<login>``), resolved by
    the caller through the actor registry - this port does not resolve
    identity itself. ``external_ref`` is the adapter's own opaque handle
    (e.g. a comment id) so a caller can trace a decision to its source.
    """

    task_id: str
    gate_step: str
    action: str  # "approve" | "reject"
    actor: str
    external_ref: str
    note: str = ""


@runtime_checkable
class CollaborationAdapter(Protocol):
    """One external surface. ``publish`` posts a card and returns an opaque
    reference to it; ``poll`` reads that surface and returns whatever new
    decisions it finds. Nothing here applies a decision - that is a
    caller's job, against the gate the decision names."""

    @property
    def surface(self) -> str: ...

    def publish(self, card: DecisionCard, target) -> str: ...

    def poll(self, target) -> list[DecisionSignal]: ...


# ── registry (mirrors api/integrations.py's _adapters exactly) ────────────

_adapters: dict[str, CollaborationAdapter] = {}


def register_adapter(adapter: CollaborationAdapter) -> None:
    _adapters[adapter.surface] = adapter


def reset_adapters() -> None:
    _adapters.clear()


def adapters_snapshot() -> dict:
    """Point-in-time copy of the registry, for a test to restore after it
    mutates (register_adapter/reset_adapters) - same pattern as
    api/integrations.py's adapters_snapshot."""
    return dict(_adapters)


def restore_adapters(snapshot: dict) -> None:
    _adapters.clear()
    _adapters.update(snapshot)


def surfaces() -> list[str]:
    return list(_adapters.keys())


def get_adapter(surface: str) -> "CollaborationAdapter | None":
    return _adapters.get(surface)
