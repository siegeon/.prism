"""RED scaffold — the task detail page shows the counterpart issue (epic
02672417, GAP-B, AC-6).

Today: TaskDetailPage.tsx (:1479-1493) gates the whole external-context
block on `(task.tags ?? []).includes("external")` and reads a dead
`task.external_url` field — nothing in the tree ever sets it
(project-wide grep confirms only that one TSX file references it), and
`api/tasks.py::get_task` returns the raw Task with no mirror field. An
IMPORTED task carries the "external" tag; an OUTBOUND-created native task
(pushed via GAP-A / task_mirror.py) never gets that tag, so it would be
invisible even if external_url were wired — the tag-gate itself is the
wrong test.

KEY PREMISE (already true on disk, no migration needed):
ExternalEntity.url / last_seen_at (models/integration.py:131,137) already
exist, and IntegrationStore.upsert_entity stamps last_seen_at on both
insert AND update, for pulled AND pushed issues alike. list_entities(...,
task_id=...) already joins work_item_external_links.

ASSERT THE AFFORDANCE A PERSON USES: the source test below requires an
actual `href` reading `task.mirror`, not merely the field's existence
somewhere in the file, and requires the old tag-gate to be gone — a
guard left standing would keep the outbound case invisible even after
the backend field ships.
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

_DETAIL = (_SERVICE_ROOT / "prism_service" / "web" / "src" / "pages"
          / "TaskDetailPage.tsx")

REPO = "siegeon/.prism"
SCOPE = "personal-local-user"  # what current_principal resolves to in
                                # local mode (AuthService.LOCAL_USER_ID)


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
            "client": client, "svc": svc, "store": store,
        })()

    set_integration_store(None)
    set_workspace_service(None)


def _link_entity(store, task_id, issue="#42",
                 url=None, provider="github"):
    """A container + entity + ACTIVE link — the shape BOTH a pull and a
    push leave behind (store.upsert_entity/claim_import_link/activate_link
    are provider- and direction-agnostic)."""
    from prism_service.models.integration import ExternalEntityInput

    url = url or f"https://github.com/{REPO}/issues/{issue.lstrip('#')}"
    conn = store.ensure_connection(SCOPE, provider, "install-detail")
    cont = store.ensure_container(SCOPE, conn.id, "repository", REPO,
                                  display_key=REPO)
    entity, _ = store.upsert_entity(
        SCOPE, conn.id, cont.id,
        ExternalEntityInput(entity_kind="issue", remote_id=f"I_{issue}",
                            display_key=issue, url=url,
                            remote_status="open", status_category="open"))
    link = store.claim_import_link(SCOPE, entity.id, task_id)
    store.activate_link(SCOPE, link.id)
    return entity


# ── AC-6, imported half: the "external" case must keep working ────────────

def test_get_task_exposes_mirror_for_an_imported_task(rig):
    task = rig.svc.create(title="came from github", tags=["github", "external"])
    entity = _link_entity(rig.store, task.id, issue="#900")

    body = rig.client.get(f"/api/tasks/{task.id}").json()
    mirror = body.get("mirror")
    assert mirror is not None, (
        "GET /api/tasks/{id} must carry a 'mirror' key for a linked task")
    assert mirror["provider"] == "github"
    assert mirror["issue"] == "#900"
    assert mirror["url"] == entity.url
    assert mirror["last_synced_at"], "last_synced_at must not be empty"  # field name per shipped slice a7c989c6 (assembly 2026-08-09)


# ── AC-6, outbound half: THE gap — no "external" tag, still must show ─────

def test_get_task_exposes_mirror_for_an_outbound_created_native_task(rig):
    """A task pushed via the GAP-A sweep is PRISM-native — it never gets
    the "external" tag (that tag means IMPORTED). If the mirror field is
    gated on that tag, this is exactly the case that stays invisible."""
    task = rig.svc.create(title="native task, pushed to github")
    assert "external" not in (task.tags or [])
    entity = _link_entity(rig.store, task.id, issue="#4242")

    body = rig.client.get(f"/api/tasks/{task.id}").json()
    mirror = body.get("mirror")
    assert mirror is not None, (
        "an outbound-created native task (no 'external' tag) has no "
        "'mirror' in the API response — AC-6 fails for this half of the "
        "round trip even though a github issue really exists for it")
    assert mirror["url"] == entity.url
    assert mirror["last_synced_at"]  # per shipped slice a7c989c6


def test_get_task_mirror_is_none_without_a_counterpart(rig):
    task = rig.svc.create(title="purely local, never mirrored")
    body = rig.client.get(f"/api/tasks/{task.id}").json()
    assert "mirror" in body, "the key must always be present (additive)"
    assert body["mirror"] is None


# ── UI: the actual affordance a person clicks ──────────────────────────────

# SUPERSEDED by task 6fbbec35: task.mirror (singular) can only ever show ONE
# counterpart, so a task linked to both github AND jira lost one badge. The
# detail page now maps over the store-derived, array-valued task.mirrors;
# task.mirror survives only as a read-only mirrors[0] alias on the wire (AC-7,
# still pinned above). Rewritten, not deleted — the "real clickable href,
# not just a field reference" discipline still applies to the new map.
def test_taskdetail_renders_a_working_link_from_task_mirrors():
    """Must find a REAL rendered `href` reading inside a task.mirrors map,
    not merely the substring somewhere in the file (a stray comment has
    satisfied look-alike assertions before — CLAUDE.md lessons)."""
    assert _DETAIL.exists(), f"expected source missing: {_DETAIL}"
    src = _DETAIL.read_text(encoding="utf-8")

    assert re.search(r"task\.mirrors\s*\?\?\s*\[\]\s*\)?\s*\.\s*map\(|"
                      r"task\.mirrors\s*\.\s*map\(", src), (
        "TaskDetailPage must render one row per entry via "
        "(task.mirrors ?? []).map(...) — it is the only field that shows "
        "BOTH an imported and an outbound-created task's counterparts, and "
        "the only shape that can render more than one")

    href = re.search(r"href=\{[^}]*\.url[^}]*\}", src)
    assert href, (
        "no <a href=...> inside the mirrors map actually reads a mirror's "
        "url — a person needs a real clickable link, not just a field "
        "reference somewhere")

    window = src[max(0, href.start() - 400):href.end() + 400]
    assert "last_synced" in window, (
        "each counterpart link must show a last-synced time next to it "
        "(AC-6); found a link but no last_synced nearby")


def test_taskdetail_no_longer_gates_the_counterpart_on_the_external_tag():
    """The old gate made an outbound-created native task (no 'external'
    tag) permanently invisible — retiring it is the fix, not an
    afterthought."""
    src = _DETAIL.read_text(encoding="utf-8")
    assert 'includes("external")' not in src, (
        "the counterpart block must no longer be gated on "
        "(task.tags ?? []).includes(\"external\") — that hides every "
        "outbound-created native task, which has no such tag")
    assert "task.external_url" not in src, (
        "task.external_url is dead (no backend producer anywhere in the "
        "tree); it must be retired in favour of task.mirror.url")
