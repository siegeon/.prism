"""Dependency-link integrity (stress finding 47d0179a).

POST /api/tasks (and MCP task_create) persisted `dependencies` with NO
validation anywhere - phantom uuids, empty strings, and (via service
callers) self/cyclic dependencies all landed silently, poisoning the
conductor's dependency-aware scheduling. The earlier belief that deps got
a 400 was update_task's generic "no fields to update" (REST PATCH does
not carry the field).

Contract pinned here, mirroring the landed status-enum (16234231,
63d7aba) and parent-link (24ed8027, 123dea5) precedents - validation in
TaskService.create/update so BOTH doors share one set of teeth, riding
the SAME translators (api/tasks.py ValueError -> 422; MCP in-band error):

  * AC-1 phantom dependency ids are 422 on POST, naming the id;
  * AC-2 empty-string dependency ids are 422;
  * AC-3 the MCP door surfaces the in-band error and persists nothing;
  * AC-4 self-dependency is rejected on update;
  * AC-5 dependency cycles (2-node and deeper) are rejected on update;
  * AC-6 the epic-seeding shape (create with dependencies=[existing
    sibling ids], commit ae8abc2's G0-G5 seed) keeps working.
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path))
    # config.py resolves DATA_DIR/PROJECTS_DIR ONCE at import (config.py:14-17),
    # so setenv alone never re-points them and every write leaked into the
    # process's first data dir (finding 33a1397b). Re-point the frozen globals
    # at this test's tmp dir; the source cache-key fix (keyed by resolved data
    # dir) then guarantees a fresh, isolated ProjectContext per test.
    import prism_service.config as _cfg
    monkeypatch.setattr(_cfg, "DATA_DIR", tmp_path)
    monkeypatch.setattr(_cfg, "PROJECTS_DIR", tmp_path / "projects")
    from fastapi.testclient import TestClient

    from prism_service.main import app

    c = TestClient(app)
    # get_project no longer creates on miss (d37193da): create the
    # test project explicitly through the documented affordance.
    c.post("/api/projects", json={"name": "depval"})
    return c


def _mk(client, title="dep probe", **extra):
    r = client.post(
        "/api/tasks", params={"project": "depval"},
        json={"title": title, **extra},
    )
    assert r.status_code == 200, r.text
    return r.json()["task"]["id"]


def _get(client, tid):
    r = client.get(f"/api/tasks/{tid}", params={"project": "depval"})
    assert r.status_code == 200, r.text
    return r.json()["task"]


def _svc(client):
    from prism_service.project_context import get_project
    return get_project("depval").task_svc


# ----------------------------------------------------------------------
# AC-1 / AC-2 - phantom + empty ids are 422 on the REST door
# ----------------------------------------------------------------------


def test_post_phantom_dependency_422(client):
    """AC-1: the filed repro - a phantom uuid persisted silently."""
    phantom = "11111111-2222-3333-4444-555555555555"
    r = client.post(
        "/api/tasks", params={"project": "depval"},
        json={"title": "phantom dep probe", "dependencies": [phantom]},
    )
    assert r.status_code == 422, (
        f"phantom dependency must be 422, got {r.status_code}: {r.text[:200]}"
    )
    assert phantom in str(r.json().get("detail", "")), r.text
    # Nothing persisted.
    listed = client.get("/api/tasks", params={"project": "depval"}).json()["tasks"]
    assert all(t["title"] != "phantom dep probe" for t in listed)


def test_post_empty_dependency_422(client):
    """AC-2: an empty-string dependency id is meaningless - reject it."""
    r = client.post(
        "/api/tasks", params={"project": "depval"},
        json={"title": "empty dep probe", "dependencies": [""]},
    )
    assert r.status_code == 422, (
        f"empty dependency id must be 422, got {r.status_code}: {r.text[:200]}"
    )


# ----------------------------------------------------------------------
# AC-3 - the MCP door shares the service-layer teeth
# ----------------------------------------------------------------------


def test_mcp_task_create_phantom_dep_rejected(client):
    """AC-3: MCP task_create with a phantom dep surfaces the in-band
    machine-legible error (4f76beb9 contract) and persists nothing."""
    r = client.post(
        "/api/agent/tool", params={"project": "depval"},
        json={"name": "task_create", "args": {
            "title": "mcp phantom dep probe",
            "dependencies": ["not-a-real-task-id"],
        }},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("ok") is False, body
    assert "not-a-real-task-id" in str(body.get("error", "")), body
    listed = client.get("/api/tasks", params={"project": "depval"}).json()["tasks"]
    assert all(t["title"] != "mcp phantom dep probe" for t in listed)


# ----------------------------------------------------------------------
# AC-4 / AC-5 - self-dependency and cycles rejected at the service layer
# (REST PATCH does not carry the field; the service is the shared seam)
# ----------------------------------------------------------------------


def test_update_rejects_self_dependency(client):
    """AC-4: a task cannot depend on itself."""
    tid = _mk(client, title="self dep probe")
    svc = _svc(client)
    with pytest.raises(ValueError, match="itself"):
        svc.update(tid, dependencies=[tid])
    assert _get(client, tid)["dependencies"] == []


def test_update_rejects_dependency_cycles(client):
    """AC-5: the filed repro (A->B then B->A) plus a deeper
    A->B->C->A chain - any update that closes a cycle is rejected and
    nothing persists."""
    a = _mk(client, title="cycle A")
    b = _mk(client, title="cycle B")
    c = _mk(client, title="cycle C")
    svc = _svc(client)
    # Legit chain: A depends on B, B depends on C.
    svc.update(a, dependencies=[b])
    svc.update(b, dependencies=[c])
    # Two-node cycle: B -> A (A already -> B).
    with pytest.raises(ValueError, match="[Cc]ycle"):
        svc.update(b, dependencies=[a])
    # Deeper cycle: C -> A (A -> B -> C).
    with pytest.raises(ValueError, match="[Cc]ycle"):
        svc.update(c, dependencies=[a])
    assert _get(client, b)["dependencies"] == [c]
    assert _get(client, c)["dependencies"] == []


# ----------------------------------------------------------------------
# AC-6 - the epic-seeding shape keeps working
# ----------------------------------------------------------------------


def test_epic_seed_shape_still_works(client):
    """AC-6: create with dependencies=[existing sibling ids] - the
    G0-G5 epic seed shape (ae8abc2) - still lands and round-trips."""
    g0 = _mk(client, title="G0 foundations")
    g1 = _mk(client, title="G1 rails", dependencies=[g0])
    g2 = _mk(client, title="G2 engine", dependencies=[g0, g1])
    assert _get(client, g1)["dependencies"] == [g0]
    assert _get(client, g2)["dependencies"] == [g0, g1]
    # And a valid dependency update through the service persists.
    svc = _svc(client)
    svc.update(g2, dependencies=[g1])
    assert _get(client, g2)["dependencies"] == [g1]
