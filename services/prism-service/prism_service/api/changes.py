"""GET /api/changes -- the ONE tiny endpoint the SPA's shared poll layer
watches, so every page-level data query can gate its own refetch on "did
anything change" instead of guessing on a private fixed-interval timer.

Observed live (owner 2026-09-13): an idle Workflows tab issued ~10
independent requests every 1-2s -- version, staleness, workflows/live,
conductor/state, consolidation/workers, tasks, tasks/stranded, jobs,
sse/work -- because every consumer polled its own endpoint on its own
clock with no notion of whether the underlying data had moved.

Backed by services/wakeups.py's existing signal bus (the same mutation
points that already wake standing workers) -- no new entity, no new
storage, just a read of the newest signal timestamp visible to the
caller's project. Cheap by design: the SPA polls this every second.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Query

from prism_service.services import wakeups

router = APIRouter()


@router.get("")
def changes(project: str = Query("", description="Project scope; empty sees every project.")) -> dict:
    counter = wakeups.changes_snapshot(project or None)
    return {"counter": counter, "at": time.time()}
