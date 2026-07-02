"""Conductor routes honor body.project (stress-loop finding 48b965e0).

POST /api/conductor/advance read project ONLY from the query string
(default 'default') and silently dropped a 'project' key in the JSON
body, then reported a REAL task as bare 'unknown task'. Every MCP-shaped
client that passes project in the body (the tasks-API convention) got a
plausible-but-wrong failure against the wrong project. Contract under
test: query param wins; body.project is the fallback when the query is
absent; unknown-task refusals name WHICH project was searched. The
sibling /api/conductor/gate route shares the same resolver.

Exercises the real FastAPI router via TestClient against an isolated
PRISM_DATA_DIR so no real project stores are touched.
"""

import pytest

PROJ = "condbody"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path))
    from fastapi.testclient import TestClient

    from prism_service.main import app

    return TestClient(app)


def _mk(client, title="conductor body probe"):
    r = client.post(
        "/api/tasks", params={"project": PROJ}, json={"title": title},
    )
    assert r.status_code == 200, r.text
    return r.json()["task"]["id"]


def test_advance_honors_body_project(client):
    """AC-1: the filed repro — body {project, task_id} with NO query param
    must advance the real task instead of failing 'unknown task' against
    the 'default' project."""
    tid = _mk(client)
    r = client.post(
        "/api/conductor/advance",
        json={"project": PROJ, "task_id": tid},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("ok") is True, body
    assert body.get("to_step") == "review_previous_notes", body


def test_query_param_wins_over_body(client):
    """AC-2: when both are present the query param stays authoritative —
    the body's project must not redirect the call."""
    tid = _mk(client)
    r = client.post(
        "/api/conductor/advance",
        params={"project": PROJ},
        json={"project": "some-other-project", "task_id": tid},
    )
    assert r.status_code == 200, r.text
    assert r.json().get("ok") is True, r.text


@pytest.mark.parametrize(
    "params,body_project,expect_project",
    [
        ({"project": PROJ}, None, PROJ),        # query-carried
        ({}, PROJ, PROJ),                        # body-carried
        ({}, None, "default"),                   # neither -> default
    ],
    ids=["query", "body", "default"],
)
def test_unknown_task_reason_names_project(
    client, params, body_project, expect_project,
):
    """AC-3: an unknown-task refusal must say WHICH project was searched,
    not a bare 'unknown task' that misdirects debugging."""
    _mk(client)  # ensure the project exists
    payload = {"task_id": "no-such-task-id"}
    if body_project:
        payload["project"] = body_project
    r = client.post("/api/conductor/advance", params=params, json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("ok") is False, body
    reason = str(body.get("reason", ""))
    assert reason != "unknown task", "bare reason must be enriched"
    assert "no-such-task-id" in reason, reason
    assert expect_project in reason, reason


def test_gate_honors_body_project(client):
    """AC-4: /api/conductor/gate shares the fallback — a body-project-only
    call on a real task resolves THAT project (the refusal talks about the
    gate step, never 'unknown task')."""
    tid = _mk(client)
    r = client.post(
        "/api/conductor/gate",
        json={"project": PROJ, "task_id": tid, "action": "approve",
              "reason": "probe"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # The task exists but is not on a gate step -> the in-project refusal.
    assert body.get("ok") is False, body
    assert "not currently on a gate step" in str(body.get("reason", "")), body


def test_gate_unknown_task_reason_names_project(client):
    """AC-4 (reason half): the gate's unknown-task refusal is enriched the
    same way as advance's."""
    _mk(client)
    r = client.post(
        "/api/conductor/gate",
        json={"project": PROJ, "task_id": "ghost-task", "action": "approve",
              "reason": "probe"},
    )
    assert r.status_code == 200, r.text
    reason = str(r.json().get("reason", ""))
    assert "ghost-task" in reason and PROJ in reason, reason
