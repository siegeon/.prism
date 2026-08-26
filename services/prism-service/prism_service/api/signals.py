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
from prism_service.models.task import validate_channel, validate_workflow
from prism_service.project_context import get_project
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
    return {"signal": signal.__dict__}


@router.get("")
def get_signals(
    project: str = Query("default"),
    state: str = Query(""),
    limit: int = Query(200),
) -> dict:
    store = SignalStore(project)
    signals = store.list(state=state or None, limit=limit)
    return {"signals": [s.__dict__ for s in signals]}


class SignalPromote(BaseModel):
    title: str
    workflow: str = "triage"
    description: str = ""


# The owner's model (mx-0889e4): a signal becomes a task ONLY when the
# owner types what to do and clicks -- promote is that click. Description
# defaults to the signal's own body plus a plain context line naming where
# it came from (NOT the mirror-trailer format another slice owns -- this is
# just context for whoever reads the new task).
@router.post("/{signal_id}/promote")
def promote_signal(signal_id: str, body: SignalPromote, project: str = Query("default")) -> dict:
    store = SignalStore(project)
    signal = store.get(signal_id)
    if signal is None:
        raise HTTPException(status_code=404, detail="signal not found")
    if signal.state != "open":
        raise HTTPException(status_code=409, detail=f"signal is {signal.state}, not open")

    try:
        workflow = validate_workflow(body.workflow or "triage") or "triage"
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    description = body.description
    if not description:
        context = f"From {signal.channel}: {signal.channel_ref}"
        description = f"{signal.body}\n\n{context}" if signal.body else context

    tags = ["queue"] + ([signal.channel] if signal.channel else [])
    task = get_project(project).task_svc.create(
        title=body.title,
        description=description,
        channel=signal.channel,
        channel_ref=signal.channel_ref,
        workflow=workflow,
        tags=tags,
    )

    store.update(signal_id, state="became_task", task_id=task.id)
    updated = store.get(signal_id)
    return {"signal": updated.__dict__, "task": task.__dict__}


class SignalDrop(BaseModel):
    reason: str = ""


@router.post("/{signal_id}/drop")
def drop_signal(signal_id: str, body: SignalDrop, project: str = Query("default")) -> dict:
    store = SignalStore(project)
    signal = store.get(signal_id)
    if signal is None:
        raise HTTPException(status_code=404, detail="signal not found")
    if signal.state != "open":
        raise HTTPException(status_code=409, detail=f"signal is {signal.state}, not open")

    store.update(signal_id, state="dropped", drop_reason=body.reason)
    updated = store.get(signal_id)
    return {"signal": updated.__dict__}
