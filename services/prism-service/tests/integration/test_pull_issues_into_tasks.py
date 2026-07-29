"""RED scaffold — GitHub issues become PRISM tasks (task 900a4fb9).

Owner 2026-07-28: "the whole idea of the sync is that we only have a single
source of truth and we can get changes from github, and put changes into
github as we work tasks out of prism". They turned sync on and saw nothing.

Measured live before writing this: sync_enabled True, tracking [], connections
[], workspaces [], and nothing in the service reads sync_enabled at all.

Walking skeleton of epic 02672417: track a repo, sync, and the issues are
PRISM TASKS on the ordinary /api/tasks endpoint the Work page already reads.
Read-only this slice, by the owner's explicit instruction.

The ONLY thing substituted anywhere here is the HTTP transport, which is the
network boundary. The adapter registry is the one production builds at import.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

REPO = "siegeon/.prism"
PROJECT = "prism"

ISSUES = [
    {"id": 1, "node_id": "I_kwOne", "number": 41,
     "title": "Connectors page needs a repo picker", "body": "from github",
     "state": "open", "html_url": f"https://github.com/{REPO}/issues/41",
     "assignees": [], "labels": []},
    {"id": 2, "node_id": "I_kwOtwo", "number": 42,
     "title": "Sync should be two way", "body": "also from github",
     "state": "open", "html_url": f"https://github.com/{REPO}/issues/42",
     "assignees": [], "labels": []},
]


class RecordingTransport:
    """Stands in for the network ONLY. Records every URL it is asked for so
    the test can prove this slice reads and never writes."""

    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, url: str, token: str):
        self.calls.append(url)
        if "/issues" in url:
            return list(ISSUES)
        if "/pulls" in url:
            return []
        raise AssertionError(f"unexpected GitHub call: {url}")


@pytest.fixture
def app(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setenv("PRISM_AUTH_MODE", "local")
    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path / "data"))

    from prism_service.api import integrations, integrations_connect as connect
    from prism_service.services import github_cli_auth, sync_prefs
    from prism_service.services.integration_store import (
        IntegrationStore, set_integration_store,
    )
    from prism_service.services.workspace_service import (
        WorkspaceService, set_workspace_service,
    )

    set_workspace_service(WorkspaceService(tmp_path / "workspace.db"))
    set_integration_store(IntegrationStore(str(tmp_path / "integrations.db")))
    connect.configure_state_db(str(tmp_path / "oauth_states.db"))
    sync_prefs.set_sync_preferences(
        sync_prefs.SyncPreferences(str(tmp_path / "sync_prefs.db")))

    class Creds:
        def token(self): return "gho_test"
        def account(self): return "siegeon"
        def installation_token(self, _=None): return "gho_test"
        def status(self):
            return {"state": "connected", "detail": "test", "account": "siegeon"}

    github_cli_auth.set_cli_credentials(Creds())

    # Swap ONLY the network boundary inside the adapter production registered.
    transport = RecordingTransport()
    adapter = integrations._adapters.get("github")
    assert adapter is not None, (
        "production must register a github adapter (task f4dd3687)")
    adapter._client._transport = transport

    api = FastAPI()
    api.include_router(connect.router, prefix="/api/integrations/connect")
    with TestClient(api) as client:
        client.transport_recorder = transport
        yield client

    adapter._client._transport = None
    sync_prefs.set_sync_preferences(None)
    github_cli_auth.set_cli_credentials(None)
    set_integration_store(None)
    set_workspace_service(None)


def _track(app):
    return app.post("/api/integrations/connect/github/track",
                    json={"repo": REPO})


def _enable(app):
    """Turn syncing on, the way the owner did. Sync is opt-in and starts OFF
    (task 01118728), so every import test must ask for it explicitly."""
    r = app.put("/api/integrations/connect/github/sync", json={"enabled": True})
    assert r.status_code == 200, r.text


def _sync(app):
    # /sync is the PREFERENCE (PUT). Running a sync is a distinct action, so
    # it gets its own path rather than overloading the same one by verb.
    return app.post(f"/api/integrations/connect/github/sync/run?project={PROJECT}")


def _tasks():
    """The tasks a person would see, read the way the Work page reads them."""
    from prism_service import project_context
    return project_context.get_project(PROJECT).task_svc.list()


# ── AC-1 / AC-2: a repo can be tracked with no team workspace ─────────

def test_a_repo_can_be_tracked_without_any_workspace(app):
    """Local mode never creates a team workspace, which is exactly why the
    picker is unreachable today."""
    from prism_service.services.workspace_service import get_workspace_service
    assert get_workspace_service().list_workspaces_for_user("local-user") == [], (
        "precondition: no team workspace, the state the owner is actually in")

    resp = _track(app)
    assert resp.status_code == 200, resp.text


def test_tracking_persists_a_real_connection(app):
    _track(app)
    conns = app.get("/api/integrations/connect/connections").json()["connections"]
    assert conns, "tracking must persist a connection; today this list is empty"
    row = {r["provider"]: r for r in
           app.get("/api/integrations/connect/status").json()["connectors"]}["github"]
    assert REPO in (row.get("tracking") or []), (
        f"the card must report what it tracks; got {row.get('tracking')!r}")


# ── AC-3 / AC-4 / AC-7: issues become PRISM TASKS ─────────────────────

def test_sync_imports_issues_as_prism_tasks(app):
    _track(app)
    _enable(app)
    resp = _sync(app)
    assert resp.status_code == 200, resp.text

    titles = {t.title for t in _tasks()}
    for issue in ISSUES:
        assert issue["title"] in titles, (
            f"issue {issue['number']} must become a PRISM task; the Work page "
            f"reads /api/tasks, so an import that lands anywhere else is "
            f"invisible. Saw: {sorted(titles)[:6]}")


def test_each_imported_task_carries_the_issue_it_came_from(app):
    """Without this the later push slice cannot find the counterpart."""
    _track(app)
    _enable(app)
    _sync(app)
    imported = [t for t in _tasks() if t.title == ISSUES[0]["title"]]
    assert imported, "precondition: the issue was imported"
    blob = repr(imported[0])
    assert "41" in blob or REPO in blob, (
        "the task must record the issue it mirrors")


# ── AC-5: the switch is load-bearing ──────────────────────────────────

def test_sync_refuses_when_the_switch_is_off(app):
    """Recorded second misfire: the owner's switch stays decorative and the
    pull happens whether or not they consented."""
    _track(app)
    app.put("/api/integrations/connect/github/sync", json={"enabled": False})
    before = len(_tasks())

    resp = _sync(app)
    # 409, not merely ">= 400": a missing route also 404s, and an assertion
    # that cannot tell "refused because you turned it off" from "this endpoint
    # does not exist" would stay green forever without the feature.
    assert resp.status_code == 409, (
        f"sync must refuse with a conflict while syncing is off; got "
        f"{resp.status_code} {resp.text[:120]}")
    assert "off" in resp.text.lower() or "disabled" in resp.text.lower(), (
        f"the refusal must SAY syncing is off, so a person can fix it; got "
        f"{resp.text[:160]}")
    assert len(_tasks()) == before, (
        "a refusal that still imported is not a refusal")


# ── AC-6: re-running does not duplicate ───────────────────────────────

def test_running_sync_twice_does_not_duplicate(app):
    """Recorded third misfire. Identity is meant to be a deterministic UUIDv5
    of the provider tuple, so this proves that rather than assuming it."""
    _track(app)
    _enable(app)
    _sync(app)
    first = len(_tasks())
    assert first >= len(ISSUES), (
        "precondition: the first sync must actually import, or 'no duplicates' "
        f"is 0 == 0 and proves nothing. Imported {first}")
    _sync(app)
    assert len(_tasks()) == first, (
        f"re-syncing duplicated tasks: {first} -> {len(_tasks())}")


# ── AC-8: read-only ───────────────────────────────────────────────────

def test_this_slice_never_writes_to_github(app):
    """The PULL path never writes to GitHub.

    Originally "Owner instruction: siegeon/.prism, read-only for now"
    (task 900a4fb9). The owner LIFTED the repo-wide read-only rule on
    2026-07-29, authorising write-back to siegeon/.prism, and task
    ae67ed5c added GithubRestClient.close_issue (a PATCH) to serve it.
    The blanket source-scan below asserted no write VERB may appear
    anywhere in github_rest.py, so it now contradicts a shipped
    capability rather than protecting anything; it is retired here, in
    the slice that supersedes it, instead of being left to redden main.

    What still holds, and is still asserted: a SYNC (the inbound pull)
    must not write. That is the real contract, and it is checked at
    runtime below against the transport recorder. Closing an issue is a
    separate, explicitly-scoped push (work_item_sync.push_task_closure),
    never something a pull may do.
    """
    _track(app)
    _enable(app)
    _sync(app)
    assert app.transport_recorder.calls, (
        "precondition: the sync must actually have reached GitHub, or "
        "'never writes' is vacuously true because nothing happened")
    for url in app.transport_recorder.calls:
        assert "/issues" in url or "/pulls" in url, (
            f"unexpected GitHub endpoint touched: {url}")

    # (retired) The source-scan for '"POST"' / '"PATCH"' / '"PUT"' /
    # '"DELETE"' in github_rest.py lived here. Superseded 2026-07-29 by
    # task ae67ed5c, which added close_issue (PATCH) under the owner's
    # explicit write-back authorisation. See this test's docstring.
