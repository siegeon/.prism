"""SSE endpoint for server-push UI updates.

Subscribes to the event bus and streams filtered events for a given
project. The new SPA opens `new EventSource('/sse/sessions?project=X')`
and rebuilds only when a relevant event arrives.
"""

from __future__ import annotations

import asyncio
import json
import time

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


# Task fix/polling ("why are you hammering the server with polling rather
# than updating with streaming", owner 2026-09-13): GET /sse/changes
# replaces the SPA's shared GET /api/changes 1Hz poll (lib/useChanges.ts)
# with a real push. Driven by services/wakeups.py's wait()/changed_since()
# instead of the in-process `bus` every other route on this page uses,
# because wakeups already bridges CROSS-PROCESS signals (a 2026-09-13
# worker-host process split moved the nine standing workers into a
# separate OS process; wait() polls a small sqlite table under the data
# dir every 250ms so a signal() raised in that other process still wakes a
# waiter here) -- exactly the channel this stream needs to reach a signal
# raised by a background worker, not just one raised by an HTTP request in
# THIS process. `wait()` is a blocking call (threading.Condition), so it
# runs in a thread via asyncio.to_thread rather than blocking the event
# loop; its own 15s timeout doubles as the heartbeat interval.
_CHANGE_KINDS = frozenset({
    "task_changed", "shipped", "activity",
    # Task fix/lasttimers: three fixed-interval browser timers (a jobs
    # poll, a staleness+consolidation poll, and two /api/version polls)
    # were replaced with real backend signals -- these three kinds must
    # be in this set or those signals never reach a browser at all, and
    # the "no interval" fix silently regresses to "never refetches until
    # focus/the reconnect floor". See services/wakeups.py's call sites:
    # inference/queue.py ("jobs"), understand_engine.py/maintenance_clock.py/
    # drift_worker.py ("staleness"), deploy_worker.py/main.py ("deployed").
    "jobs", "staleness", "deployed",
})
_CHANGE_HEARTBEAT_S = 15.0


async def _gen_changes(request: Request, project: str):
    """The stream body, factored out of sse_changes() as a plain
    module-level async generator so a test can drive it directly (pull
    frames via `__anext__()` against a fake `request`) instead of going
    through TestClient's HTTP/ASGI streaming layer, which buffers a
    StreamingResponse's chunks unpredictably under a synchronous test
    client and made a first version of this test hang."""
    from prism_service.services import wakeups

    # Baseline captured BEFORE the first yield, not after: a generator
    # pauses AT a yield until the next pull, so anything computed after it
    # would race a caller that reacts to "connected" by signalling right
    # away (as a real client effectively does, and as the cross-process
    # test below does) -- that signal must land AFTER this baseline, never
    # racing to land before it and being missed as "already old".
    baseline = time.time()
    yield b": connected\n\n"
    while True:
        if await request.is_disconnected():
            break
        woke = await asyncio.to_thread(
            wakeups.wait, _CHANGE_KINDS, project=project,
            timeout=_CHANGE_HEARTBEAT_S, since=baseline,
        )
        if not woke:
            yield b": keepalive\n\n"
            continue
        changes = wakeups.changed_since(_CHANGE_KINDS, project, baseline)
        baseline = time.time()
        for kind, proj, ts, task_id in changes:
            payload = json.dumps(
                {"kind": kind, "project": proj, "task_id": task_id, "at": ts},
                separators=(",", ":"),
            )
            yield f"data: {payload}\n\n".encode("utf-8")


@router.get("/changes")
async def sse_changes(request: Request, project: str = "default"):
    """Stream one event per wakeups signal -- {kind, project, counter} --
    for `project` (plus wildcard signals). A heartbeat comment every
    15s keeps the connection alive through idle-timeout proxies and gives
    the client a liveness signal distinct from "no changes happened"."""
    return StreamingResponse(
        _gen_changes(request, project),
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
