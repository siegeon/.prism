"""RED scaffold - imported issue bodies land on the task (task 4db228ec).

Today `_import_one` (services/work_item_sync.py) only ever writes a two-line
mirror-pointer stub into the description, never the real GitHub issue body
that github_work._issue_input already captured on ExternalEntityInput.body
and that store.upsert_entity already persists. This file fails (import
error / assertion error) until _import_one forwards entity_input.body into
the description it passes to ensure_external_intake.

Pins:
- AC-1: a fresh import's description carries the real body verbatim, plus
  the mirror provenance (display_key + url).
- AC-2: syncing the same remote item twice does not duplicate the body.
- AC-3: a human-edited local task survives a re-sync unchanged, even though
  the remote body/title changed underneath it (the no-clobber contract in
  ensure_external_intake, task_service.py:409-442, must not be bypassed).

Modeled on the scripted-adapter + IntegrationStore + TaskService harness in
tests/integration/test_external_work_sync.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

WS_A = "workspace-a"
SCOPE = "personal-local-user"  # what current_principal resolves to in local
                                # mode (api/integrations_connect.personal_scope),
                                # so a sync at this workspace_id is where the
                                # API layer's mirror lookups actually look.

ISSUE_BODY = (
    "Where the hole is, from the source\n\n"
    "The importer never forwards the body, so mirrored tasks are two lines "
    "of pointer and nothing else."
)


def _store(tmp_path, name="integrations.db"):
    from prism_service.services.integration_store import IntegrationStore

    return IntegrationStore(str(tmp_path / name))


def _task_svc(tmp_path, name="tasks.db"):
    from prism_service.services.task_service import TaskService

    return TaskService(str(tmp_path / name))


def _scripted_adapter(provider, pages):
    """A no-network adapter carrying full ExternalEntityInput fields,
    including body/display_key/url (the ones _import_one must forward)."""
    from prism_service.services.work_item_sync import PulledPage
    from prism_service.models.integration import ExternalEntityInput

    script = list(pages)

    class _Adapter:
        def __init__(self):
            self.provider = provider
            self.calls = 0

        def pull_page(self, connection, container, cursor, page_token):
            self.calls += 1
            idx = 0 if page_token is None else int(page_token)
            page = script[idx] if idx < len(script) else {"entities": []}
            ents = [
                ExternalEntityInput(
                    entity_kind=e.get("kind", "issue"),
                    remote_id=e["remote_id"],
                    display_key=e.get("display_key", ""),
                    title=e.get("title", ""),
                    body=e.get("body", ""),
                    url=e.get("url", ""),
                    remote_status=e.get("remote_status", "open"),
                    status_category=e.get("status_category", "open"),
                )
                for e in page.get("entities", [])
            ]
            return PulledPage(
                entities=ents,
                next_page_token=page.get("next_page_token"),
                next_cursor=page.get("next_cursor"),
            )

    return _Adapter()


def _sync(store, intake, *adapters):
    from prism_service.services.work_item_sync import WorkItemSyncService

    svc = WorkItemSyncService(store, intake=intake)
    for adapter in adapters:
        svc.register(adapter)
    return svc


def _import_task(tmp_path, body=ISSUE_BODY, url="https://github.com/siegeon/.prism/issues/222"):
    store = _store(tmp_path)
    tasks = _task_svc(tmp_path)
    conn = store.ensure_connection(WS_A, "github", "install-1")
    cont = store.ensure_container(WS_A, conn.id, "repository", ".prism")
    adapter = _scripted_adapter("github", [{"entities": [
        {"remote_id": "gh-222", "display_key": "#222", "title": "raw github headline",
         "body": body, "url": url},
    ]}])
    svc = _sync(store, tasks, adapter)
    svc.pull_container(WS_A, conn, cont)
    link = store.list_links(WS_A)[0]
    task = tasks.get(link.task_id)
    return store, tasks, conn, cont, task


# ── AC-1: the real body lands in the description, provenance retained ──
#
# The three assertions this originally pinned -- "#222" and the issue URL
# appended as description prose -- are retired by task fb7edc46: channel=
# provider + channel_ref=url (task b480eb15) now carry that provenance, and
# a fresh import writes the body ALONE into description, no trailer. What
# survives from the original oracle (the real body lands in the task) is
# kept below; the provenance half moves to channel/channel_ref, and
# mirror_url/mirrors[].url resolving from those fields is pinned in the
# API-level test right after this one.

def test_imported_description_carries_the_real_body_channel_carries_provenance(tmp_path):
    _, _, _, _, task = _import_task(tmp_path)

    assert task.description == ISSUE_BODY
    assert "Mirrored from" not in task.description
    assert task.channel == "github"
    assert task.channel_ref == "https://github.com/siegeon/.prism/issues/222"


def test_mirror_url_and_mirrors_resolve_with_no_description_trailer(monkeypatch, tmp_path):
    """Same fresh-import shape as the test above, but through the real API
    layer (task fb7edc46): with no provenance prose in description any
    more, GET /api/tasks?fields=mirror_url must fall back to channel_ref,
    and GET /api/tasks/{id} must still report mirrors[] from the store."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from prism_service.api import tasks as tasks_api
    from prism_service.services.integration_store import (
        IntegrationStore, set_integration_store)
    from prism_service.services.workspace_service import (
        WorkspaceService, set_workspace_service)

    monkeypatch.setenv("PRISM_AUTH_MODE", "local")
    tasks = _task_svc(tmp_path)

    class _Ctx:
        task_svc = tasks

    monkeypatch.setattr(tasks_api, "get_project", lambda p: _Ctx())
    set_workspace_service(WorkspaceService(tmp_path / "workspace.db"))
    store = IntegrationStore(str(tmp_path / "integrations.db"))
    set_integration_store(store)
    try:
        conn = store.ensure_connection(SCOPE, "github", "install-1")
        cont = store.ensure_container(SCOPE, conn.id, "repository", ".prism")
        adapter = _scripted_adapter("github", [{"entities": [
            {"remote_id": "gh-222", "display_key": "#222", "title": "raw github headline",
             "body": ISSUE_BODY, "url": "https://github.com/siegeon/.prism/issues/222"},
        ]}])
        svc = _sync(store, tasks, adapter)
        svc.pull_container(SCOPE, conn, cont)
        link = store.list_links(SCOPE)[0]
        task_id = link.task_id

        app = FastAPI()
        app.include_router(tasks_api.router, prefix="/api/tasks")
        with TestClient(app) as client:
            r = client.get("/api/tasks", params={"fields": "id,mirror_url"})
            row = next(x for x in r.json()["tasks"] if x["id"] == task_id)
            assert row["mirror_url"] == "https://github.com/siegeon/.prism/issues/222"

            body = client.get(f"/api/tasks/{task_id}").json()
            assert len(body.get("mirrors") or []) == 1, body.get("mirrors")
            assert body["mirrors"][0]["url"] == "https://github.com/siegeon/.prism/issues/222"
    finally:
        set_integration_store(None)
        set_workspace_service(None)


# ── AC-2: a second sync of the same remote item does not duplicate the body ──

def test_second_sync_does_not_duplicate_the_body(tmp_path):
    store, tasks, conn, cont, task = _import_task(tmp_path)

    adapter2 = _scripted_adapter("github", [{"entities": [
        {"remote_id": "gh-222", "display_key": "#222", "title": "raw github headline",
         "body": ISSUE_BODY, "url": "https://github.com/siegeon/.prism/issues/222"},
    ]}])
    svc2 = _sync(store, tasks, adapter2)
    svc2.pull_container(WS_A, conn, cont)

    link = store.list_links(WS_A)[0]
    task_after = tasks.get(link.task_id)
    assert task_after.description.count("Where the hole is, from the source") == 1


# ── AC-3: a human-edited local row is untouched by a re-sync ────────────

def test_human_edited_task_survives_a_resync(tmp_path):
    store, tasks, conn, cont, task = _import_task(tmp_path)

    link = store.list_links(WS_A)[0]
    tasks.update(
        link.task_id,
        title="Gate the ticket's premise, not just its form",
        description="hand-enriched local text, not the mirror stub",
    )

    adapter2 = _scripted_adapter("github", [{"entities": [
        {"remote_id": "gh-222", "display_key": "#222", "title": "a totally different remote headline",
         "body": "a totally different remote body", "url": "https://github.com/siegeon/.prism/issues/222"},
    ]}])
    svc2 = _sync(store, tasks, adapter2)
    svc2.pull_container(WS_A, conn, cont)

    task_after = tasks.get(link.task_id)
    assert task_after.title == "Gate the ticket's premise, not just its form"
    assert task_after.description == "hand-enriched local text, not the mirror stub"
