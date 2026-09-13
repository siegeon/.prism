"""GET /api/changes -- originally the SPA's shared poll target (task
fix/polling); superseded by GET /sse/changes (routes/sse.py) once the SPA
moved from polling to a real push. Kept alive as a DEBUG/diagnostic
surface: `?debug=1` names the busiest (kind, call site) pairs on the
wakeups signal bus, for exactly the question "the bus is noisy, not the
pages" needs answered (owner 2026-09-13, live measurement: 265 req/min on
a nominally idle tab).
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Query

from prism_service.services import wakeups

router = APIRouter()


@router.get("")
def changes(
    project: str = Query("", description="Project scope; empty sees every project."),
    debug: bool = Query(False, description="Include the busiest (kind, call site) pairs since the last reset."),
) -> dict:
    counter = wakeups.changes_snapshot(project or None)
    body: dict = {"counter": counter, "at": time.time()}
    if debug:
        body["top_sources"] = wakeups.debug_sources()
    return body
