"""Task fix/polling: GET /api/changes -- the ONE tiny endpoint the SPA's
shared poll layer watches to decide whether any page-level data query
needs to refetch at all.

Observed live (owner 2026-09-13): an idle Workflows tab issued ~10
independent requests every 1-2s (version, staleness, workflows/live,
conductor/state, consolidation/workers, tasks, tasks/stranded, jobs,
sse/work), because every page/hook polled its OWN endpoint on its OWN
fixed interval regardless of whether anything had actually changed.

/api/changes answers a single bumped counter (backed by
services/wakeups.py's existing signal bus -- the same mutation points
that already wake standing workers) so lib/useChanges.ts can run ONE
1s poll per tab and every other data query can gate its own refetch on
"did the counter move" instead of guessing on a timer.

Deliberately NOT a new source of truth: the counter is the newest
signal timestamp visible to the caller's project (or globally with no
project), read straight off wakeups._LAST -- no new entity, no new
storage.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from prism_service.main import app
from prism_service.services import wakeups


def setup_function(_fn) -> None:
    wakeups._reset_for_tests()


def _get(client: TestClient, project: str | None = None) -> dict:
    path = "/api/changes"
    if project is not None:
        path += f"?project={project}"
    resp = client.get(path)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _isolated_changes_client() -> TestClient:
    """A bare app with ONLY the /api/changes route, no lifespan -- so the
    real app's standing background workers (task_runner, and since the
    SSE follow-up, services/system_activity.py's own record()/pass_()
    signalling "activity" on every real pass) can never contaminate a
    baseline that expects EXACTLY zero. `with TestClient(app)` mounts the
    full app including its lifespan; the two tests below need a process
    with no other signaller in it at all, same shape as
    test_work_bus_publishers.py's _heartbeat_client()."""
    from fastapi import FastAPI
    from prism_service.api.changes import router as changes_router
    bare = FastAPI()
    bare.include_router(changes_router, prefix="/api/changes")
    return TestClient(bare)


def test_changes_returns_zero_counter_when_nothing_has_ever_signalled():
    body = _get(_isolated_changes_client(), project="prism")
    assert body["counter"] == 0.0
    assert "at" in body


def test_changes_counter_bumps_after_a_matching_signal():
    with TestClient(app) as client:
        before = _get(client, project="prism")
        wakeups.signal("task_changed", "prism")
        after = _get(client, project="prism")
    assert after["counter"] > before["counter"]


def test_changes_counter_bumps_on_a_wildcard_signal_for_any_project():
    with TestClient(app) as client:
        before = _get(client, project="prism")
        wakeups.signal("shipped", "*")
        after = _get(client, project="prism")
    assert after["counter"] > before["counter"]


def test_changes_counter_ignores_a_signal_for_a_different_project():
    client = _isolated_changes_client()
    wakeups.signal("task_changed", "some-other-project")
    body = _get(client, project="prism")
    assert body["counter"] == 0.0


def test_changes_with_no_project_sees_every_project():
    with TestClient(app) as client:
        wakeups.signal("task_changed", "some-other-project")
        body = _get(client)
    assert body["counter"] > 0.0


def test_changes_response_is_tiny_two_fields_only():
    # The whole point is a cheap poll target -- no payload bloat creeping
    # in as more signal kinds are added.
    with TestClient(app) as client:
        body = _get(client, project="prism")
    assert set(body.keys()) == {"counter", "at"}
