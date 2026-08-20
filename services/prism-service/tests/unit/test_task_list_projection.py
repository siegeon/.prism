"""The board ships only what it renders (task 842248bd).

MEASURED on the live daemon: /api/tasks ships 1.84 MB over 356 rows when the
Work board renders six columns; task_service.py's list() has no `fields`
param at all (the projection support lives one layer up, in the MCP tool
handler, never wired into this REST route — services/prism-service/
prism_service/api/tasks.py:109-120 calls `_svc(project).list()` with zero
arguments today). A task detail page separately re-fetches the WHOLE board
just to filter client-side by parent_id for its children checklist
(TaskDetailPage.tsx:933). /api/version ships its entire ~262 KB changelog on
every 15s poll that only ever reads `.version`.

FAILS TODAY because:
  - GET /api/tasks has no `fields` or `parent_id` query param at all — passing
    either changes nothing (an unrecognized query param is silently dropped).
  - GET /api/version always returns the full `notes` string.
  - TasksPage.tsx's board fetch has no `fields=` in its URL, and mirrorOf()
    regexes `item.description`, never `item.mirror_url` (that field doesn't
    exist yet).
  - TaskDetailPage.tsx's children fetch has no `parent_id=`/`fields=` params.
  - Sidebar.tsx's tooltip has no notes opt-in call.

The SPA ships no JS test runner, so the TSX-facing acceptance criteria (AC-5,
AC-6, AC-8) are pinned by asserting the ACTUAL TSX SOURCE, same pattern as
the other *_ui.py suites — the rendered wiring, never a comment near it.
AC-9 (search still matches id/title/tag) is already pinned by
test_work_search_filter_ui.py and is intentionally not duplicated here.
"""

from __future__ import annotations

import re
import sys
import types
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_WEB = _SERVICE_ROOT / "prism_service" / "web" / "src"


# ---------------------------------------------------------------------------
# Backend: GET /api/tasks fields + parent_id projection (AC-1, AC-2, AC-3, AC-4)
# ---------------------------------------------------------------------------

def _mk_task(**over):
    from prism_service.models.task import Task
    base = dict(
        id="t-root", title="Root task", description="", status="pending",
        priority=5, assigned_agent="alice", updated_at="2026-07-30T00:00:00Z",
        workflow_step="implement", gate_state="none", parent_id="",
        tags=["ui"],
    )
    base.update(over)
    return Task(**base)


def _client(tasks, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import tasks as tasks_api

    class _Svc:
        def list(self, status=None, assigned_agent=None, tag=None,
                  story_file=None, parent_id=None, id=None):
            rows = tasks
            if parent_id is not None:
                rows = [t for t in rows if t.parent_id == parent_id]
            if id is not None:
                rows = [t for t in rows if t.id == id]
            return rows

    monkeypatch.setattr(tasks_api, "get_project",
                         lambda p: types.SimpleNamespace(task_svc=_Svc()))

    app = FastAPI()
    app.include_router(tasks_api.router, prefix="/api/tasks")
    return TestClient(app)


def test_fields_projects_each_row_to_only_the_requested_keys(monkeypatch):
    tasks = [
        _mk_task(id="t-1", title="One"),
        _mk_task(id="t-2", title="Two", description="a" * 500),
    ]
    client = _client(tasks, monkeypatch)
    r = client.get("/api/tasks", params={"fields": "id,title,status"})
    assert r.status_code == 200, r.text
    rows = r.json()["tasks"]
    assert len(rows) == 2
    for row in rows:
        assert set(row.keys()) == {"id", "title", "status"}, row


def test_parent_id_scopes_to_direct_children_only(monkeypatch):
    tasks = [
        _mk_task(id="epic", title="Epic", parent_id=""),
        _mk_task(id="child-1", title="Child 1", parent_id="epic"),
        _mk_task(id="other", title="Unrelated", parent_id=""),
    ]
    client = _client(tasks, monkeypatch)
    r = client.get("/api/tasks", params={"parent_id": "epic"})
    assert r.status_code == 200, r.text
    rows = r.json()["tasks"]
    assert [row["id"] for row in rows] == ["child-1"]


def test_parent_id_and_fields_combine(monkeypatch):
    tasks = [
        _mk_task(id="epic", title="Epic", parent_id=""),
        _mk_task(id="child-1", title="Child 1", parent_id="epic", priority=9),
    ]
    client = _client(tasks, monkeypatch)
    r = client.get("/api/tasks", params={"parent_id": "epic", "fields": "id,title"})
    assert r.status_code == 200, r.text
    rows = r.json()["tasks"]
    assert rows == [{"id": "child-1", "title": "Child 1"}]


def test_mirror_url_derived_from_description_without_leaking_raw_description(monkeypatch):
    mirrored = _mk_task(
        id="t-mirror", title="Imported issue", tags=["github", "external"],
        description=("Fix the thing.\n\nMirrored from github owner/repo#42."
                      "\nhttps://github.com/owner/repo/issues/42"),
    )
    client = _client([mirrored], monkeypatch)
    r = client.get("/api/tasks", params={"fields": "id,mirror_url"})
    assert r.status_code == 200, r.text
    row = r.json()["tasks"][0]
    assert row["mirror_url"] == "https://github.com/owner/repo/issues/42"
    assert "description" not in row


def test_default_unfiltered_call_is_unchanged_full_rows(monkeypatch):
    # Backward compat: no fields/parent_id -> the full board, exactly like
    # today (other pages may still rely on this shape).
    tasks = [_mk_task(id="t-1", description="the full body")]
    client = _client(tasks, monkeypatch)
    r = client.get("/api/tasks")
    assert r.status_code == 200, r.text
    row = r.json()["tasks"][0]
    assert row["id"] == "t-1"
    assert row["description"] == "the full body", (
        "an unscoped, unprojected call must still return full task rows")


# ---------------------------------------------------------------------------
# Backend: GET /api/version notes gating (AC-7)
# ---------------------------------------------------------------------------

def _version_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import version as version_api

    app = FastAPI()
    app.include_router(version_api.router, prefix="/api/version")
    return TestClient(app)


def test_version_default_omits_the_changelog_notes():
    client = _version_client()
    r = client.get("/api/version")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "version" in body and body["version"]
    assert not body.get("notes"), (
        "default /api/version must not ship the full changelog; got "
        f"{len(str(body.get('notes')))} chars of notes")


def test_version_explicit_opt_in_still_returns_full_notes():
    client = _version_client()
    r = client.get("/api/version", params={"notes": "true"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("notes"), "explicit opt-in must still return the full notes string"
    assert len(body["notes"]) > 1000, "the real changelog is long; a stub would fail this"


def test_version_exposes_the_current_web_bundle_identity():
    from prism_service.api import version as version_api

    body = version_api.version(notes=False)
    assert body["web_build"]
    assert isinstance(body["web_build"], str)


# ---------------------------------------------------------------------------
# Frontend source contracts (AC-5, AC-6, AC-8) — no JS test runner, so the
# ACTUAL TSX source is asserted, matching test_work_search_filter_ui.py's
# convention.
# ---------------------------------------------------------------------------

def _read(rel: str) -> str:
    p = _WEB / rel
    assert p.exists(), f"expected source missing: {p}"
    return p.read_text(encoding="utf-8")


def test_board_fetch_requests_a_lean_field_projection():
    src = _read("pages/TasksPage.tsx")
    m = re.search(r'/api/tasks\?project=\$\{project\}[^"`]*', src)
    assert m, "expected the board's /api/tasks fetch URL"
    url = m.group(0)
    assert "fields=" in url, f"board fetch must request a fields= projection; got {url!r}"
    for must_have in ("id", "title", "status", "assigned_agent", "priority",
                       "updated_at", "workflow_step", "gate_state", "parent_id", "tags"):
        assert must_have in url, f"lean field set must include {must_have!r}; got {url!r}"
    for must_not_have in ("description", "plan_doc", "completion_proof", "oracle"):
        assert must_not_have not in url, (
            f"board fetch must NOT request heavy field {must_not_have!r}; got {url!r}")


# SUPERSEDED by task 6fbbec35: item.mirror_url was itself a description-prose
# derivative (single-valued, and null when a mirror had no "Mirrored to..."
# line). The badge now reads the store-derived, array-valued item.mirrors via
# mirrorsOf() — see test_mirror_badges_read_the_store.py for the full RED
# coverage of that contract. This test is rewritten (not deleted) to keep
# pinning the invariant that never changed: no raw description regex.
def test_mirrors_of_reads_item_mirrors_not_raw_description():
    src = _read("pages/TasksPage.tsx")
    i = src.index("function mirrorsOf(")
    end = src.index("\n}\n", i)
    body = src[i:end]
    assert "item.mirrors" in body, (
        "mirrorsOf must read the server-derived, store-backed item.mirrors "
        "array instead of the old singular item.mirror_url prose field")
    assert "item.mirror_url" not in body, (
        "mirrorsOf must not read the retired singular item.mirror_url field")
    assert "item.description" not in body, (
        "mirrorsOf must never regex item.description")


def test_detail_children_fetch_is_scoped_not_the_whole_board():
    src = _read("pages/TaskDetailPage.tsx")
    # The children-checklist fetch, not the `const [children, setChildren] =
    # useState(...)` declaration earlier in the file (a fixed offset from the
    # first "setChildren" hit would land on the wrong occurrence).
    i = src.index("ChildTask[] }>(")
    window = src[i:i + 200]
    assert "parent_id=" in window, (
        f"children fetch must scope with parent_id=; got context: {window!r}")
    assert "fields=" in window, (
        f"children fetch must project to lean fields; got context: {window!r}")
    # The OLD misfire: fetching the whole unscoped board and filtering client-side.
    assert not re.search(r"/api/tasks\?project=\$\{project\}`\)", window), (
        "children fetch must not be the old bare unscoped /api/tasks call")


def test_sidebar_tooltip_uses_the_explicit_notes_opt_in():
    sidebar = _read("components/Sidebar.tsx")
    assert "useVersionNotes" in sidebar, (
        "Sidebar's tooltip must read notes via a dedicated opt-in hook, not "
        "the lean useVersion() default")
    version_lib = _read("lib/version.ts")
    assert re.search(r"useVersionNotes", version_lib), (
        "lib/version.ts must export the notes opt-in hook Sidebar calls")
    assert "?notes=true" in version_lib or "notes=true" in version_lib, (
        "the notes hook must fetch the explicit opt-in query param, not the "
        "lean default")
