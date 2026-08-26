"""The Inbox surface a person actually clicks (epic 0784729f).

WHY THIS FILE EXISTS, and it is a correction. The first pass at this epic
shipped an Actor resolver, a collaboration adapter and a diagnostics
endpoint, reported 1521 green tests, and matched NOTHING in the prototype
the owner approved. The AC oracle I had written was `curl
/api/collaboration/surfaces` — an API, not an affordance. The approved mock
specifies an INBOX nav entry: "what needs you, and what happened for you",
deliberately NOT called My Tasks because Tasks already carries a My Tasks /
Team toggle (web/src/pages/TasksPage.tsx:54).

So these assertions pin THE ENTRY POINT A HUMAN USES — the nav item, its
position, and the route it lands on — plus the one behavioural invariant the
mock states outright: "Every message is conductor state, projected: a
message never mints a task."

The PRISM SPA has no JS test runner, so UI contracts are pinned by asserting
the ACTUAL TSX source (the convention documented in
tests/unit/test_conductor_page_animated_cleanup_ui.py). Where that is done
here it parses structure, never a fixed character window around a match, so
an explanatory comment cannot satisfy an assertion.

RE-ANCHORED (task 01d05bff, 2026-08-25): the Inbox nav entry/route are
retired outright in favour of Queue (owner's model, mx-0889e4 — signals
arrive in the Queue; a signal becomes a task only on the owner's word,
never a bare conductor-state projection). The three invariants that survive
a person can reach the surface from the nav, it sits above Tasks, and the
route lands on a real page — are re-pointed at Queue below with a `queue_`
prefix. The My-Tasks-naming and per-viewer-identity checks were specific to
the old Inbox mock and are retired outright (Queue is a shared open-signal
list, not a personal inbox). The backend /api/inbox tests below are
UNTOUCHED — api/inbox.py itself is out of this task's scope and keeps
serving that endpoint; only its UI reachability is gone.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_WEB = _SERVICE_ROOT / "prism_service" / "web" / "src"


def _sidebar() -> str:
    return (_WEB / "components" / "Sidebar.tsx").read_text(encoding="utf-8")


def test_a_person_can_reach_the_queue_from_the_nav():
    """RE-ANCHORED (task 01d05bff): Inbox is retired outright, not hidden
    behind a flag — Queue is the real surface a person clicks now. What
    survives is the original invariant: a real, labelled nav item points at
    the route (e139295d's lesson stands — source presence isn't reachability
    on its own, but this item is never gated off, unlike the old flag)."""
    src = _sidebar()
    item = re.search(r'\{[^{}]*to:\s*"/queue"[^{}]*\}', src)
    assert item, 'no sidebar item points at "/queue"'
    assert re.search(r'label:\s*"Queue"', item.group(0)), (
        f"the /queue nav item is not labelled Queue: {item.group(0)}")


def test_the_queue_sits_above_tasks_in_activity():
    """RE-ANCHORED (task 01d05bff): the ordering decision that Inbox
    originally protected — the thing that needs you sits above the full
    list you go query — now applies to Queue. RE-ANCHORED again for the
    Work -> Tasks nav rename: the label text changed, the ordering
    invariant did not."""
    src = _sidebar()
    queue_at = src.find('to: "/queue"')
    tasks_at = src.find('to: "/tasks", label: "Tasks"')
    assert queue_at != -1 and tasks_at != -1, "both nav entries must exist"
    assert queue_at < tasks_at, "Queue's source position must precede Tasks's"


def test_the_queue_route_lands_on_a_real_page():
    """RE-ANCHORED (task 01d05bff): /inbox now redirects to /queue
    (test_queue_makes_a_task_on_your_word.py owns that redirect contract) —
    this pins that /queue itself mounts a real page, the same invariant
    this test originally protected for /inbox."""
    src = (_WEB / "App.tsx").read_text(encoding="utf-8")
    route = re.search(r'<Route\s+path="/queue"[^>]*element=\{([^}]*)\}[^>]*/>', src)
    assert route, "no <Route path=\"/queue\"> is registered"
    assert "QueuePage" in route.group(1), (
        f"/queue's route registration does not reference QueuePage: {route.group(1)}")


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("PRISM_AUTH_MODE", "local")
    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path / "data"))
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from prism_service.api import inbox as inbox_api

    api = FastAPI()
    api.include_router(inbox_api.router, prefix="/api/inbox")
    return TestClient(api)


def test_a_pending_gate_is_what_needs_you(tmp_path, monkeypatch):
    """The mock's headline item: an agent finished work and cannot sign its
    own gate, so it lands in your Inbox carrying a deep link into the task
    page where the gate card and Approve already live."""
    client = _client(tmp_path, monkeypatch)
    from prism_service.project_context import get_project

    svc = get_project("default").task_svc
    task = svc.create(title="a task parked at a gate")
    svc.update(task.id, workflow_step="green_gate", gate_state="pending",
               status="in_progress")

    body = client.get("/api/inbox?project=default").json()
    hit = [i for i in body["needs_you"] if i["task_id"] == task.id]
    assert hit, f"a pending gate is not in needs_you: {body}"
    assert hit[0]["url"] == f"/tasks/{task.id}", (
        "the item must deep-link into the existing task page")


def test_reading_the_inbox_never_mints_work(tmp_path, monkeypatch):
    """BEHAVIOURAL, not a source scan. The mock states the invariant
    outright: 'Every message is conductor state, projected: a message never
    mints a task.' Reading an inbox must not create or advance anything —
    checked by comparing real state before and after, so it keeps holding
    however the endpoint is later reimplemented."""
    client = _client(tmp_path, monkeypatch)
    from prism_service.project_context import get_project

    svc = get_project("default").task_svc
    task = svc.create(title="a task parked at a gate")
    svc.update(task.id, workflow_step="green_gate", gate_state="pending",
               status="in_progress")

    before = [(t.id, t.status, t.workflow_step) for t in svc.list()]
    for _ in range(3):
        assert client.get("/api/inbox?project=default").status_code == 200
    after = [(t.id, t.status, t.workflow_step) for t in svc.list()]
    assert after == before, "reading the inbox changed task state"


def test_the_real_app_exposes_the_inbox_route(quiet_boot):
    """The tests above mount the router into a bare FastAPI, which proves the
    handler works and proves NOTHING about whether the app wires it — the
    same shape as an adapter that only exists when a test injects it. This
    asserts the assembled application, so forgetting the include_router in
    api/__init__.py fails here rather than in a browser.

    `quiet_boot` (shared, tests/conftest.py) silences the real lifespan's
    background workers so booting the assembled app here doesn't leak
    daemon threads into the rest of the pytest process (task 0784729f)."""
    from fastapi.testclient import TestClient

    from prism_service.main import app

    # Through TestClient, not app.routes: the app mounts its routers during
    # STARTUP, so an import-time scan of app.routes reports nothing and would
    # fail on a correctly-wired app. Ask it the way a browser does.
    with TestClient(app) as client:
        r = client.get("/api/inbox?project=default")
    assert r.status_code != 404, (
        "the assembled app does not serve /api/inbox - the include_router in "
        "api/__init__.py is missing")


def test_a_subtask_gate_never_reaches_the_inbox(tmp_path, monkeypatch):
    """OWNER RULE (2026-08-06): subtasks are the driver's business and are
    "beneath the notice of the human" — they must never be surfaced as
    something the owner has to act on. The Inbox is the surface where that
    rule is most easily broken, because a child parked at a gate looks
    exactly like a root parked at a gate. Only ROOT tasks reach it; a
    child's gate is resolved by whoever is driving it."""
    client = _client(tmp_path, monkeypatch)
    from prism_service.project_context import get_project

    svc = get_project("default").task_svc
    epic = svc.create(title="an epic the owner watches")
    child = svc.create(title="a slice the owner should never see", parent_id=epic.id)
    for t in (epic, child):
        svc.update(t.id, workflow_step="green_gate", gate_state="pending",
                   status="in_progress")

    body = client.get("/api/inbox?project=default").json()
    ids = {i["task_id"] for i in body["needs_you"]}
    assert epic.id in ids, "a root task's gate must still reach the inbox"
    assert child.id not in ids, (
        "a subtask's gate reached the owner's inbox — subtasks are the "
        "driver's to resolve")
