"""A signal is resolved against the ontology on arrival (task 785bb4ce).

services/signal_resolver.py resolves a signal against SERVICE-LEVEL PRISM
facts -- TaskService rows, the memory service (memory_recall's dispatch
target), the brain engine (brain_search's dispatch target), the actor
resolver -- never the sqlite ontology_store.py tables (a sibling epic is
moving those onto an RDF graph). Called at the end of POST /api/signals
create (best-effort) and re-runnable via POST /api/signals/{id}/resolve.

QueueItem is re-pointed to SIGNALS (ontology_prototype_projection.py); Task
stays its own class so nothing is lost.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


@pytest.fixture
def project():
    """A throwaway project under the suite-pinned PRISM_DATA_DIR (tests/
    conftest.py) -- unique per test so parallel runs never collide."""
    return f"signal-resolve-{uuid.uuid4().hex[:8]}"


def _ctx(project: str):
    from prism_service.project_context import get_project
    return get_project(project)


def _make_signal(project: str, **kw):
    from prism_service.models.signal import Signal
    defaults = dict(project=project, channel="ui", subject="", body="",
                     sender="", channel_ref="")
    defaults.update(kw)
    return Signal(**defaults)


def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import signals as signals_api
    app = FastAPI()
    app.include_router(signals_api.router, prefix="/api/signals")
    return TestClient(app)


# ── related_tasks ────────────────────────────────────────────────────────

def test_related_tasks_matches_by_title_overlap(project):
    from prism_service.services.signal_resolver import resolve

    task = _ctx(project).task_svc.create(title="Fix the channel filter", channel="ui")
    signal = _make_signal(project, subject="about the channel filter")

    matches = resolve(project, signal)
    ids = {t["id"]: t for t in matches["related_tasks"]}
    assert task.id in ids
    assert ids[task.id]["why"]


def test_related_tasks_matches_by_same_channel_ref(project):
    from prism_service.services.signal_resolver import resolve

    task = _ctx(project).task_svc.create(
        title="unrelated title", channel="github", channel_ref="org/repo#42",
    )
    signal = _make_signal(project, channel="github", channel_ref="org/repo#42",
                           subject="totally different subject")

    matches = resolve(project, signal)
    ids = {t["id"]: t for t in matches["related_tasks"]}
    assert task.id in ids
    assert "channel_ref" in ids[task.id]["why"]


# ── channel match ─────────────────────────────────────────────────────────

def test_channel_match_equals_signal_channel(project):
    from prism_service.services.signal_resolver import resolve

    signal = _make_signal(project, channel="slack", subject="hi")
    matches = resolve(project, signal)
    assert matches["channel"]["id"] == "slack"
    assert matches["channel"]["known"] is True


# ── ask classification ──────────────────────────────────────────────────

@pytest.mark.parametrize("subject,expected_kind", [
    ("Please approve the migration plan", "decision"),
    ("Can you review this PR before EOD?", "review"),
    ("Please deliver the report by Friday", "deliverable"),
    ("Can you take a look when you get a chance?", "reply"),
    ("FYI, the deploy finished cleanly", "fyi"),
])
def test_ask_classification_five_samples(project, subject, expected_kind):
    from prism_service.services.signal_resolver import resolve

    signal = _make_signal(project, subject=subject)
    matches = resolve(project, signal)
    assert matches["ask"]["kind"] == expected_kind
    assert matches["ask"]["reason"]


def test_ask_classification_unknown_when_nothing_matches(project):
    from prism_service.services.signal_resolver import resolve

    signal = _make_signal(project, subject="server room temperature log")
    matches = resolve(project, signal)
    assert matches["ask"]["kind"] == "unknown"
    assert matches["ask"]["reason"]


# ── memory / brain matches ─────────────────────────────────────────────────

def test_memory_and_brain_matches_empty_with_reason_on_empty_store(project):
    from prism_service.services.signal_resolver import resolve

    signal = _make_signal(project, subject="something nobody has ever written about",
                           body="truly unindexed content xyzzy123")
    matches = resolve(project, signal)
    assert matches["concepts"] == []
    assert matches["code"] == []
    assert "reasons" in matches
    assert matches["reasons"].get("concepts")
    assert matches["reasons"].get("code")


def test_memory_matches_populated_from_seeded_memory(project):
    from prism_service.services.signal_resolver import resolve

    mem_svc = _ctx(project).memory_svc
    mem_svc.store(
        domain="project", name="channel filter convention",
        description="The channel filter always defaults to open state.",
        type="pattern", classification="tactical", importance=8,
    )

    signal = _make_signal(project, subject="question about the channel filter",
                           body="does it default to open or closed?")
    matches = resolve(project, signal)
    assert matches["concepts"], matches.get("reasons")
    assert any("channel filter" in c["title"] for c in matches["concepts"])
    assert all("score" in c for c in matches["concepts"])


# ── people ───────────────────────────────────────────────────────────────

def test_people_empty_with_reason_for_unknown_sender(project):
    from prism_service.services.signal_resolver import resolve

    signal = _make_signal(project, subject="hi", sender="nobody@nowhere.example")
    matches = resolve(project, signal)
    assert matches["people"] == []
    assert matches["reasons"].get("people")


def test_people_resolves_a_known_actor(project):
    from prism_service.services.signal_resolver import resolve
    from prism_service.services.workspace_service import get_workspace_service

    email = f"resolver-test-{uuid.uuid4().hex[:8]}@example.com"
    get_workspace_service().create_user(email=email, display_name="Resolver Tester")

    signal = _make_signal(project, subject="hi", sender=email)
    matches = resolve(project, signal)
    assert matches["people"]
    assert matches["people"][0]["name"] == "Resolver Tester"
    assert matches["people"][0]["actor_id"]


# ── persistence + wiring ────────────────────────────────────────────────

def test_matches_persisted_on_the_signal(project):
    from prism_service.services.signal_resolver import resolve
    from prism_service.services.signal_store import SignalStore

    store = SignalStore(project)
    signal = store.create(_make_signal(project, subject="ping"))
    matches = resolve(project, signal)
    store.update(signal.id, matches=matches)

    got = store.get(signal.id)
    assert got.matches["ask"]["kind"]
    assert got.matches["resolved_at"]


def test_post_create_triggers_resolve(project):
    from prism_service.services.signal_store import SignalStore

    task = _ctx(project).task_svc.create(title="Fix the channel filter", channel="ui")
    client = _client()
    r = client.post(
        "/api/signals", params={"project": project},
        json={"channel": "ui", "subject": "about the channel filter"},
    )
    assert r.status_code == 200, r.text
    body = r.json()["signal"]
    assert body["matches"], "POST create should resolve best-effort"
    assert any(t["id"] == task.id for t in body["matches"]["related_tasks"])

    got = SignalStore(project).get(body["id"])
    assert got.matches["related_tasks"]


def test_resolve_endpoint_reruns(project):
    from prism_service.services.signal_store import SignalStore

    client = _client()
    r = client.post(
        "/api/signals", params={"project": project},
        json={"channel": "ui", "subject": "about the channel filter"},
    )
    signal_id = r.json()["signal"]["id"]
    before = SignalStore(project).get(signal_id)
    assert before.matches["related_tasks"] == []

    task = _ctx(project).task_svc.create(title="Fix the channel filter", channel="ui")

    r2 = client.post(f"/api/signals/{signal_id}/resolve", params={"project": project})
    assert r2.status_code == 200, r2.text
    after = r2.json()["signal"]
    assert any(t["id"] == task.id for t in after["matches"]["related_tasks"])

    persisted = SignalStore(project).get(signal_id)
    assert any(t["id"] == task.id for t in persisted.matches["related_tasks"])


def test_resolve_endpoint_404_for_unknown_signal(project):
    client = _client()
    r = client.post("/api/signals/does-not-exist/resolve", params={"project": project})
    assert r.status_code == 404


# ── ontology projection: QueueItem <- signals, Task stays its own class ───

def test_queue_item_projects_from_signals_and_task_class_exists(project):
    from prism_service.services import ontology_prototype_projection as proj
    from prism_service.services.ontology_store import OntologyStore
    from prism_service.services.signal_store import SignalStore

    _ctx(project).task_svc.create(title="a real task", channel="ui")
    _ctx(project).task_svc.create(title="another task", channel="mcp")

    sig_store = SignalStore(project)
    sig_store.create(_make_signal(project, subject="signal one"))
    sig_store.create(_make_signal(project, subject="signal two"))
    sig_store.create(_make_signal(project, subject="signal three"))

    # Re-anchored for task b1971944 ("a firing rule becomes a decision on
    # the Queue"): rebuild()'s own OntologyGraph.rebuild() -> validate()
    # pass may now post NEW "ontology"-channel signals for any rule this
    # project's own tasks/docs happen to violate, AFTER this project's
    # QueueItem projection already took its snapshot via gather() at the
    # top of rebuild() -- so the count this pins is the one BEFORE
    # rebuild runs, never a later sig_store.list() call, which could see
    # those new signals too.
    signal_count_before_rebuild = len(sig_store.list())

    proj.rebuild(project)

    store = OntologyStore(project)
    classes = {c["id"]: c for c in store.list_classes()}
    assert classes["QueueItem"]["instance_count"] == signal_count_before_rebuild
    assert classes["QueueItem"]["source"] == "signals"

    assert "Task" in classes
    assert classes["Task"]["instance_count"] == 2
    task_labels = {i["label"] for i in store.list_instances("Task", limit=50)}
    assert task_labels == {"a real task", "another task"}
    store.close()
