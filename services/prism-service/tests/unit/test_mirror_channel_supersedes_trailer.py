"""Pins task fb7edc46: mirrored work keeps its channel, not a prose trailer.

A fresh import through WorkItemSyncService._import_one no longer appends a
"Mirrored from ..." trailer to description -- channel=provider and
channel_ref=url (task b480eb15) already carry the provenance, so the real
issue body alone becomes the description. Two surfaces must still work on a
fresh import with no trailer prose:
  - GET /api/tasks/{id} -> mirrors[] (store-derived via _mirrors_for_task,
    untouched by this change) still reports the counterpart url.
  - GET /api/tasks?fields=...,mirror_url -> the legacy regex now falls back
    to task.channel_ref when the description carries no trailer, so a
    fresh import's badge never goes blank (api/tasks.py _mirror_url).

Rig + fixtures copied from test_mirror_badges_read_the_store.py (API rig)
and test_github_work_import.py (issues.json / WS_A sync wiring).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_FIXTURES = _HERE.parent.parent / "fixtures" / "github"
WS_A = "workspace-a"
SCOPE = "personal-local-user"  # what current_principal resolves to in local
                                # mode (AuthService.LOCAL_USER_ID), pattern
                                # per test_mirror_badges_read_the_store.py


def _fixture(name):
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


class _FixtureClient:
    def issues(self, connection, container, token):
        return _fixture("issues.json")

    def pulls(self, connection, container, token):
        return []


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


def _synced(rig, workspace_id=SCOPE):
    """Pull once through the real sync core against the SCOPE the api
    layer's personal_scope() resolves to, so GET /api/tasks/{id} finds the
    same store rows the sync just wrote."""
    from prism_service.services.github_work import GitHubWorkAdapter
    from prism_service.services.work_item_sync import WorkItemSyncService

    adapter = GitHubWorkAdapter(_FixtureClient())
    conn = rig.store.ensure_connection(workspace_id, "github", "install-1")
    cont = rig.store.ensure_container(
        workspace_id, conn.id, "repository", "R_repo1")
    svc = WorkItemSyncService(rig.store, intake=rig.svc)
    svc.register(adapter)
    svc.pull_container(workspace_id, conn, cont)
    return conn, cont


def _imported(rig):
    return [t for t in rig.svc.list() if "external" in t.tags]


def test_fresh_import_carries_channel_not_trailer(rig):
    _synced(rig)
    imported = _imported(rig)
    assert len(imported) == 2
    task = next(t for t in imported
                if t.title == "Crash on startup when config missing")

    assert task.channel == "github"
    assert task.channel_ref == "https://github.com/acme/app/issues/1"
    assert "Mirrored from" not in task.description
    # the real body alone, no appended provenance prose
    assert task.description == "Steps to reproduce..."


def test_fresh_import_with_empty_body_has_empty_description(rig):
    _synced(rig)
    imported = _imported(rig)
    closed = next(t for t in imported if t.title == "Add dark mode")

    assert closed.channel == "github"
    assert closed.channel_ref == "https://github.com/acme/app/issues/2"
    assert closed.description == ""


def test_get_task_still_reports_mirrors_from_the_store(rig):
    _synced(rig)
    task = next(t for t in _imported(rig)
                if t.title == "Crash on startup when config missing")

    body = rig.client.get(f"/api/tasks/{task.id}").json()
    assert len(body.get("mirrors") or []) == 1, body.get("mirrors")
    assert body["mirrors"][0]["url"] == "https://github.com/acme/app/issues/1"
    assert body["mirror"] == body["mirrors"][0]


def test_list_mirror_url_falls_back_to_channel_ref_with_no_trailer(rig):
    """The board's fields=mirror_url projection regexes description prose
    (api/tasks.py _mirror_url) -- a fresh import writes none of that prose
    any more, so the badge must fall back to channel_ref instead of going
    blank."""
    _synced(rig)
    task = next(t for t in _imported(rig)
                if t.title == "Crash on startup when config missing")

    r = rig.client.get("/api/tasks", params={"fields": "id,mirror_url"})
    row = next(x for x in r.json()["tasks"] if x["id"] == task.id)
    assert row["mirror_url"] == "https://github.com/acme/app/issues/1"
