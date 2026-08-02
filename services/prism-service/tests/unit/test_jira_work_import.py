"""RED scaffold — Jira issue import via the provider-neutral core (task fbe9f26c).

Drives the not-yet-built JiraWorkAdapter through WorkItemSyncService against a
fake /search/jql client (no network). Pins: dedupe by stable issue ID within
connection/site scope, identical keys across sites do not collide, remote
status stays local pending, and a secret access token never reaches a receipt.

Prism modules import INSIDE helpers/tests so the file collects and fails at
runtime (red = rc 1) before jira_work exists.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_FIXTURES = _HERE.parent.parent / "fixtures" / "jira"
WS_A = "workspace-a"


def _fixture(name="search_jql.json"):
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


class _FakeJiraClient:
    """Returns canned /search/jql pages keyed by cloud_id; records tokens."""

    def __init__(self, pages_by_cloud):
        self._pages = pages_by_cloud
        self.tokens_seen = []

    def search_jql(self, cloud_id, access_token, jql, page_token=None, max_results=50):
        self.tokens_seen.append(access_token)
        return self._pages.get(cloud_id, {"issues": [], "nextPageToken": None})


def _store(tmp_path):
    from prism_service.services.integration_store import IntegrationStore

    return IntegrationStore(str(tmp_path / "integrations.db"))


def _task_svc(tmp_path):
    from prism_service.services.task_service import TaskService

    return TaskService(str(tmp_path / "tasks.db"))


def _adapter(client, token="access-token"):
    from prism_service.services.jira_work import JiraWorkAdapter

    return JiraWorkAdapter(client, access_token_provider=lambda connection: token)


def _sync(store, tasks, adapter):
    from prism_service.services.work_item_sync import WorkItemSyncService

    svc = WorkItemSyncService(store, intake=tasks)
    svc.register(adapter)
    return svc


def test_search_jql_import_dedupes_by_stable_id(tmp_path):
    client = _FakeJiraClient({"cloud-1": _fixture()})
    store = _store(tmp_path)
    tasks = _task_svc(tmp_path)
    conn = store.ensure_connection(WS_A, "jira", "cloud-1")
    cont = store.ensure_container(WS_A, conn.id, "jira_project", "PROJ")
    svc = _sync(store, tasks, _adapter(client))

    svc.pull_container(WS_A, conn, cont)
    svc.pull_container(WS_A, conn, cont)  # replay

    issues = store.list_entities(WS_A)
    assert {e.remote_id for e in issues} == {"10001", "10002"}
    assert all(e.entity_kind == "jira_issue" for e in issues)
    # the mutable key is display-only, never identity
    by_id = {e.remote_id: e for e in issues}
    assert by_id["10001"].display_key == "PROJ-1"
    assert by_id["10001"].title == "Login is broken on Safari"


def test_identical_keys_across_sites_do_not_collide(tmp_path):
    shared = {"issues": [{"id": "500", "key": "SHARED-1",
                          "fields": {"summary": "same key", "status": {"name": "To Do"},
                                     "assignee": None, "updated": "2026-07-01T00:00:00.000+0000"}}],
              "nextPageToken": None}
    client = _FakeJiraClient({"cloud-1": shared, "cloud-2": shared})
    store = _store(tmp_path)
    tasks = _task_svc(tmp_path)
    c1 = store.ensure_connection(WS_A, "jira", "cloud-1")
    c2 = store.ensure_connection(WS_A, "jira", "cloud-2")
    k1 = store.ensure_container(WS_A, c1.id, "jira_project", "PROJ")
    k2 = store.ensure_container(WS_A, c2.id, "jira_project", "PROJ")
    svc = _sync(store, tasks, _adapter(client))

    svc.pull_container(WS_A, c1, k1)
    svc.pull_container(WS_A, c2, k2)

    # identical issue key "SHARED-1" under two cloudId connections → two entities
    assert len(store.list_entities(WS_A)) == 2
    assert len({e.id for e in store.list_entities(WS_A)}) == 2


def test_remote_done_completes_the_task_but_stays_out_of_conductor(tmp_path):
    """SUPERSEDES test_remote_status_stays_out_of_conductor (task 0a9b511f).

    Its `assert task.status == "pending"  # NOT done` was reversed by the owner
    on 2026-08-02 ("both ways"). The reconcile lives in the shared
    work_item_sync._import_one, so Jira inherits it rather than getting a
    provider-specific path; that is intentional, since a second code path is a
    second thing to keep honest.

    The conductor half of the old name still holds, and is the reason this is a
    rewrite and not a deletion: workflow_step and gate_state are still never
    driven from remote state.
    """
    client = _FakeJiraClient({"cloud-1": _fixture()})
    store = _store(tmp_path)
    tasks = _task_svc(tmp_path)
    conn = store.ensure_connection(WS_A, "jira", "cloud-1")
    cont = store.ensure_container(WS_A, conn.id, "jira_project", "PROJ")
    _sync(store, tasks, _adapter(client)).pull_container(WS_A, conn, cont)

    done_issue = [e for e in store.list_entities(WS_A) if e.remote_id == "10002"][0]
    assert done_issue.remote_status == "Done"          # raw preserved
    assert done_issue.status_category == "done"        # normalized
    link = store.list_links(WS_A, entity_id=done_issue.id)[0]
    task = tasks.get(link.task_id)
    assert task.status == "done"                       # reversed contract
    # UNCHANGED: remote state still never drives the conductor.
    assert task.workflow_step == "" and task.gate_state == "none"


def test_secret_access_token_never_reaches_receipts(tmp_path):
    secret = "jira_ACCESS_SECRET_TOKEN_777"
    client = _FakeJiraClient({"cloud-1": _fixture()})
    store = _store(tmp_path)
    tasks = _task_svc(tmp_path)
    conn = store.ensure_connection(WS_A, "jira", "cloud-1")
    cont = store.ensure_container(WS_A, conn.id, "jira_project", "PROJ")
    _sync(store, tasks, _adapter(client, token=secret)).pull_container(WS_A, conn, cont)

    assert secret in client.tokens_seen  # used for auth
    raw = sqlite3.connect(str(tmp_path / "integrations.db"))
    try:
        dump = "\n".join(raw.iterdump())
    finally:
        raw.close()
    assert secret not in dump
    for entity in store.list_entities(WS_A):
        assert secret not in json.dumps(entity.__dict__, default=str)
