"""RED - the Jira (and by symmetry GitHub) card shows what the sync did
(task 1c9899d6).

Owner complaint (2026-08-11): a sync that DID run reported
succeeded/imported:0 with no durable trace of why (real cause: the in-flow
JQL filter is ``assignee = currentUser() AND statusCategory != Done``, and
none of the tracked project's issues were assigned to the connected user).

Pins:
  AC-1: GET /api/integrations/connect/status surfaces the LATEST sync_runs
        row per tracked container (integration_store.py's own
        IntegrationStore.list_runs ordering, never re-derived), as a new
        ``last_sync`` field on each connector row.
  AC-2: a zero-import last_sync names the in-flow filter in plain words.
  AC-3: a non-zero last_sync needs no filter prose.
  AC-4: a tracked container that never synced reads as calmly absent
        (last_sync: null), never fabricated.
  AC-5: GitHub gets the same last_sync shape (symmetric, RepoSync is shared).
  AC-6: the connector card (SettingsPage.tsx RepoSync) renders last_sync
        DURABLY on the card body - independent of the transient `note`
        local-state line that vanishes on reload/remount.
  AC-7: a zero-import last_sync explains why on the card.
  AC-8: a non-zero last_sync links to the Work view filtered to this
        provider's imported rows (TasksPage's existing tag search, driven
        by a `q` URL param it does not yet read).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

SITE = "https://acme.atlassian.net"
SCOPE = "personal-local-user"

_SETTINGS = (_SERVICE_ROOT / "prism_service" / "web" / "src" / "pages"
             / "SettingsPage.tsx")
_TASKS = (_SERVICE_ROOT / "prism_service" / "web" / "src" / "pages"
          / "TasksPage.tsx")


class _FakeConnectTransport:
    """Fake for the connect front door's injectable `_transports` seam
    (api/integrations_connect.py:44-51) — mirrors
    test_jira_api_token_connect.py's fixture."""

    def __init__(self, myself=None, tenant_info=None):
        self.myself = myself if myself is not None else {}
        self.tenant_info = tenant_info

    def get(self, url, headers=None):
        if "/myself" in url:
            return self.myself
        if "_edge/tenant_info" in url:
            if self.tenant_info is None:
                raise RuntimeError("tenant_info unavailable")
            return self.tenant_info
        return {}


class _NoCliRunner:
    """Stands in for `gh` being absent - the OAuth-app fallback path is what
    this file exercises for GitHub (task f4dd3687's own convention)."""

    def __call__(self, args):
        raise FileNotFoundError("gh")


@pytest.fixture
def app(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setenv("PRISM_AUTH_MODE", "local")
    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("PRISM_JIRA_CLIENT_ID", raising=False)

    from prism_service.api import integrations_connect as connect
    from prism_service.services.integration_store import (
        IntegrationStore, set_integration_store,
    )
    from prism_service.services.jira_auth import JiraAuthStore, set_jira_auth_store
    from prism_service.services.workspace_service import (
        WorkspaceService, set_workspace_service,
    )

    set_workspace_service(WorkspaceService(tmp_path / "workspace.db"))
    set_integration_store(IntegrationStore(str(tmp_path / "integrations.db")))
    set_jira_auth_store(JiraAuthStore(str(tmp_path / "jira_auth.db")))
    connect.configure_state_db(str(tmp_path / "oauth_states.db"))

    api = FastAPI()
    api.include_router(connect.router, prefix="/api/integrations/connect")
    with TestClient(api) as client:
        yield client
    set_integration_store(None)
    set_jira_auth_store(None)
    set_workspace_service(None)


def _connect_jira(app) -> None:
    from prism_service.api import integrations_connect as connect

    myself = {"accountId": "acc-1", "displayName": "Ada",
              "emailAddress": "ada@acme.com"}
    connect.set_transport(
        "jira", _FakeConnectTransport(myself=myself,
                                       tenant_info={"cloudId": "cloud-1"}))
    resp = app.post("/api/integrations/connect/jira/api-token", json={
        "site_url": SITE, "email": "ada@acme.com", "api_token": "tok-1"})
    assert resp.status_code == 200, resp.text


def _seed_run(store, connection_id, container_id, items, status="succeeded"):
    run = store.start_run(SCOPE, connection_id, container_id)
    return store.finish_run(SCOPE, run.id, status, items_processed=items)


def _jira_row(app) -> dict:
    rows = {r["provider"]: r for r in
            app.get("/api/integrations/connect/status").json()["connectors"]}
    return rows["jira"]


# ── AC-1 / AC-2: zero-import last_sync names the in-flow filter ──────────

def test_jira_status_zero_imported_names_the_inflow_filter(app):
    from prism_service.services.integration_store import get_integration_store

    _connect_jira(app)
    resp = app.post("/api/integrations/connect/jira/track", json={"repo": "PROJ"})
    assert resp.status_code == 200, resp.text

    store = get_integration_store()
    conn = store.list_connections(SCOPE, "jira")[0]
    container = store.list_containers(SCOPE, conn.id)[0]
    _seed_run(store, conn.id, container.id, items=0)

    row = _jira_row(app)
    last = row.get("last_sync")
    assert last is not None, (
        "a tracked project with a finished sync must carry its result on "
        f"the status row (reusing IntegrationStore.list_runs), not leave it "
        f"for the client to remember; got {row}")
    assert last["imported"] == 0
    assert last["status"] == "succeeded"
    assert last["container"] == "PROJ"
    reason = (last.get("reason") or "").lower()
    assert "assigned to you" in reason, (
        "a zero-import sync must name the in-flow filter (assignee = "
        f"currentUser()) so a person understands why nothing showed up; "
        f"got {reason!r}")
    assert "done" in reason, (
        f"the filter also excludes Done work; the reason must say so too; "
        f"got {reason!r}")


# ── AC-3: non-zero import needs no filter prose ───────────────────────────

def test_jira_status_nonzero_imported_needs_no_filter_prose(app):
    from prism_service.services.integration_store import get_integration_store

    _connect_jira(app)
    app.post("/api/integrations/connect/jira/track", json={"repo": "PROJ"})
    store = get_integration_store()
    conn = store.list_connections(SCOPE, "jira")[0]
    container = store.list_containers(SCOPE, conn.id)[0]
    _seed_run(store, conn.id, container.id, items=3)

    last = _jira_row(app).get("last_sync")
    assert last is not None, "expected a last_sync row after a finished sync"
    assert last["imported"] == 3
    assert not last.get("reason"), (
        f"a successful non-zero sync has nothing to explain; got "
        f"{last.get('reason')!r}")


# ── AC-1: the LATEST run wins (list_runs ordering, not re-derived) ────────

def test_jira_status_last_sync_is_the_most_recent_run(app):
    from prism_service.services.integration_store import get_integration_store

    _connect_jira(app)
    app.post("/api/integrations/connect/jira/track", json={"repo": "PROJ"})
    store = get_integration_store()
    conn = store.list_connections(SCOPE, "jira")[0]
    container = store.list_containers(SCOPE, conn.id)[0]
    _seed_run(store, conn.id, container.id, items=5)   # older
    _seed_run(store, conn.id, container.id, items=0)   # newer

    last = _jira_row(app).get("last_sync")
    assert last is not None, "expected a last_sync row after a finished sync"
    assert last["imported"] == 0, (
        "status must surface IntegrationStore.list_runs' latest row (ORDER "
        f"BY started_at DESC), not the first/oldest one; got {last}")


# ── AC-4: never-synced is calmly absent, never fabricated ─────────────────

def test_jira_status_before_any_sync_has_no_last_sync(app):
    _connect_jira(app)
    app.post("/api/integrations/connect/jira/track", json={"repo": "PROJ"})
    row = _jira_row(app)
    assert row.get("last_sync") is None, (
        "a tracked project that has never synced must read as calmly "
        f"absent, not fabricate a result; got {row.get('last_sync')}")


# ── AC-5: GitHub gets the same shape, symmetric per the ticket ───────────

def test_github_status_also_carries_a_last_sync_row(app, monkeypatch):
    from prism_service.services import github_cli_auth
    from prism_service.services.integration_store import get_integration_store

    monkeypatch.setenv("PRISM_GITHUB_APP_SLUG", "prism-test-app")
    github_cli_auth.set_cli_credentials(
        github_cli_auth.GithubCliCredentials(runner=_NoCliRunner()))
    try:
        store = get_integration_store()
        conn = store.ensure_connection(SCOPE, "github", "acme",
                                        display_name="acme")
        container = store.ensure_container(
            SCOPE, conn.id, "repository", "acme/widgets",
            display_key="acme/widgets", url="https://github.com/acme/widgets")
        _seed_run(store, conn.id, container.id, items=0)

        rows = {r["provider"]: r for r in
                app.get("/api/integrations/connect/status").json()["connectors"]}
        github_row = rows["github"]
    finally:
        github_cli_auth.set_cli_credentials(None)

    assert github_row["state"] == "connected", github_row
    last = github_row.get("last_sync")
    assert last is not None, (
        "GitHub must get the same durable last-sync treatment as Jira "
        f"(RepoSync is shared, symmetric per the ticket); got {github_row}")
    assert last["container"] == "acme/widgets"
    assert last["imported"] == 0


# ── AC-6 / AC-7 / AC-8: the card itself (source-level, no JS runner) ─────

def _read(p: Path) -> str:
    assert p.exists(), f"expected source missing: {p}"
    return p.read_text(encoding="utf-8")


def _reposync_body() -> str:
    src = _read(_SETTINGS)
    body = src[src.index("function RepoSync("):]
    return body[:body.index("function BacklogPush(")]


def test_reposync_renders_last_sync_durably_not_only_the_transient_note():
    body = _reposync_body()
    assert "connector.last_sync" in body, (
        "the card must read the SERVER's durable last_sync field, not only "
        "the transient `note` local state that vanishes on reload/remount")
    note_start = body.index("{note &&")
    note_block_end = body.index("</p>}", note_start) + len("</p>}")
    note_block = body[note_start:note_block_end]
    assert "connector.last_sync" not in note_block, (
        "the durable last-sync line must render on its own, independent of "
        "this session's transient `note && ...` click state, or it "
        "disappears on reload/remount exactly like `note` does today")


def test_reposync_zero_import_explains_why():
    body = _reposync_body()
    assert ("last_sync.imported === 0" in body
            or "last_sync?.imported === 0" in body), (
        "the card must branch on a ZERO-import last sync to explain why, "
        "not just report a raw count")
    assert "last_sync.reason" in body or "last_sync?.reason" in body, (
        "the zero-import branch must render the SERVER's reason text, not "
        "a client-guessed one")


def test_reposync_nonzero_import_links_to_the_filtered_work_view():
    body = _reposync_body()
    assert "<Link" in body, (
        "a last sync that imported work must LINK to the Work view, not "
        "just state a count")
    i = body.index("/tasks?q=")
    assert i != -1, (
        "the link must filter the Work view to this provider's imported "
        "rows, reusing the existing tag search (imported tasks are tagged "
        "with their provider, work_item_sync.py:252)")
    assert "connector.provider" in body[i:i + 200], (
        "the filter must be scoped to THIS card's own provider, never a "
        "fixed one - GitHub and Jira share this component")


def test_settingspage_imports_link_from_react_router():
    src = _read(_SETTINGS)
    i = src.index('from "react-router-dom"')
    line_start = src.rindex("import", 0, i)
    import_stmt = src[line_start:i + len('from "react-router-dom"')]
    assert "Link" in import_stmt, (
        "RepoSync's last-sync line needs <Link> from react-router-dom; "
        f"current import is: {import_stmt!r}")


def test_taskspage_reads_the_q_param_to_seed_the_search_box():
    src = _read(_TASKS)
    assert "useSearchParams" in src, (
        "TasksPage must read a `q` URL param on mount so a deep link from "
        "the connector card lands pre-filtered, reusing the EXISTING tag "
        "search box (data-work-search) rather than inventing new filter "
        "logic")
    i = src.index('const [query, setQuery] = useState')
    window = src[max(0, i - 400):i + 200]
    assert 'get("q")' in window or "get('q')" in window, (
        "the `query` state (the box the search reuses for provider-tag "
        "filtering) must be seeded from the `q` URL param; nearby source: "
        f"{window!r}")
