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


def test_remote_done_reconciles_board_wins_but_never_touches_the_conductor(tmp_path):
    """UPDATED under task 64ba4755 (owner 2026-08-10, FR-7): Jira now joins
    STATUS_RECONCILE_PROVIDERS, so a Done-category issue DOES move its local
    task to done on import — superseding the old "task.status stays pending"
    assertion this test carried. The real invariant survives unchanged:
    remote state still never drives workflow_step or gate_state, so a closed
    issue can never manufacture a passed gate."""
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
    assert task.status == "done", (
        "board-wins mapping (task 64ba4755, FR-7) must reconcile a Done "
        "Jira issue to a done local task on import")
    assert task.workflow_step == "" and task.gate_state == "none", (
        "the surviving invariant: remote state must still never drive "
        "workflow_step/gate_state")


def test_a_task_with_a_workflow_step_set_is_never_status_clobbered(tmp_path):
    """AC-8: today only the pre-existing `done` short-circuit protects a
    mirrored task; this pins the NEW guard the board-wins mapping needs —
    a task a driver already picked up (non-empty workflow_step) must stay
    exactly as it is even when the remote issue closes."""
    client = _FakeJiraClient({"cloud-1": _fixture()})
    store = _store(tmp_path)
    tasks = _task_svc(tmp_path)
    conn = store.ensure_connection(WS_A, "jira", "cloud-1")
    cont = store.ensure_container(WS_A, conn.id, "jira_project", "PROJ")
    sync = _sync(store, tasks, _adapter(client))
    sync.pull_container(WS_A, conn, cont)  # first pull: creates the task

    done_issue = [e for e in store.list_entities(WS_A) if e.remote_id == "10002"][0]
    link = store.list_links(WS_A, entity_id=done_issue.id)[0]
    tasks.update(link.task_id, status="in_progress", workflow_step="write_failing_tests")

    sync.pull_container(WS_A, conn, cont)  # second pull: issue is still Done

    task = tasks.get(link.task_id)
    assert task.status == "in_progress", (
        "a task with workflow_step set must never be clobbered by remote "
        "status, even when the mirror keeps seeing it as Done")
    assert task.workflow_step == "write_failing_tests"


def test_jql_is_scoped_to_in_flow_work_and_the_project_key_is_validated(tmp_path):
    """FR-5: the JQL must scope to the connected user's in-flow work, and the
    project key is validated against ^[A-Za-z][A-Za-z0-9_]*$ BEFORE
    interpolation — closing the recorded JQL-injection defect mx-8f4666."""
    from prism_service.services.jira_work import JiraWorkAdapter
    from prism_service.services.work_item_sync import AdapterError

    class _CapturingClient:
        def __init__(self):
            self.jqls: list[str] = []

        def search_jql(self, cloud_id, access_token, jql, page_token=None, max_results=50):
            self.jqls.append(jql)
            return {"issues": [], "nextPageToken": None}

    store = _store(tmp_path)
    conn = store.ensure_connection(WS_A, "jira", "cloud-1")
    cont = store.ensure_container(WS_A, conn.id, "jira_project", "PROJ")

    client = _CapturingClient()
    adapter = JiraWorkAdapter(client, access_token_provider=lambda c: "tok")
    adapter.pull_page(conn, cont, cursor=None, page_token=None)

    assert len(client.jqls) == 1
    jql = client.jqls[0]
    assert 'project = "PROJ"' in jql
    assert "assignee = currentUser()" in jql
    assert "statusCategory != Done" in jql

    injector = store.ensure_container(
        WS_A, conn.id, "jira_project", 'PROJ" OR 1=1 --')
    bad_client = _CapturingClient()
    bad_adapter = JiraWorkAdapter(bad_client, access_token_provider=lambda c: "tok")
    with pytest.raises(AdapterError):
        bad_adapter.pull_page(conn, injector, cursor=None, page_token=None)
    assert not bad_client.jqls, (
        "an invalid project key must never reach JQL interpolation")


def test_issue_url_is_built_from_the_connections_site_url(tmp_path):
    """FR-6: `_issue_input` must build a real browse URL from the connection's
    stored site_url, replacing the hardcoded url=''."""
    from prism_service.services.jira_work import _issue_input

    issue = {"id": "10001", "key": "PROJ-1",
             "fields": {"summary": "x", "status": {"name": "To Do"},
                        "assignee": None, "updated": "2026-08-01T00:00:00Z"}}
    entity = _issue_input(issue, "https://acme.atlassian.net")
    assert entity.url == "https://acme.atlassian.net/browse/PROJ-1"

    # no site_url known yet -> stays empty, never a broken partial URL
    entity_no_site = _issue_input(issue, "")
    assert entity_no_site.url == ""


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
