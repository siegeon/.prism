"""The Queue shows signals and makes one a task on your word (task 01d05bff).

Owner's model (mx-0889e4): the Queue is where signals arrive over their
channel; a signal becomes a task ONLY when the owner types what to do and
clicks. Work (/tasks) is tasks and already exists -- the Queue is not a view
of it. Pins: the nav entry, the route, QueuePage's promote/drop affordances
(source-scan, no JS runner -- convention in
test_conductor_page_animated_cleanup_ui.py), and the promote/drop API
contract built on the signal-intake slice (task a6858911).
"""

from __future__ import annotations

import re
import sys
import uuid
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_WEB = _SERVICE_ROOT / "prism_service" / "web" / "src"


def _sidebar() -> str:
    return (_WEB / "components" / "Sidebar.tsx").read_text(encoding="utf-8")


def _app() -> str:
    return (_WEB / "App.tsx").read_text(encoding="utf-8")


def _queue_page() -> str:
    return (_WEB / "pages" / "QueuePage.tsx").read_text(encoding="utf-8")


# ── Sidebar ──────────────────────────────────────────────────────────────

def test_queue_is_first_activity_item_and_inbox_enabled_is_gone():
    src = _sidebar()
    assert "INBOX_ENABLED" not in src, (
        "the temporary INBOX_ENABLED gate must be retired now that Queue "
        "is the real surface")
    m = re.search(r'label:\s*"Activity".*?items:\s*\[(.*?)\n\s*\],', src, re.DOTALL)
    assert m, "could not locate the Activity section's items array"
    items_src = m.group(1)
    queue_at = items_src.find('to: "/queue"')
    work_at = items_src.find('to: "/tasks"')
    assert queue_at != -1, "no /queue item in the Activity section"
    assert work_at != -1, "no /tasks (Work) item in the Activity section"
    assert queue_at < work_at, "Queue must sit above Work in Activity"
    first_to = re.search(r'to:\s*"([^"]+)"', items_src)
    assert first_to and first_to.group(1) == "/queue", (
        f"Queue is not the first Activity item: {items_src!r}")
    item = re.search(r'\{[^{}]*to:\s*"/queue"[^{}]*\}', items_src)
    assert item and re.search(r'label:\s*"Queue"', item.group(0))


# ── App routing ──────────────────────────────────────────────────────────

def test_queue_route_mounts_queuepage():
    src = _app()
    route = re.search(r'<Route\s+path="/queue"[^>]*element=\{([^}]*)\}[^>]*/>', src)
    assert route, 'no <Route path="/queue"> is registered'
    assert "QueuePage" in route.group(1)


def test_inbox_redirects_to_queue():
    src = _app()
    route = re.search(r'<Route\s+path="/inbox"[^>]*element=\{([^}]*)\}[^>]*/>', src)
    assert route, 'no <Route path="/inbox"> is registered'
    assert re.search(r'<Navigate\s+to="/queue"\s+replace\s*/>', route.group(1)), (
        f"/inbox must redirect to /queue: {route.group(1)!r}")


# ── QueuePage affordances ───────────────────────────────────────────────

def test_queue_page_lists_open_signals_by_channel():
    src = _queue_page()
    assert "/api/signals" in src and "state=open" in src
    assert "Lozenge" in src, "channel grouping should reuse the Work board's Lozenge chip"


def test_queue_page_has_a_make_it_a_task_affordance_bound_to_promote():
    src = _queue_page()
    assert "what should happen?" in src, "no prefilled-title text input"
    assert re.search(r"<select[^>]*>", src), "no workflow <select>"
    assert '"implement"' in src and '"triage"' in src
    assert "Make it a task" in src
    assert "/promote" in src, "the affordance must post to /promote"


def test_queue_page_has_a_drop_affordance_bound_to_drop():
    src = _queue_page()
    assert "Drop" in src
    assert "/drop" in src, "the drop affordance must post to /drop"


def test_queue_page_shows_a_became_task_link():
    src = _queue_page()
    assert "became task" in src
    assert "became_task" in src
    assert "/tasks/${" in src, "the became-task row must link into /tasks/<id>"


def test_queue_page_empty_state():
    src = _queue_page()
    assert "Nothing in the queue" in src


# ── Promote / drop API ──────────────────────────────────────────────────

@pytest.fixture
def project():
    """A throwaway project name under the suite-pinned PRISM_DATA_DIR
    (tests/conftest.py) -- unique per test so parallel runs never collide."""
    return f"queue-test-{uuid.uuid4().hex[:8]}"


def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import signals as signals_api
    app = FastAPI()
    app.include_router(signals_api.router, prefix="/api/signals")
    return TestClient(app)


def _post_signal(client, project, **overrides):
    body = {"channel": "slack", "channel_ref": "C1/2", "subject": "ping",
             "body": "please look at this", "sender": "alice"}
    body.update(overrides)
    r = client.post("/api/signals", params={"project": project}, json=body)
    assert r.status_code == 200, r.text
    return r.json()["signal"]


def test_promote_creates_a_task_with_the_typed_title_channel_and_tags(project):
    client = _client()
    signal = _post_signal(client, project)

    r = client.post(f"/api/signals/{signal['id']}/promote",
                     params={"project": project}, json={"title": "go look at this"})
    assert r.status_code == 200, r.text
    body = r.json()
    task = body["task"]
    assert task["title"] == "go look at this"
    assert task["channel"] == "slack"
    assert task["channel_ref"] == "C1/2"
    assert task["workflow"] == "triage", "default workflow must be triage"
    assert "queue" in task["tags"] and "slack" in task["tags"]

    assert body["signal"]["state"] == "became_task"
    assert body["signal"]["task_id"] == task["id"]

    from prism_service.project_context import get_project
    got = get_project(project).task_svc.get(task["id"])
    assert got is not None and got.title == "go look at this"


def test_promote_honours_an_explicit_workflow(project):
    client = _client()
    signal = _post_signal(client, project)
    r = client.post(f"/api/signals/{signal['id']}/promote",
                     params={"project": project},
                     json={"title": "x", "workflow": "implement"})
    assert r.status_code == 200, r.text
    assert r.json()["task"]["workflow"] == "implement"


def test_second_promote_is_refused(project):
    client = _client()
    signal = _post_signal(client, project)
    r1 = client.post(f"/api/signals/{signal['id']}/promote",
                      params={"project": project}, json={"title": "first"})
    assert r1.status_code == 200, r1.text
    r2 = client.post(f"/api/signals/{signal['id']}/promote",
                      params={"project": project}, json={"title": "second"})
    assert r2.status_code == 409, r2.text


def test_promote_unknown_signal_is_404(project):
    client = _client()
    r = client.post("/api/signals/does-not-exist/promote",
                     params={"project": project}, json={"title": "x"})
    assert r.status_code == 404


def test_drop_sets_dropped_with_reason(project):
    client = _client()
    signal = _post_signal(client, project)
    r = client.post(f"/api/signals/{signal['id']}/drop",
                     params={"project": project}, json={"reason": "duplicate"})
    assert r.status_code == 200, r.text
    assert r.json()["signal"]["state"] == "dropped"
    assert r.json()["signal"]["drop_reason"] == "duplicate"


def test_promote_after_drop_is_refused(project):
    client = _client()
    signal = _post_signal(client, project)
    r1 = client.post(f"/api/signals/{signal['id']}/drop",
                      params={"project": project}, json={"reason": "dup"})
    assert r1.status_code == 200, r1.text
    r2 = client.post(f"/api/signals/{signal['id']}/promote",
                      params={"project": project}, json={"title": "x"})
    assert r2.status_code == 409, r2.text
