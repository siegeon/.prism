"""RED scaffold — flipping the sync switch ON pushes the backlog (epic
02672417, GAP-A).

Owner intent: PRISM tasks are the RECORD, GitHub issues are a MIRROR. AC-4
says turning the Settings-page SyncSwitch ON (``PUT
/api/integrations/connect/{provider}/sync``) is what starts the outbound
backlog push for pre-existing ACTIVE tasks. Today ``set_sync``
(api/integrations_connect.py) only ever calls
``get_sync_preferences().set_enabled(...)`` and returns — nothing reads the
task board, nothing calls ``push_task_creation``. ``scan_active_tasks``
(work_item_sync.py) exists and classifies rows correctly but has ZERO
production callers, so it can never actually reach GitHub.

ASSERT THE AFFORDANCE A PERSON USES: every test below drives the real
``PUT .../sync`` endpoint through a mounted FastAPI router — never a bare
call to a not-yet-wired helper function — because that endpoint IS the
SyncSwitch's own handler (lib/api.ts setConnectorSync).

Pre-declared misfire (this task's stop_if): a sweep that is not
edge-triggered (re-fires on every ON->ON write) or has no cap. AC-7 and
AC-9 are aimed squarely at that.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

REPO = "siegeon/.prism"
ACCOUNT = "siegeon"
TOKEN = "gho_pretend_token_value_0123456789"
_STATUS_OK = f"""github.com
  x Logged in to github.com account {ACCOUNT} (keyring)
  - Token scopes: 'repo'
"""


class FakeRunner:
    """The `gh` process boundary only — same double test_sync_is_opt_in.py
    uses, so the connected account resolves the same way production does."""

    def __call__(self, args):
        if args[:3] == ["gh", "auth", "status"]:
            return 0, _STATUS_OK, ""
        if args[:3] == ["gh", "auth", "token"]:
            return 0, TOKEN + "\n", ""
        raise AssertionError(f"unexpected command: {args}")


class _FakeGithub:
    """Stands in for the network only. AC-2's real point is that the SWITCH
    reaches this at all — the real adapter is proven registered elsewhere
    (test_task_mirror_production_wiring.py AC-2)."""

    provider = "github"

    def __init__(self):
        self.calls: list[dict] = []

    def create(self, connection, container, title, body="", assignee=""):
        from prism_service.models.integration import ExternalEntityInput

        n = len(self.calls) + 1
        self.calls.append({"title": title, "assignee": assignee})
        return ExternalEntityInput(
            entity_kind="issue", remote_id=f"I_bl_{n}",
            display_key=f"#{9000 + n}", title=title, body=body,
            url=f"https://github.com/{REPO}/issues/{9000 + n}",
            remote_status="open", status_category="open",
            remote_updated_at="2026-08-09T00:00:00Z")


class _ExplodingJira:
    """AC-11: flipping GITHUB's switch must never reach jira, even though
    both providers share the SAME endpoint shape and the same registry.
    Records rather than raises — a raise inside the sweep's background
    thread is exactly the kind of exception production is written to
    swallow (task_mirror.py: "never raises, never blocks"), so it would
    never surface as a test failure; recording and asserting on the list
    afterward is not fooled by that."""

    provider = "jira"

    def __init__(self):
        self.calls: list[str] = []

    def create(self, connection, container, title, body="", assignee=""):
        self.calls.append(title)
        raise AssertionError(
            "flipping github's sync switch must never reach the jira "
            "adapter — jira has not been switched on")


def _wait_until(cond, timeout=3.0, interval=0.02):
    """The sweep fires off the request thread (owner-required, so a slow
    provider never hangs the SyncSwitch click). Poll for the observable
    side effect instead of assuming synchronous completion."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(interval)
    return cond()


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """A real store, a real outbox, a real TaskService, a real sync-prefs
    db, mounted behind the REAL /sync router — fake network only. Mirrors
    tests/unit/test_task_mirror_production_wiring.py's `wired` fixture and
    tests/integration/test_sync_is_opt_in.py's `app` fixture, combined: this
    task's whole point is that those two surfaces are not actually wired to
    each other yet.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setenv("PRISM_AUTH_MODE", "local")
    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("PRISM_GITHUB_APP_SLUG", raising=False)
    monkeypatch.delenv("PRISM_GITHUB_CLIENT_ID", raising=False)

    from prism_service import project_context
    from prism_service.api import integrations as integrations_api
    from prism_service.api import integrations_connect as connect
    from prism_service.services import github_cli_auth, sync_prefs
    from prism_service.services.integration_outbox import (
        IntegrationOutbox, set_outbox)
    from prism_service.services.integration_store import (
        IntegrationStore, set_integration_store)
    from prism_service.services.task_service import TaskService
    from prism_service.services.workspace_service import (
        WorkspaceService, set_workspace_service)

    set_workspace_service(WorkspaceService(tmp_path / "workspace.db"))
    store = IntegrationStore(str(tmp_path / "integrations.db"))
    set_integration_store(store)
    connect.configure_state_db(str(tmp_path / "oauth_states.db"))
    github_cli_auth.set_cli_credentials(
        github_cli_auth.GithubCliCredentials(runner=FakeRunner()))

    outbox = IntegrationOutbox(str(tmp_path / "outbox.db"))
    set_outbox(outbox)

    sync_prefs.set_sync_preferences(
        sync_prefs.SyncPreferences(str(tmp_path / "sync_prefs.db")))

    svc = TaskService(db_path=str(tmp_path / "tasks.db"), project="prism")
    monkeypatch.setattr(
        project_context, "get_project",
        lambda project: type("Ctx", (), {"task_svc": svc})())

    fake_github = _FakeGithub()
    fake_jira = _ExplodingJira()
    monkeypatch.setitem(integrations_api._adapters, "github", fake_github)
    monkeypatch.setitem(integrations_api._adapters, "jira", fake_jira)

    # personal-local-user is what current_principal resolves to in local
    # mode (AuthService.LOCAL_USER_ID) — the same scope /track leaves a
    # tracked repo under.
    scope = "personal-local-user"
    connection = store.ensure_connection(scope, "github", "install-bl",
                                         display_name="siegeon")
    store.ensure_container(scope, connection.id, "repository", REPO,
                           display_key=REPO, display_name=REPO,
                           url=f"https://github.com/{REPO}")

    api = FastAPI()
    api.include_router(connect.router, prefix="/api/integrations/connect")
    with TestClient(api) as client:
        yield type("Rig", (), {
            "client": client, "svc": svc, "store": store, "scope": scope,
            "github": fake_github, "jira": fake_jira,
        })()

    sync_prefs.set_sync_preferences(None)
    github_cli_auth.set_cli_credentials(None)
    set_integration_store(None)
    set_outbox(None)
    set_workspace_service(None)


def _active(svc, title, status="pending"):
    task = svc.create(title=title)
    if status != "pending":
        svc.update(task.id, status=status)
    return task


# ── AC-2 / AC-4: the switch itself is what starts the push ────────────────

def test_flipping_the_switch_on_pushes_preexisting_active_tasks(rig):
    """The affordance a person uses: PUT .../github/sync. Two tasks predate
    switch-on (pending, in_progress) — both must reach GitHub, ASSIGNED to
    the connected account (AC-2), once the switch itself is flipped, with
    no other call made."""
    pending = _active(rig.svc, "backlog task, still pending")
    active = _active(rig.svc, "backlog task, in progress", status="in_progress")

    resp = rig.client.put("/api/integrations/connect/github/sync",
                          params={"project": "prism"},
                          json={"enabled": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["sync_enabled"] is True

    ok = _wait_until(lambda: len(rig.github.calls) >= 2)
    assert ok, (
        f"switching sync on never reached the github adapter for the "
        f"pre-existing backlog (calls so far: {rig.github.calls})")
    assert {c["title"] for c in rig.github.calls} == {
        pending.title, active.title}
    assert all(c["assignee"] == ACCOUNT for c in rig.github.calls), (
        "AC-2: pre-existing backlog tasks must be pushed ASSIGNED to the "
        f"connected account; got {rig.github.calls}")


# ── AC-9: done/cancelled never exported; the sweep does not balloon ───────

def test_done_and_cancelled_backlog_is_never_exported(rig):
    active = _active(rig.svc, "still open backlog work")
    _active(rig.svc, "already finished", status="done")
    _active(rig.svc, "abandoned", status="cancelled")

    rig.client.put("/api/integrations/connect/github/sync",
                   params={"project": "prism"}, json={"enabled": True})

    ok = _wait_until(lambda: len(rig.github.calls) >= 1)
    assert ok, "the one active backlog task was never pushed"
    time.sleep(0.2)  # let a wrongly-included done/cancelled call surface
    assert [c["title"] for c in rig.github.calls] == [active.title], (
        "a done or cancelled task reached github: "
        f"{[c['title'] for c in rig.github.calls]}")


def test_the_sweep_has_a_cap_and_does_not_try_to_push_everything(rig):
    """Structural safety, not an exact number: the recorded misfire is an
    unattended drain that floods the repo the moment the switch flips. A
    real cap must stay well under the full backlog size."""
    n = 60
    for i in range(n):
        _active(rig.svc, f"bulk backlog task {i}")

    rig.client.put("/api/integrations/connect/github/sync",
                   params={"project": "prism"}, json={"enabled": True})

    ok = _wait_until(lambda: len(rig.github.calls) >= 1, timeout=5.0)
    assert ok, "the sweep never pushed anything at all"
    time.sleep(0.5)
    assert len(rig.github.calls) < n, (
        f"the sweep pushed all {len(rig.github.calls)}/{n} backlog tasks "
        "in one shot; it must be capped, not unlimited")


# ── AC-7: the sweep fires on False->True only, never on ON->ON ────────────

def test_resyncing_while_already_on_does_not_refire_the_sweep(rig):
    """Re-running sync (an already-True write) must create nothing new and
    duplicate nothing — the sweep is edge-triggered, not level-triggered."""
    _active(rig.svc, "backlog task")

    rig.client.put("/api/integrations/connect/github/sync",
                   params={"project": "prism"}, json={"enabled": True})
    ok = _wait_until(lambda: len(rig.github.calls) >= 1)
    assert ok, "the first False->True flip never pushed the backlog"
    first_count = len(rig.github.calls)

    # ON -> ON: same value written again.
    resp = rig.client.put("/api/integrations/connect/github/sync",
                          params={"project": "prism"}, json={"enabled": True})
    assert resp.status_code == 200, resp.text
    time.sleep(0.3)
    assert len(rig.github.calls) == first_count, (
        "an ON->ON sync write re-fired the sweep and made new github "
        f"calls: {rig.github.calls}")


def test_toggle_off_then_on_again_does_not_duplicate_already_linked_tasks(rig):
    """False->True->False->True: the second True must not re-create issues
    for tasks the first sweep already linked."""
    _active(rig.svc, "backlog task")

    rig.client.put("/api/integrations/connect/github/sync",
                   params={"project": "prism"}, json={"enabled": True})
    ok = _wait_until(lambda: len(rig.github.calls) >= 1)
    assert ok
    first_count = len(rig.github.calls)

    rig.client.put("/api/integrations/connect/github/sync",
                   params={"project": "prism"}, json={"enabled": False})
    rig.client.put("/api/integrations/connect/github/sync",
                   params={"project": "prism"}, json={"enabled": True})
    time.sleep(0.5)
    assert len(rig.github.calls) == first_count, (
        "toggling off then back on duplicated an already-linked task: "
        f"{rig.github.calls}")


# ── AC-11: jira contracts are untouched ────────────────────────────────────

def test_flipping_github_sync_never_reaches_the_jira_adapter(rig):
    """jira has NOT been switched on. `_ExplodingJira.create` raises if it
    is ever reached, so this fails loudly rather than silently passing on
    an untouched double."""
    _active(rig.svc, "backlog task")

    resp = rig.client.put("/api/integrations/connect/github/sync",
                          params={"project": "prism"}, json={"enabled": True})
    assert resp.status_code == 200, resp.text
    ok = _wait_until(lambda: len(rig.github.calls) >= 1)
    assert ok, "the github sweep never ran at all"
    time.sleep(0.3)  # give a wrongly-provider-neutral sweep a chance to fire
    assert rig.jira.calls == []
