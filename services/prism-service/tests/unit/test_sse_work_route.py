"""GET /sse/work -- project + event-type filter (gamify walking skeleton).

The /live graph opens ONE EventSource against /sse/work?project=X and
expects to see only {task.changed, drive.heartbeat, agent.run,
tokens.turn} events scoped to its own project -- never another project's
traffic, never an unrelated bus event type (e.g. session_outcome).
Drives the REAL sse_work generator directly (same pattern as
test_task_page_payload_scope.py's test_sse_tasks_route_filters_by_task_id)
against the REAL prism_service.events.bus.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


class _FakeRequest:
    def __init__(self):
        self.disconnected = False

    async def is_disconnected(self):
        return self.disconnected


def test_sse_work_route_exists_and_connects():
    from prism_service.routes import sse as sse_mod

    assert hasattr(sse_mod, "sse_work"), (
        "routes/sse.py must expose sse_work — GET /sse/work?project= — "
        "the /live graph's incremental feed")

    async def _run():
        req = _FakeRequest()
        resp = await sse_mod.sse_work(req, project="gamify")
        it = resp.body_iterator
        first = await asyncio.wait_for(it.__anext__(), timeout=2.0)
        assert b"connected" in first, f"expected an initial connect comment frame; got {first!r}"
        req.disconnected = True

    asyncio.run(_run())


def test_sse_work_filters_by_project_and_type():
    from prism_service.events import bus
    from prism_service.routes import sse as sse_mod

    async def _run():
        req = _FakeRequest()
        resp = await sse_mod.sse_work(req, project="gamify")
        it = resp.body_iterator
        await asyncio.wait_for(it.__anext__(), timeout=2.0)  # connect frame

        # 1. Foreign project's drive.heartbeat must never be delivered.
        bus.publish({"project": "OTHER", "type": "drive.heartbeat", "task_id": "x"})
        # 2. Right project, but a type sse_work does NOT carry (session_outcome).
        bus.publish({"project": "gamify", "type": "session_outcome", "session_id": "s1"})
        # 3. Right project, right type -> must be delivered.
        bus.publish({"project": "gamify", "type": "drive.heartbeat", "task_id": "T-1"})

        chunk = await asyncio.wait_for(it.__anext__(), timeout=2.0)
        payload = json.loads(chunk.decode("utf-8").split("data: ", 1)[1].strip())
        assert payload.get("project") == "gamify", f"got {payload!r}"
        assert payload.get("type") == "drive.heartbeat", f"got {payload!r}"
        assert payload.get("task_id") == "T-1", (
            f"the foreign-project event and the off-type event must both "
            f"have been skipped, delivering only the matching one; got {payload!r}")
        req.disconnected = True

    asyncio.run(_run())


def test_sse_work_forwards_every_declared_type():
    from prism_service.events import bus
    from prism_service.routes import sse as sse_mod

    async def _run():
        req = _FakeRequest()
        resp = await sse_mod.sse_work(req, project="gamify")
        it = resp.body_iterator
        await asyncio.wait_for(it.__anext__(), timeout=2.0)  # connect frame

        for et in ("task.changed", "drive.heartbeat", "agent.run", "tokens.turn"):
            bus.publish({"project": "gamify", "type": et, "marker": et})
            chunk = await asyncio.wait_for(it.__anext__(), timeout=2.0)
            payload = json.loads(chunk.decode("utf-8").split("data: ", 1)[1].strip())
            assert payload.get("type") == et, f"expected {et}, got {payload!r}"
        req.disconnected = True

    asyncio.run(_run())
