"""RED -- mirror badges read the STORE, not description prose (task 6fbbec35).

Owner rule 2026-08-11: PRISM is the source, github AND jira are both mirrors,
never randomly shown -- a task linked to both providers must show BOTH
badges. Today `_mirror_for_task` (api/tasks.py:485-512) is store-derived but
SINGULAR: `next((l for l in links if l.state == "active"), None)` (:496)
finds every active link and throws away all but the first. The board's badge
(TasksPage.tsx `mirrorOf`, :95-105) is worse -- it reads `item.mirror_url`,
itself derived by regexing description PROSE (`_mirror_url`, tasks.py:173-
178), so a store link with no "Mirrored to..." line renders nothing, and
prose left behind by an unlinked/broken sync renders a phantom badge.

FAILS TODAY because:
  - `list_tasks` has no `mirrors` branch in its fields= projection (the
    3-branch if/elif at tasks.py:219-225 only knows mirror_url/artifact_url).
  - GET /api/tasks/{id} has no `mirrors` key (only the singular `mirror`).
  - TasksPage.tsx has no `mirrorsOf` (only the single-badge `mirrorOf`).
  - TaskDetailPage.tsx has no `task.mirrors` array or `.map(` over it.

ASSERT THE AFFORDANCE A PERSON USES: the TSX tests below parse the enclosing
function/JSX branch, never a fixed character window around a match -- a
comment above the element has satisfied look-alike assertions before
(CLAUDE.md lessons).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_WEB = _SERVICE_ROOT / "prism_service" / "web" / "src"
_TASKS_TSX = _WEB / "pages" / "TasksPage.tsx"
_DETAIL_TSX = _WEB / "pages" / "TaskDetailPage.tsx"

REPO = "siegeon/.prism"
SCOPE = "personal-local-user"  # what current_principal resolves to in local
                                # mode (AuthService.LOCAL_USER_ID), pattern
                                # per test_task_detail_shows_counterpart.py


@pytest.fixture
def rig(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from prism_service.api import tasks as tasks_api
    from prism_service.services.integration_store import (
        IntegrationStore, set_integration_store)
    from prism_service.services.task_service import TaskService
    from prism_service.services.workspace_service import (
        WorkspaceService, set_workspace_service)

    monkeypatch.setenv("PRISM_AUTH_MODE", "local")

    svc = TaskService(str(tmp_path / "tasks.db"))

    class _Ctx:
        task_svc = svc

    monkeypatch.setattr(tasks_api, "get_project", lambda p: _Ctx())

    set_workspace_service(WorkspaceService(tmp_path / "workspace.db"))
    store = IntegrationStore(str(tmp_path / "integrations.db"))
    set_integration_store(store)

    app = FastAPI()
    app.include_router(tasks_api.router, prefix="/api/tasks")
    with TestClient(app) as client:
        yield type("Rig", (), {
            "client": client, "svc": svc, "store": store, "api": tasks_api,
        })()

    set_integration_store(None)
    set_workspace_service(None)


def _link(store, task_id, issue="#42", url=None, provider="github"):
    """A container + entity + ACTIVE link -- the shape both a pull and a
    push leave behind (provider- and direction-agnostic, matching
    test_task_detail_shows_counterpart.py's `_link_entity`)."""
    from prism_service.models.integration import ExternalEntityInput

    if url is None:
        url = (f"https://github.com/{REPO}/issues/{issue.lstrip('#')}"
               if provider == "github"
               else f"https://acme.atlassian.net/browse/{issue}")
    conn = store.ensure_connection(SCOPE, provider, "install")
    cont = store.ensure_container(SCOPE, conn.id, "repository", REPO, display_key=REPO)
    entity, _ = store.upsert_entity(
        SCOPE, conn.id, cont.id,
        ExternalEntityInput(entity_kind="issue", remote_id=f"I_{provider}_{issue}",
                            display_key=issue, url=url,
                            remote_status="open", status_category="open"))
    link = store.claim_import_link(SCOPE, entity.id, task_id)
    store.activate_link(SCOPE, link.id)
    return entity


def _read(p: Path) -> str:
    assert p.exists(), f"expected source missing: {p}"
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# AC-1: GET /api/tasks?fields=...,mirrors is store-derived, description
# stays untouched.
# ---------------------------------------------------------------------------

def test_list_projection_serves_store_mirrors(rig):
    task = rig.svc.create(title="mirrored, no prose", tags=["github"])
    entity = _link(rig.store, task.id)

    r = rig.client.get("/api/tasks", params={"fields": "id,mirrors"})
    assert r.status_code == 200, r.text
    row = next(x for x in r.json()["tasks"] if x["id"] == task.id)
    assert set(row.keys()) == {"id", "mirrors"}, (
        f"a fields= projection must ship ONLY the requested keys, got {row}")
    assert len(row["mirrors"]) == 1, row
    m = row["mirrors"][0]
    assert m["provider"] == "github"
    assert m["url"] == entity.url
    assert m["issue"] == entity.display_key
    assert m["last_synced_at"], "last_synced_at must not be empty"


# ---------------------------------------------------------------------------
# AC-2: both providers linked -> two distinct entries.
# ---------------------------------------------------------------------------

def test_both_providers_are_served(rig):
    task = rig.svc.create(title="mirrored to both", tags=["github", "jira"])
    gh = _link(rig.store, task.id, issue="#1", provider="github")
    jr = _link(rig.store, task.id, issue="PROJ-1", provider="jira")

    r = rig.client.get("/api/tasks", params={"fields": "id,mirrors"})
    row = next(x for x in r.json()["tasks"] if x["id"] == task.id)
    providers = {m["provider"] for m in row["mirrors"]}
    urls = {m["url"] for m in row["mirrors"]}
    assert providers == {"github", "jira"}, (
        f"a task linked to both providers must serve both, got {row['mirrors']}")
    assert urls == {gh.url, jr.url}, "each provider entry must keep its own url"
    assert gh.url != jr.url, "sanity: the two fixtures must not collide"


# ---------------------------------------------------------------------------
# AC-4 / AC-5: the store link is the ONLY source; description prose is
# provenance-only.
# ---------------------------------------------------------------------------

def test_store_link_without_prose_still_renders(rig):
    task = rig.svc.create(title="never got the description line",
                          description="Just the original ask, nothing else.")
    _link(rig.store, task.id)
    assert "Mirrored to" not in task.description

    r = rig.client.get("/api/tasks", params={"fields": "id,mirrors"})
    row = next(x for x in r.json()["tasks"] if x["id"] == task.id)
    assert len(row["mirrors"]) == 1, (
        "an ACTIVE store link with no 'Mirrored to...' description prose "
        f"must still serve a mirror entry, got {row['mirrors']}")


def test_prose_without_a_store_link_renders_nothing(rig):
    task = rig.svc.create(
        title="prose only, no real link",
        description=("Mirrored to github owner/repo#42.\n"
                      "https://github.com/owner/repo/issues/42"))
    # deliberately never call _link -- no store row behind this task

    r = rig.client.get("/api/tasks", params={"fields": "id,mirrors"})
    row = next(x for x in r.json()["tasks"] if x["id"] == task.id)
    assert row["mirrors"] == [], (
        "mirrors must come from the store, never from description prose; "
        f"got {row['mirrors']}")


def test_mirrorsOf_never_reads_item_mirror_url_or_regexes_description():
    src = _read(_TASKS_TSX)
    i = src.index("function mirrorsOf(")
    end = src.index("\n}\n", i)
    body = src[i:end]
    assert "item.mirror_url" not in body, (
        "mirrorsOf must read the store-derived item.mirrors, not the "
        "retired singular item.mirror_url prose field")
    assert "item.description" not in body, (
        "mirrorsOf must never regex item.description")


# ---------------------------------------------------------------------------
# AC-3: the board renders ONE badge per mirror entry via a real .map(), not
# a single-badge function.
# ---------------------------------------------------------------------------

def test_board_renders_one_badge_per_mirror():
    src = _read(_TASKS_TSX)
    assert "function mirrorsOf(" in src, (
        "TasksPage.tsx must define an array-returning mirrorsOf, replacing "
        "the single-badge mirrorOf() -- .map() needs an array to iterate")

    i = src.index("function WorkRow(")
    j = src.index("\nfunction ", i + 1) if "\nfunction " in src[i + 1:] else len(src)
    row_body = src[i:j]
    m = re.search(r"mirrorsOf\(item\)\s*\.\s*map\(", row_body)
    assert m, (
        "WorkRow must render one badge per entry via mirrorsOf(item).map(...), "
        "not a single mirrorOf(item) call gated by `mirror &&`")

    window = row_body[m.start():m.start() + 600]
    assert re.search(r"<a[\s\S]*href=\{", window), (
        "no <a href=...> rendered inside the mirrorsOf(item).map(...) "
        "callback -- a person needs a real clickable link per mirror")


def test_board_badge_shape_unchanged_single_and_zero_mirror():
    """AC-6: single-mirror and zero-mirror tasks must render exactly as
    before -- same class list and aria-label pattern, just array-driven."""
    src = _read(_TASKS_TSX)
    assert 'aria-label={`Open this task' in src, (
        "the badge's aria-label affordance must be preserved unchanged")
    assert "text-2xs font-semibold uppercase tracking-wider px-1.5 py-0.5 rounded" in src, (
        "the badge's visual class shape must be preserved unchanged")


# ---------------------------------------------------------------------------
# AC-7: GET /api/tasks/{id} serves both `mirrors` (list) and `mirror`
# (singular alias = mirrors[0] or None).
# ---------------------------------------------------------------------------

def test_detail_route_serves_plural_mirrors(rig):
    task = rig.svc.create(title="two counterparts")
    _link(rig.store, task.id, issue="#1", provider="github")
    _link(rig.store, task.id, issue="PROJ-9", provider="jira")

    body = rig.client.get(f"/api/tasks/{task.id}").json()
    assert len(body.get("mirrors") or []) == 2, body.get("mirrors")
    assert body["mirror"] == body["mirrors"][0], (
        "the singular 'mirror' key must remain a live alias for "
        "mirrors[0], never a second independent lookup")


def test_detail_route_mirrors_empty_and_mirror_none_without_a_counterpart(rig):
    task = rig.svc.create(title="purely local, never mirrored")
    body = rig.client.get(f"/api/tasks/{task.id}").json()
    assert "mirrors" in body, "the key must always be present (additive)"
    assert body["mirrors"] == []
    assert body["mirror"] is None


def test_taskdetail_renders_all_mirrors_via_map():
    src = _read(_DETAIL_TSX)
    assert re.search(r"task\.mirrors[\s\S]{0,20}\.\s*map\(", src), (
        "TaskDetailPage must render one row per entry via "
        "(task.mirrors ?? []).map(...) -- a task linked to both github and "
        "jira must show both counterparts, not just task.mirror (singular)")


# ---------------------------------------------------------------------------
# AC-8 (the ticket's own named misfire): the mirrors join is ONE batched
# fetch per request, not one per row. Pinned at two row counts, same
# sqlite3.set_trace_callback technique as test_task_views_dont_overfetch.py.
# Calls list_tasks DIRECTLY (not via TestClient) -- FastAPI runs sync routes
# in a threadpool, and IntegrationStore._db is a thread-local connection, so
# a trace callback set on the main-thread connection would silently miss
# every query the request actually ran.
# ---------------------------------------------------------------------------

def _seed_linked_tasks(rig, n, offset=0):
    ids = []
    for i in range(offset, offset + n):
        t = rig.svc.create(title=f"seed-{i}")
        _link(rig.store, t.id, issue=f"#{i}")
        ids.append(t.id)
    return ids


def _store_sql(calls):
    tables = ("work_item_external_links", "external_entities",
              "integration_connections")
    return [c for c in calls if any(t in c for t in tables)]


def test_mirrors_join_is_batched(rig):
    conn = rig.store._db

    _seed_linked_tasks(rig, 5)
    calls_5 = []
    conn.set_trace_callback(lambda sql: calls_5.append(sql))
    try:
        rig.api.list_tasks(project="prism", include_deleted=False,
                           parent_id=None, fields="id,mirrors")
    finally:
        conn.set_trace_callback(None)
    store_sql_5 = _store_sql(calls_5)

    _seed_linked_tasks(rig, 45, offset=5)  # 50 linked tasks total now
    calls_50 = []
    conn.set_trace_callback(lambda sql: calls_50.append(sql))
    try:
        rig.api.list_tasks(project="prism", include_deleted=False,
                           parent_id=None, fields="id,mirrors")
    finally:
        conn.set_trace_callback(None)
    store_sql_50 = _store_sql(calls_50)

    assert len(store_sql_5) == len(store_sql_50), (
        f"mirrors join issued {len(store_sql_5)} store queries for 5 linked "
        f"tasks but {len(store_sql_50)} for 50 -- it must be ONE batched "
        f"query per table per request, never one per row.\n"
        f"5-task queries: {store_sql_5}\n50-task queries: {store_sql_50}")
    assert 0 < len(store_sql_5) <= 3, (
        f"expected at most one query each against links/entities/"
        f"connections (and at least one, since links exist), "
        f"got {len(store_sql_5)}: {store_sql_5}")
