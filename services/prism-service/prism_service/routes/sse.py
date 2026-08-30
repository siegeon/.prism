"""SSE endpoint for server-push UI updates.

Subscribes to the event bus and streams filtered events for a given
project. The new SPA opens `new EventSource('/sse/sessions?project=X')`
and rebuilds only when a relevant event arrives.
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Request
from starlette.responses import StreamingResponse

from prism_service.events import bus

router = APIRouter()

_KEEPALIVE_SECONDS = 25.0

# Event types the /live "PRISM shows its work" graph consumes (gamify
# walking skeleton). task.changed already existed for /sse/tasks;
# drive.heartbeat, agent.run, tokens.turn are new bus publishers (see
# api/drive_heartbeat.py, api/agent_runs.py, services/conductor_service.py
# _record_agent_run, services/work_stream.py).
# flow.node (task 8fbd5cf0): one event per CONCLUDED conductor node, so an
# open canvas moves the token in real time instead of waiting for a reload.
_WORK_EVENT_TYPES = frozenset({
    "task.changed", "drive.heartbeat", "agent.run", "tokens.turn",
    "flow.node",
})

# gamify data-enrichment slice item 3: NO separate "work.status" event.
# task.changed's `fields` dict (services/task_service.py TaskService.update
# -- `changed_fields`) already carries every SCALAR field that changed on
# the write, which includes `status` and `gate_state` whenever either one
# actually moved (workflow_step too, when it moved). So a task completing
# (status -> done/cancelled) or a gate arriving/clearing (gate_state
# changing) is ALREADY an unambiguous task.changed event on this stream --
# the /live frontend's completion/gate animations should key off
# `event.fields.status` and `event.fields.gate_state` on task.changed
# (present in `fields` only when that field is one of the ones that just
# changed; absent otherwise, never a false trigger). Publishing a second,
# differently-named event with the same payload would be a duplicate
# source of truth for the same write.


@router.get("/sessions")
async def sse_sessions(request: Request, project: str = "default"):
    """Stream session/skill events for one project as SSE."""

    async def gen():
        q = bus.subscribe()
        try:
            yield b": connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(q.get(), timeout=_KEEPALIVE_SECONDS)
                except asyncio.TimeoutError:
                    yield b": keepalive\n\n"
                    continue
                if event.get("project") != project:
                    continue
                payload = json.dumps(event, separators=(",", ":"))
                yield f"data: {payload}\n\n".encode("utf-8")
        finally:
            bus.unsubscribe(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/tasks")
async def sse_tasks(request: Request, project: str = "default", task_id: str = ""):
    """Stream task-lifecycle events for ONE task as SSE (task 2d480b08).

    Mirrors sse_sessions's project filter, plus a task_id filter so the
    task detail page gets scoped/incremental pushes instead of polling.
    """

    async def gen():
        q = bus.subscribe()
        try:
            yield b": connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(q.get(), timeout=_KEEPALIVE_SECONDS)
                except asyncio.TimeoutError:
                    yield b": keepalive\n\n"
                    continue
                if event.get("project") != project:
                    continue
                if event.get("type") != "task.changed" or event.get("task_id") != task_id:
                    continue
                payload = json.dumps(event, separators=(",", ":"))
                yield f"data: {payload}\n\n".encode("utf-8")
        finally:
            bus.unsubscribe(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/work")
async def sse_work(request: Request, project: str = "default"):
    """Stream the 'PRISM shows its work' event set for one project as SSE
    -- the /live graph's incremental feed after its /api/work/graph boot
    snapshot. Same project-scoped, bare `data: {json}` shape as
    /sse/sessions and /sse/tasks; filters to _WORK_EVENT_TYPES so the
    graph never has to filter irrelevant bus traffic client-side."""

    async def gen():
        q = bus.subscribe()
        try:
            yield b": connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(q.get(), timeout=_KEEPALIVE_SECONDS)
                except asyncio.TimeoutError:
                    yield b": keepalive\n\n"
                    continue
                if event.get("project") != project:
                    continue
                if event.get("type") not in _WORK_EVENT_TYPES:
                    continue
                payload = json.dumps(event, separators=(",", ":"))
                yield f"data: {payload}\n\n".encode("utf-8")
        finally:
            bus.unsubscribe(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/agent-bridge/{session_id}")
async def sse_agent_bridge(request: Request, session_id: str, token: str = ""):
    """Command stream for ONE agent-bridge session (live remote-assist —
    see services/agent_bridge.py). Mirrors sse_tasks's per-id filter shape.

    Auth gap this exists for: EventSource cannot send an Authorization
    header, so unlike every other route here this one is NOT gated by
    enforce_team_boundary's bearer/access-key path (api/security.py carves
    this path shape out explicitly). Instead the bridge session's own
    short-lived token — passed as a query param, the only option EventSource
    has — is the credential, checked right here against the session record.
    An invalid/expired/revoked token gets a real 401, never a silent stream
    that simply never delivers anything.
    """
    from prism_service.services.agent_bridge import get_agent_bridge_service

    service = get_agent_bridge_service()
    if service.validate_token(session_id, token) is None:
        from fastapi import HTTPException
        raise HTTPException(401, "invalid, expired, or revoked bridge session")

    async def gen():
        q = bus.subscribe()
        try:
            yield b": connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(q.get(), timeout=_KEEPALIVE_SECONDS)
                except asyncio.TimeoutError:
                    yield b": keepalive\n\n"
                    continue
                if event.get("type") != "agent_bridge.command":
                    continue
                if event.get("session_id") != session_id:
                    continue
                payload = json.dumps(event, separators=(",", ":"))
                yield f"data: {payload}\n\n".encode("utf-8")
        finally:
            bus.unsubscribe(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/live")
async def sse_live(request: Request):
    """Emit the running build's version so the SPA can detect a
    container swap (e.g. Watchtower auto-update) and reload itself
    without the user having to hard-refresh."""

    from prism_service.__version__ import PRISM_VERSION

    async def gen():
        payload = json.dumps({"version": PRISM_VERSION}, separators=(",", ":"))
        yield f"data: {payload}\n\n".encode("utf-8")
        while True:
            if await request.is_disconnected():
                break
            await asyncio.sleep(_KEEPALIVE_SECONDS)
            yield b": keepalive\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
