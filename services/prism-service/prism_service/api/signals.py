"""Signals API -- the Queue's intake surface (task a6858911).

POST /api/signals is where a channel (collector, or the UI) drops a
signal into the Queue; GET /api/signals lists them, newest first. A
signal never mints a tasks row here -- that only happens when the owner
acts on it in the app (out of scope for this walking skeleton).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from prism_service.models.signal import Signal
from prism_service.models.task import validate_channel
from prism_service.services.signal_store import SignalStore

router = APIRouter()


class SignalCreate(BaseModel):
    channel: str = ""
    channel_ref: str = ""
    subject: str = ""
    body: str = ""
    sender: str = ""
    arrived_at: str = ""


@router.post("")
def post_signal(body: SignalCreate, project: str = Query("default")) -> dict:
    try:
        channel = validate_channel(body.channel) or "ui"
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    signal = Signal(
        project=project,
        channel=channel,
        channel_ref=body.channel_ref,
        subject=body.subject,
        body=body.body,
        sender=body.sender,
    )
    if body.arrived_at:
        signal.arrived_at = body.arrived_at
    store = SignalStore(project)
    store.create(signal)
    _resolve_best_effort(store, project, signal)
    return {"signal": signal.__dict__}


# ── Ontology resolution (task 785bb4ce) ─────────────────────────────────────
# Kept to a small, separate block so it merges cleanly alongside the sibling
# queue-page slice's own additions (POST .../promote, .../drop) to this file.

def _resolve_best_effort(store: SignalStore, project: str, signal: Signal) -> None:
    """Resolve `signal` against the ontology and persist matches. Never
    raises -- a resolver failure must not fail signal intake."""
    try:
        from prism_service.services.signal_resolver import resolve as resolve_signal
        signal.matches = resolve_signal(project, signal)
        store.update(signal.id, matches=signal.matches)
    except Exception:
        pass


@router.post("/{signal_id}/resolve")
def post_signal_resolve(signal_id: str, project: str = Query("default")) -> dict:
    store = SignalStore(project)
    signal = store.get(signal_id)
    if signal is None:
        raise HTTPException(status_code=404, detail="signal not found")
    _resolve_best_effort(store, project, signal)
    updated = store.get(signal_id)
    return {"signal": updated.__dict__}


@router.get("")
def get_signals(
    project: str = Query("default"),
    state: str = Query(""),
    limit: int = Query(200),
) -> dict:
    store = SignalStore(project)
    signals = store.list(state=state or None, limit=limit)
    return {"signals": [s.__dict__ for s in signals]}
