"""warm_polled_route_caches primes conductor/state + work/graph for every
project already in use, and skips ones that are not (tick-cost pass,
external fixer, owner brief 2026-09-13, no PRISM ticket -- the first
request after a restart measured 3.2-3.6s live; this pays that cost off
the request path instead)."""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def test_lifespan_wires_the_warm_start_thread():
    """Source check, not a full lifespan boot (too heavy for this suite,
    spins up MCP/workers): main.py must actually spawn the primer thread,
    or this whole module is dead code nobody calls."""
    main_py = _SERVICE_ROOT / "prism_service" / "main.py"
    src = main_py.read_text(encoding="utf-8")
    assert "warm_polled_route_caches" in src, (
        "main.py's lifespan must import and spawn warm_polled_route_caches "
        "in a background thread, or the primer never runs")
    assert "threading.Thread(target=warm_polled_route_caches" in src


def test_warms_only_projects_in_use(monkeypatch):
    from prism_service.services import warm_start
    from prism_service.services import project_activity
    from prism_service.api import conductor as conductor_api
    from prism_service.api import work as work_api

    monkeypatch.setattr(
        "prism_service.config.list_projects", lambda: ["busy", "idle"])
    monkeypatch.setattr(
        project_activity, "is_in_use",
        lambda pid, *a, **k: pid == "busy")
    monkeypatch.setattr(
        "prism_service.project_context.get_project", lambda pid: None)

    calls = []
    monkeypatch.setattr(
        conductor_api, "state", lambda project: calls.append(("state", project)))
    monkeypatch.setattr(
        work_api, "work_graph", lambda project: calls.append(("graph", project)))

    warm_start.warm_polled_route_caches()

    assert ("state", "busy") in calls
    assert ("graph", "busy") in calls
    assert not any(p == "idle" for _, p in calls), (
        f"warmed an idle project too: {calls}")


def test_one_project_failure_does_not_skip_the_rest(monkeypatch):
    from prism_service.services import warm_start
    from prism_service.services import project_activity
    from prism_service.api import conductor as conductor_api
    from prism_service.api import work as work_api

    monkeypatch.setattr(
        "prism_service.config.list_projects", lambda: ["bad", "good"])
    monkeypatch.setattr(project_activity, "is_in_use", lambda pid, *a, **k: True)
    monkeypatch.setattr(
        "prism_service.project_context.get_project", lambda pid: None)

    calls = []

    def _state(project):
        if project == "bad":
            raise RuntimeError("boom")
        calls.append(project)

    monkeypatch.setattr(conductor_api, "state", _state)
    monkeypatch.setattr(work_api, "work_graph", lambda project: None)

    warm_start.warm_polled_route_caches()  # must not raise

    assert "good" in calls, "a failure on one project must not skip the next"
