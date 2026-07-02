"""Task-boundary validation (stress-loop findings 16234231 + 24ed8027).

16234231: PATCH /api/tasks/{id} accepted ANY string as status ('bananas'
persisted, making the task invisible to every status-keyed surface) and
an unbounded title (10,240 chars accepted, breaking board rendering).
Validation lives in the SERVICE layer (TaskService.create/update) so the
REST API and the MCP task_update path are covered by the same teeth:
api/tasks.py translates the service ValueError into HTTP 422; the MCP
door surfaces it as its in-band machine-legible error.

Exercises the real FastAPI router via TestClient against an isolated
PRISM_DATA_DIR so no real project stores are touched.
"""

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path))
    from fastapi.testclient import TestClient

    from prism_service.main import app

    return TestClient(app)


def _mk(client, title="probe task", **extra):
    r = client.post(
        "/api/tasks", params={"project": "valtest"},
        json={"title": title, **extra},
    )
    assert r.status_code == 200, r.text
    return r.json()["task"]["id"]


def _get(client, tid):
    r = client.get(f"/api/tasks/{tid}", params={"project": "valtest"})
    assert r.status_code == 200, r.text
    return r.json()["task"]


# ----------------------------------------------------------------------
# Status enum (AC-1 / AC-2)
# ----------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["bananas", "zzz_fake", "DONE", "pending "])
def test_patch_invalid_status_422_and_not_persisted(client, bad):
    """AC-1: the filed repro. An off-enum status is a 422 whose detail
    names the allowed values, and nothing persists."""
    tid = _mk(client)
    r = client.patch(
        f"/api/tasks/{tid}", params={"project": "valtest"},
        json={"status": bad},
    )
    assert r.status_code == 422, f"{bad!r}: {r.text}"
    detail = str(r.json().get("detail", ""))
    for allowed in ("pending", "in_progress", "done", "blocked"):
        assert allowed in detail, detail
    assert _get(client, tid)["status"] == "pending"


@pytest.mark.parametrize(
    "good", ["in_progress", "blocked", "done", "cancelled", "deleted", "pending"],
)
def test_patch_valid_statuses_still_work(client, good):
    """AC-2: every canonical lifecycle + terminal soft state still lands,
    and done still auto-stamps completed_at."""
    tid = _mk(client)
    r = client.patch(
        f"/api/tasks/{tid}", params={"project": "valtest"},
        json={"status": good},
    )
    assert r.status_code == 200, f"{good!r}: {r.text}"
    t = _get(client, tid)
    assert t["status"] == good
    if good == "done":
        assert t["completed_at"], "done must stamp completed_at"


# ----------------------------------------------------------------------
# Title cap (AC-3 / AC-5)
# ----------------------------------------------------------------------


def test_title_cap_enforced_on_create_and_patch(client):
    """AC-3: >500-char titles are 422 on POST and PATCH, naming the cap.
    AC-5: exactly 500 chars is accepted on both paths."""
    huge = "T" * 10_240
    r = client.post(
        "/api/tasks", params={"project": "valtest"}, json={"title": huge},
    )
    assert r.status_code == 422, r.text
    assert "500" in str(r.json().get("detail", "")), r.text

    at_cap = "T" * 500
    tid = _mk(client, title=at_cap)
    assert _get(client, tid)["title"] == at_cap

    r = client.patch(
        f"/api/tasks/{tid}", params={"project": "valtest"},
        json={"title": "x" * 501},
    )
    assert r.status_code == 422, r.text
    assert "500" in str(r.json().get("detail", "")), r.text
    assert _get(client, tid)["title"] == at_cap

    r = client.patch(
        f"/api/tasks/{tid}", params={"project": "valtest"},
        json={"title": "renamed within cap"},
    )
    assert r.status_code == 200, r.text
    assert _get(client, tid)["title"] == "renamed within cap"


# ----------------------------------------------------------------------
# The MCP door shares the service-layer teeth (AC-4)
# ----------------------------------------------------------------------


def test_mcp_task_update_invalid_status_rejected(client):
    """AC-4: probed live on the fork — MCP task_update persisted
    status='bananas-mcp-probe'. With service-layer validation the same
    call must surface an in-band error and leave the status untouched."""
    tid = _mk(client)
    r = client.post(
        "/api/agent/tool", params={"project": "valtest"},
        json={"name": "task_update", "args": {"id": tid, "status": "bananas"}},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # The 4f76beb9 contract: in-band dispatch errors ride ok:false+error.
    assert body.get("ok") is False, body
    assert "status" in str(body.get("error", "")), body
    assert _get(client, tid)["status"] == "pending"
# ----------------------------------------------------------------------
# parent_id integrity (task 24ed8027 — self/cycle/phantom parents)
# ----------------------------------------------------------------------


def test_patch_self_parent_422(client):
    """AC-1: a task cannot be its own parent."""
    tid = _mk(client)
    r = client.patch(
        f"/api/tasks/{tid}", params={"project": "valtest"},
        json={"parent_id": tid},
    )
    assert r.status_code == 422, r.text
    assert "own parent" in str(r.json().get("detail", "")), r.text
    assert _get(client, tid)["parent_id"] == ""


def test_phantom_parent_422_on_patch_and_create(client):
    """AC-2: a parent id that does not exist is rejected on PATCH and POST,
    naming the missing id."""
    tid = _mk(client)
    r = client.patch(
        f"/api/tasks/{tid}", params={"project": "valtest"},
        json={"parent_id": "not-a-real-task-id"},
    )
    assert r.status_code == 422, r.text
    assert "not-a-real-task-id" in str(r.json().get("detail", "")), r.text
    assert _get(client, tid)["parent_id"] == ""

    r = client.post(
        "/api/tasks", params={"project": "valtest"},
        json={"title": "orphan probe", "parent_id": "also-not-real"},
    )
    assert r.status_code == 422, r.text
    assert "also-not-real" in str(r.json().get("detail", "")), r.text


def test_ancestor_cycle_422(client):
    """AC-3: the filed repro (A<->B) plus a deeper A->B->C chain — any
    re-parent that closes a cycle through the task is rejected and nothing
    persists."""
    a = _mk(client, title="cycle A")
    b = _mk(client, title="cycle B")
    c = _mk(client, title="cycle C")
    # Legit chain: B under A, C under B.
    assert client.patch(
        f"/api/tasks/{b}", params={"project": "valtest"},
        json={"parent_id": a},
    ).status_code == 200
    assert client.patch(
        f"/api/tasks/{c}", params={"project": "valtest"},
        json={"parent_id": b},
    ).status_code == 200
    # Two-node cycle: A under B.
    r = client.patch(
        f"/api/tasks/{a}", params={"project": "valtest"},
        json={"parent_id": b},
    )
    assert r.status_code == 422, r.text
    assert "cycle" in str(r.json().get("detail", "")).lower(), r.text
    # Deeper cycle: A under C (A -> C -> B -> A).
    r = client.patch(
        f"/api/tasks/{a}", params={"project": "valtest"},
        json={"parent_id": c},
    )
    assert r.status_code == 422, r.text
    assert "cycle" in str(r.json().get("detail", "")).lower(), r.text
    assert _get(client, a)["parent_id"] == ""
    assert _get(client, b)["parent_id"] == a


def test_valid_reparent_still_works(client):
    """AC-4: legit re-parenting (under a root, then back to root) is
    unaffected."""
    root = _mk(client, title="epic root")
    child = _mk(client, title="child")
    r = client.patch(
        f"/api/tasks/{child}", params={"project": "valtest"},
        json={"parent_id": root},
    )
    assert r.status_code == 200, r.text
    assert _get(client, child)["parent_id"] == root
    r = client.patch(
        f"/api/tasks/{child}", params={"project": "valtest"},
        json={"parent_id": ""},
    )
    assert r.status_code == 200, r.text
    assert _get(client, child)["parent_id"] == ""


def test_mcp_task_update_self_parent_rejected(client):
    """AC-5: the MCP door shares the teeth — parent_id=self surfaces the
    in-band machine-legible error (4f76beb9 contract) and persists
    nothing."""
    tid = _mk(client)
    r = client.post(
        "/api/agent/tool", params={"project": "valtest"},
        json={"name": "task_update", "args": {"id": tid, "parent_id": tid}},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("ok") is False, body
    assert "parent" in str(body.get("error", "")), body
    assert _get(client, tid)["parent_id"] == ""
