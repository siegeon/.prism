"""Unit cover for the Explore-mesh ego-network endpoint (workstream 4).

GET /api/xref/neighbors returns a token's typed 1-hop neighborhood
{center, neighbors[]} so the SPA can draw a freeform mesh. These tests pin
the load-bearing seams with fakes (mirroring test_traceability_plumbing.py):

  * a task center threads to its linked sessions AND its children (the two
    edge sources the mesh must always have),
  * the neighbor list is deduped and hard-capped to `limit`,
  * every edge source is guarded -- a project missing a service just omits
    those edges instead of raising,
  * a session center threads back to the task it drove and the code it
    touched (session_file_paths).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from prism_service.api import xref

TASK_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
CHILD_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
SESSION_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"


class _NoMemory:
    def get_entry(self, token):
        return None

    def list_domains(self):
        return []

    def list_entries(self, domain):
        return []


class _NoBrain:
    def find_symbol(self, leaf, limit=10):
        return []

    def resolve_indexed_file(self, token):
        return None

    def get_recent_searches(self, limit=200):
        return []


class _FakeTaskSvc:
    """Enough of TaskService for the resolver + task/session gatherers."""

    def __init__(self, n_sessions: int = 1):
        self._center = SimpleNamespace(
            id=TASK_ID, title="Parent epic task", parent_id="",
            dependencies=[], workflow_step="green_gate",
            gate_state="pending",
        )
        self._child = SimpleNamespace(
            id=CHILD_ID, title="A child task", parent_id=TASK_ID,
            dependencies=[], workflow_step="", gate_state="none",
        )
        self._n_sessions = n_sessions

    def get(self, tid):
        if tid == TASK_ID:
            return self._center
        if tid == CHILD_ID:
            return self._child
        return None

    # resolver's _task_lookup prefers get_task when present
    get_task = get

    def sessions_for_task(self, task_id):
        if task_id != TASK_ID:
            return []
        return [
            {"session_id": f"{SESSION_ID[:-2]}{i:02d}", "tokens_used": 1000 * i}
            for i in range(self._n_sessions)
        ]

    def list(self, parent_id=None, **kw):
        return [self._child] if parent_id == TASK_ID else []

    def task_for_session(self, session_id):
        return TASK_ID


class _FakeConductorSvc:
    def get_session_outcomes(self, limit=500):
        return [{"session_id": SESSION_ID}]

    def session_file_paths(self, session_id):
        return {"read": ["src/reader.py"], "modified": ["src/writer.py"]}


def _fake_project(n_sessions: int = 1):
    return SimpleNamespace(
        memory_svc=_NoMemory(),
        brain_svc=_NoBrain(),
        graph_svc=None,
        task_svc=_FakeTaskSvc(n_sessions=n_sessions),
        conductor_svc=_FakeConductorSvc(),
    )


def test_task_center_threads_sessions_and_children():
    """A task doorway reaches its linked session AND its child task -- the two
    edge sources the mesh must always have."""
    res = xref.neighbors_for(TASK_ID, _fake_project(), limit=24)
    assert res["center"]["kind"] == "task"
    assert res["center"]["token"] == TASK_ID
    kinds = {(n["kind"], n["edge"]) for n in res["neighbors"]}
    assert ("session", "driven in") in kinds, res["neighbors"]
    assert ("task", "parent of") in kinds, res["neighbors"]
    # its own pending gate is a neighbor too
    assert any(n["kind"] == "gate" for n in res["neighbors"]), res["neighbors"]
    # every neighbor carries a re-center token + a click-through href
    for n in res["neighbors"]:
        assert n["token"], n
        assert n["href"], n


def test_neighbors_capped_and_deduped():
    """A gush of linked sessions is capped to `limit`; no dupes slip through."""
    res = xref.neighbors_for(TASK_ID, _fake_project(n_sessions=40), limit=10)
    ns = res["neighbors"]
    assert len(ns) == 10, len(ns)
    keys = [(n["kind"], n["href"]) for n in ns]
    assert len(keys) == len(set(keys)), "duplicate neighbors leaked"


def test_session_center_threads_task_and_code():
    """A session doorway reaches the task it drove and the code it touched."""
    res = xref.neighbors_for(SESSION_ID, _fake_project(), limit=24)
    assert res["center"]["kind"] == "session"
    kinds = {(n["kind"], n["edge"]) for n in res["neighbors"]}
    assert ("task", "drove") in kinds, res["neighbors"]
    assert ("code", "modified") in kinds, res["neighbors"]


def test_sources_are_guarded_no_service_no_crash():
    """A project missing task/conductor services yields an unresolved center
    and an empty neighborhood -- never an exception."""
    bare = SimpleNamespace(
        memory_svc=_NoMemory(), brain_svc=_NoBrain(),
        graph_svc=None, task_svc=None, conductor_svc=None,
    )
    res = xref.neighbors_for(TASK_ID, bare, limit=24)
    assert res["neighbors"] == []
    assert res["center"]["token"] == TASK_ID


CONCEPT_ID = "session-task-live-binding"
LINKED_ID = "gate-enforcement-doctrine"


class _MemoryWithConcept:
    """A memory service that resolves one concept by id -- enough for the
    resolver's step-1 concept match to fire so the center is a memory node."""

    def get_entry(self, token):
        if token == CONCEPT_ID:
            return SimpleNamespace(id=CONCEPT_ID, name="Session-task live binding")
        return None

    def list_domains(self):
        return []

    def list_entries(self, domain):
        return []


class _FakeOkfHost:
    """Stands in for OkfHost.graph(): typed concept nodes (type + domain, the
    OKF shape api/okf projects) and one directed wikilink edge."""

    def __init__(self, *_a, **_k):
        pass

    def graph(self):
        return {
            "nodes": [
                {"id": CONCEPT_ID, "title": "Session-task live binding",
                 "type": "decision", "domain": "conductor"},
                {"id": LINKED_ID, "title": "Gate enforcement doctrine",
                 "type": "convention", "domain": "conductor"},
            ],
            "edges": [{"source": CONCEPT_ID, "target": LINKED_ID}],
        }


def test_memory_neighbors_carry_type_and_domain(monkeypatch):
    """A concept center + its wikilinked neighbor both carry OKF concept_type
    and domain, and memory hrefs deep-link to /understand?concept=<id>."""
    from prism_service.services import okf_host as okf_mod
    monkeypatch.setattr(okf_mod, "OkfHost", _FakeOkfHost)

    p = SimpleNamespace(
        memory_svc=_MemoryWithConcept(), brain_svc=_NoBrain(),
        graph_svc=None, task_svc=None, conductor_svc=None,
    )
    res = xref.neighbors_for(CONCEPT_ID, p, limit=24)

    # Center is the focused concept, captioned with its own type + domain.
    assert res["center"]["kind"] == "memory"
    assert res["center"]["concept_type"] == "decision"
    assert res["center"]["domain"] == "conductor"

    mem = [n for n in res["neighbors"] if n["kind"] == "memory"]
    assert mem, res["neighbors"]
    nb = mem[0]
    assert nb["concept_type"] == "convention"
    assert nb["domain"] == "conductor"
    assert nb["edge"] == "links"
    assert nb["href"] == f"/understand?concept={LINKED_ID}"
    assert nb["token"] == LINKED_ID


def test_non_memory_neighbors_omit_concept_metadata():
    """concept_type/domain are memory-only -- a task's session/child neighbors
    must not sprout them."""
    res = xref.neighbors_for(TASK_ID, _fake_project(), limit=24)
    for n in res["neighbors"]:
        assert "concept_type" not in n, n
        assert "domain" not in n, n


def test_neighbors_endpoint_served_by_real_app(monkeypatch):
    """The REAL FastAPI route the SPA calls resolves a task to its mesh."""
    testclient_mod = pytest.importorskip("fastapi.testclient")
    from prism_service.main import app

    monkeypatch.setattr(xref, "get_project", lambda p: _fake_project())
    client = testclient_mod.TestClient(app)
    r = client.get(f"/api/xref/neighbors?token={TASK_ID}&project=prism&limit=24")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["center"]["kind"] == "task"
    assert any(n["kind"] == "session" for n in body["neighbors"])
