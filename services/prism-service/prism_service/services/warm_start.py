"""Prime GET /api/conductor/state and GET /api/work/graph's caches for
every project already in use, off the request path, at boot.

Tick-cost pass (external fixer, owner brief 2026-09-13, no PRISM ticket):
the FIRST request to either route after a daemon restart measured 3.2-3.6s
live, paying every cold cache at once (managed_tasks' advance-rows/history/
list snapshots, drive_heartbeat's batched read, control_plane's
policy_hash, claude_transcripts' per-file mtime cache) -- versus under
200ms once warm. Same shape as brain_engine.warm_embedder (task b0138f17):
a low-priority background thread does the one real cold call per route per
project so no real user request has to.
"""

from __future__ import annotations

import logging

_log = logging.getLogger("prism.warm_start")


def warm_polled_route_caches() -> None:
    """Call the real state()/work_graph() handlers once for every project
    project_activity.is_in_use() reports as in use, so their caches are
    warm before the first real poll. Never raises -- a warmup that fails
    is a missed optimization, not a boot defect; each project is wrapped
    independently so one bad project's failure never skips the rest."""
    # Deliberately NOT wakeups.wait_out_startup_warmup() -- that gate exists
    # for STANDING workers whose first tick can wait (default 120s), and
    # this primer's whole point is to beat the first real request, which
    # can land within seconds of boot. Only the low-priority nice() applies.
    try:
        from prism_service.services.wakeups import lower_thread_priority
        lower_thread_priority()
    except Exception:
        pass
    try:
        from prism_service.config import list_projects
        from prism_service.services import project_activity
        from prism_service.project_context import get_project
        from prism_service.api import conductor as conductor_api
        from prism_service.api import work as work_api
    except Exception:
        _log.warning("warm_polled_route_caches: import failed", exc_info=True)
        return
    try:
        projects = list_projects()
    except Exception:
        _log.warning("warm_polled_route_caches: list_projects failed", exc_info=True)
        return
    for pid in projects:
        try:
            if not project_activity.is_in_use(pid):
                continue
            get_project(pid)
            conductor_api.state(project=pid)
            work_api.work_graph(project=pid)
        except Exception:
            _log.warning("warm_polled_route_caches: %s failed", pid, exc_info=True)
            continue
