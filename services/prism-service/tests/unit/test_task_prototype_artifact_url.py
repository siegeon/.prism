"""Task record carries its prototype's live URL (task 3c1a326b).

GET /api/tasks/{id}?project=prism already computes has_prototype (tasks.py:
533-540, prototype_file(task_id).exists()) but never turns that bool into a
GET-able URL string. An API/agent consumer with no repo access has no way to
discover the curl-able /api/tasks/{id}/prototype route from the wire payload
alone. This slice adds a derived `artifact_url` field alongside the existing
`has_prototype`, mirroring the `mirror_url` derived-field pattern already in
list_tasks' `fields=` projection (tasks.py:173-208).

  AC-1  GET /api/tasks/{id} returns non-empty artifact_url when a prototype
        file exists on disk for that task.
  AC-2  GET /api/tasks/{id} returns null/absent artifact_url when no
        prototype file exists.
  AC-3  the URL is fetchable: GET it and get 200 + text/html.
  AC-4  has_prototype stays present, unrenamed, unchanged in meaning.
  AC-5  artifact_url is selectable via GET /api/tasks?fields=... exactly
        like mirror_url (second special-cased derived field).

FAILS TODAY because get_task's response dict (tasks.py:496-541) has no
`artifact_url` key at all, and list_tasks' fields projection loop
(tasks.py:199-207) only special-cases `mirror_url` - requesting
`artifact_url` via `fields=` returns None (falls through to the generic
`getattr(t, f, None)`, and Task has no such attribute).
"""
from __future__ import annotations

import sys
import types
import uuid
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _mk_task(**over):
    from prism_service.models.task import Task
    base = dict(
        id="t-1", title="A task", description="", status="pending",
        priority=5, assigned_agent="", updated_at="2026-08-07T00:00:00Z",
        workflow_step="implement", gate_state="none", parent_id="",
        tags=[],
    )
    base.update(over)
    return Task(**base)


class _Svc:
    """Minimal task_svc stand-in carrying only what get_task/list_tasks
    read - mirrors test_task_list_projection.py's _client() convention."""

    def __init__(self, tasks):
        self._tasks = {t.id: t for t in tasks}

    def get(self, task_id):
        return self._tasks.get(task_id)

    def history(self, task_id):
        return []

    def sessions_for_task(self, task_id):
        return []

    def list(self, status=None, assigned_agent=None, tag=None,
              story_file=None, parent_id=None, id=None):
        rows = list(self._tasks.values())
        if id is not None:
            rows = [t for t in rows if t.id == id]
        return rows


def _client(tasks, monkeypatch, data_dir):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import tasks as tasks_api

    monkeypatch.setenv("PRISM_DATA_DIR", str(data_dir))
    monkeypatch.setattr(tasks_api, "get_project",
                         lambda p: types.SimpleNamespace(task_svc=_Svc(tasks)))

    app = FastAPI()
    app.include_router(tasks_api.router, prefix="/api/tasks")
    return TestClient(app)


def _write_prototype(task_id: str, body: bytes = b"<html>mock</html>"):
    from prism_service import data_dir as dd
    p = dd.prototype_file(task_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(body)
    return p


# ---------------------------------------------------------------------------
# AC-1 / AC-4 - detail route surfaces artifact_url alongside has_prototype
# ---------------------------------------------------------------------------

def test_detail_returns_artifact_url_when_prototype_file_exists(tmp_path, monkeypatch):
    task_id = "d09bee0b-e0ea-4b4e-8865-b806007dec0e"
    task = _mk_task(id=task_id, title="Has a prototype")
    client = _client([task], monkeypatch, tmp_path / "data")
    _write_prototype(task_id)

    r = client.get(f"/api/tasks/{task_id}", params={"project": "prism"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["has_prototype"] is True, (
        "has_prototype must remain present and unchanged in meaning")
    assert body.get("artifact_url"), (
        f"expected a non-empty artifact_url for a task with a prototype "
        f"file on disk; got {body.get('artifact_url')!r}")
    assert body["artifact_url"] == f"/api/tasks/{task_id}/prototype", (
        f"artifact_url must point at the existing prototype route; got "
        f"{body['artifact_url']!r}")


# ---------------------------------------------------------------------------
# AC-2 - no prototype file -> null/absent artifact_url
# ---------------------------------------------------------------------------

def test_detail_omits_artifact_url_when_no_prototype_file(tmp_path, monkeypatch):
    task_id = "no-proto-task"
    task = _mk_task(id=task_id, title="No prototype")
    client = _client([task], monkeypatch, tmp_path / "data")
    # Deliberately never write a prototype file for this task id.

    r = client.get(f"/api/tasks/{task_id}", params={"project": "prism"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["has_prototype"] is False
    assert not body.get("artifact_url"), (
        f"expected null/absent artifact_url with no prototype file; got "
        f"{body.get('artifact_url')!r}")


# ---------------------------------------------------------------------------
# AC-3 - the returned URL is actually GET-able and serves the html
# ---------------------------------------------------------------------------

def test_returned_artifact_url_is_fetchable_and_serves_html(tmp_path, monkeypatch):
    task_id = "fetchable-task"
    task = _mk_task(id=task_id, title="Fetch me")
    client = _client([task], monkeypatch, tmp_path / "data")
    _write_prototype(task_id, b"<html><body>hi</body></html>")

    detail = client.get(f"/api/tasks/{task_id}", params={"project": "prism"})
    assert detail.status_code == 200, detail.text
    url = detail.json()["artifact_url"]
    assert url, "artifact_url must be populated to fetch it"

    fetched = client.get(url, params={"project": "prism"})
    assert fetched.status_code == 200, fetched.text
    assert fetched.headers["content-type"].startswith("text/html"), (
        f"expected text/html; got {fetched.headers['content-type']!r}")


# ---------------------------------------------------------------------------
# AC-5 - selectable via the ?fields= projection like mirror_url
# ---------------------------------------------------------------------------

def test_artifact_url_selectable_via_fields_projection(tmp_path, monkeypatch):
    task_id = "listed-task"
    task = _mk_task(id=task_id, title="On the board")
    client = _client([task], monkeypatch, tmp_path / "data")
    _write_prototype(task_id)

    r = client.get("/api/tasks", params={
        "project": "prism", "fields": "id,artifact_url"})
    assert r.status_code == 200, r.text
    rows = r.json()["tasks"]
    assert len(rows) == 1
    row = rows[0]
    assert set(row.keys()) == {"id", "artifact_url"}, row
    assert row["artifact_url"] == f"/api/tasks/{task_id}/prototype", row


def test_artifact_url_absent_via_fields_projection_without_prototype(tmp_path, monkeypatch):
    task_id = "listed-no-proto"
    task = _mk_task(id=task_id, title="No mock yet")
    client = _client([task], monkeypatch, tmp_path / "data")

    r = client.get("/api/tasks", params={
        "project": "prism", "fields": "id,artifact_url"})
    assert r.status_code == 200, r.text
    row = r.json()["tasks"][0]
    assert not row.get("artifact_url"), row
