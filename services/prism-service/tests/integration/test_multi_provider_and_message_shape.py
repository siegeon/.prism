"""The workspace connects beyond GitHub (task 33798164).

Two things, proven separately so a fake cannot hide either gap:

  AC-1 / AC-2 / AC-3 assert PRODUCTION WIRING for a SECOND provider. They
  inject nothing: they call the real `register_builtin_adapters()` (or boot
  the real app) and read the real registry / the real mirror endpoint. This
  is the exact AC-1/AC-2 shape `test_task_mirror_production_wiring.py`
  already established for GitHub, extended to prove Jira is no longer
  cosmetic (task f4dd3687's lesson, repeated once already for a provider).

  AC-4 / AC-5 assert the real `JiraWorkAdapter` + real `JiraClient` complete
  a sync and degrade cleanly on failure, faking only the network transport
  (the seam `jira_client.py` itself documents as the injection point) --
  never a stand-in adapter, so the actual production code under
  register_builtin_adapters() is what gets exercised.

  AC-6 pins the STATUS_RECONCILE_PROVIDERS regression this task must not
  repeat (task 0a9b511f): registering Jira must never extend inbound
  status-reconcile to it.

  AC-7 / AC-8 prove ExternalMessage is a real, persisted sibling of
  ExternalEntity that structurally cannot mint an intake task.

  AC-9 proves the neighbouring suites this change touches stay green
  together, not just each in isolation.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

WS = "workspace-33798164"


# ── AC-1 / AC-2: production registers Jira, from real code, no injection ──

def test_ac1_jira_adapter_is_registered_by_production_code():
    """The registry a sync reads must be populated by importing the
    product, exactly as AC-2 in test_task_mirror_production_wiring.py
    proves for github -- the original defect (task f4dd3687) was an empty
    registry in production while every test passed one in."""
    from prism_service.api import integrations

    integrations.reset_adapters()
    integrations.register_builtin_adapters()

    snapshot = integrations.adapters_snapshot()
    assert "github" in snapshot and "jira" in snapshot, (
        f"production registered only {sorted(snapshot)}: "
        f"{integrations.adapter_registration_errors}")
    assert "jira" not in integrations.adapter_registration_errors


def test_ac2_jira_registration_lives_inside_the_auto_invoked_function():
    """register_builtin_adapters() is the function actually called at
    import time (line-level pin already exists for that fact in
    test_ac2b_registration_runs_on_import_not_only_when_called); this pins
    that the JIRA branch lives INSIDE that same function body, not behind
    some other call nothing invokes."""
    from prism_service.api import integrations

    source = Path(integrations.__file__).read_text(encoding="utf-8")
    start = source.index("def register_builtin_adapters")
    end = source.index("\nregister_builtin_adapters()")
    body = source[start:end]
    assert "JiraWorkAdapter" in body, (
        "jira registration must live inside register_builtin_adapters(), "
        "the function invoked at import time -- registering it anywhere "
        "else makes the registration cosmetic")


# ── AC-3: the live mirror surface reports more than one adapter ───────────

@pytest.fixture
def quiet_boot(monkeypatch):
    """Boot the REAL lifespan without its periodic workers (mirrors
    test_task_mirror_production_wiring.py's fixture of the same name and
    the same reasoning: booting real workers here hung a CI run for 26
    minutes in sqlite_db.connect once before)."""
    for name, value in (("PRISM_WATCHDOG", "off"),
                        ("PRISM_DRIFT_INTERVAL", "0"),
                        ("PRISM_GATE_ADJUDICATOR_INTERVAL", "0"),
                        ("PRISM_EVENT_POOL_INTERVAL", "0"),
                        ("PRISM_GRAPH_ENRICH_WORKER", "off"),
                        ("PRISM_SQLITE_MAINT_INTERVAL_S", "0")):
        monkeypatch.setenv(name, value)

    class _RefuseLock:
        def exists(self) -> bool:
            return False

        def write_text(self, *_a, **_k):
            raise OSError("workers deliberately not started under test")

        def unlink(self, *_a, **_k) -> None:
            return None

    monkeypatch.setattr("prism_service.main._LOCK_FILE", _RefuseLock())


def test_ac3_live_mirror_reports_more_than_one_adapter(quiet_boot):
    from fastapi.testclient import TestClient

    from prism_service.main import app

    with TestClient(app) as client:
        body = client.get("/api/integrations/connect/mirror").json()

    assert len(body["adapters"]) > 1, (
        f"mirror still reports a single adapter: {body}")
    assert "jira" in body["adapters"]


# ── AC-4 / AC-5: the real adapter, real client, fake transport only ───────

class _FakeTransport:
    """Stands in for the network, never for JiraClient/JiraWorkAdapter --
    the same posture test_task_mirror_production_wiring.py takes for
    _FakeGitHub: fake only what would otherwise open a real connection."""

    def __init__(self, page: dict) -> None:
        self._page = page
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, headers: dict) -> dict:
        self.calls.append((url, headers))
        return self._page


def _real_adapter(page: dict, token: str, *, raise_on_token: bool = False):
    from prism_service.services.jira_client import JiraClient
    from prism_service.services.jira_work import JiraWorkAdapter

    client = JiraClient(transport=_FakeTransport(page))

    def _token_provider(connection):
        if raise_on_token:
            raise RuntimeError("refresh token expired, no access token minted")
        return token

    return JiraWorkAdapter(client, access_token_provider=_token_provider), client


def test_ac4_a_jira_container_completes_a_sync_with_a_sanitized_receipt(tmp_path):
    from prism_service.services.integration_store import IntegrationStore
    from prism_service.services.task_service import TaskService
    from prism_service.services.work_item_sync import WorkItemSyncService

    page = {
        "issues": [{
            "id": "20001", "key": "AC4-1",
            "fields": {"summary": "Sync me for real", "status": {"name": "To Do"},
                       "assignee": None, "updated": "2026-08-01T00:00:00.000+0000"},
        }],
        "nextPageToken": None,
    }
    secret = "ac4-secret-access-token"
    adapter, client = _real_adapter(page, secret)

    db_path = str(tmp_path / "integrations.db")
    store = IntegrationStore(db_path)
    tasks = TaskService(str(tmp_path / "tasks.db"))
    conn = store.ensure_connection(WS, "jira", "cloud-ac4")
    cont = store.ensure_container(WS, conn.id, "jira_project", "AC4")

    svc = WorkItemSyncService(store, intake=tasks)
    svc.register(adapter)
    run = svc.pull_container(WS, conn, cont)

    assert run.status == "succeeded", run.error_code
    receipts = store.list_receipts(WS, run.id)
    assert receipts, "no durable receipt recorded for the sync"

    # the REAL JiraClient built the request (proves the real class ran,
    # not a stand-in for it)
    assert client._transport.calls, "the real JiraClient never called the transport"
    _, headers = client._transport.calls[0]
    assert headers.get("Authorization") == f"Bearer {secret}"

    # sanitized: the access token never reaches durable storage
    raw = sqlite3.connect(db_path)
    try:
        dump = "\n".join(raw.iterdump())
    finally:
        raw.close()
    assert secret not in dump


def test_ac5_a_jira_token_failure_degrades_to_a_clean_adapter_error(tmp_path):
    from prism_service.models.integration import ADAPTER_ERROR
    from prism_service.services.integration_store import IntegrationStore
    from prism_service.services.task_service import TaskService
    from prism_service.services.work_item_sync import WorkItemSyncService

    adapter, _client = _real_adapter({"issues": []}, token="unused", raise_on_token=True)

    store = IntegrationStore(str(tmp_path / "integrations.db"))
    tasks = TaskService(str(tmp_path / "tasks.db"))
    conn = store.ensure_connection(WS, "jira", "cloud-ac5")
    cont = store.ensure_container(WS, conn.id, "jira_project", "AC5")

    svc = WorkItemSyncService(store, intake=tasks)
    svc.register(adapter)
    run = svc.pull_container(WS, conn, cont)

    assert run.status == "failed"
    assert run.error_code == ADAPTER_ERROR, (
        "a token failure must surface as the canonical adapter_error code, "
        f"got {run.error_code!r} -- a raw exception would have propagated "
        "out of pull_container instead of reaching finish_run at all")


# ── AC-6: registering Jira must never extend inbound status-reconcile ─────

def test_ac6_jira_registration_never_extends_status_reconcile():
    """Regression pin for the exact 0a9b511f mistake: a shared code path
    silently inheriting a contract change for a provider nobody had
    connected yet. Registering Jira here must not touch this set."""
    from prism_service.services.work_item_sync import STATUS_RECONCILE_PROVIDERS

    assert STATUS_RECONCILE_PROVIDERS == frozenset({"github"})


# ── AC-7 / AC-8: ExternalMessage is a real sibling, never an intake path ──

def test_ac7_external_message_is_a_real_persisted_sibling_shape(tmp_path):
    from prism_service.models.integration import ExternalMessageInput
    from prism_service.services.integration_store import IntegrationStore

    db_path = str(tmp_path / "integrations.db")
    store = IntegrationStore(db_path)
    conn = store.ensure_connection(WS, "jira", "cloud-ac7")

    message_in = ExternalMessageInput(
        remote_id="msg-1", thread_id="thread-1", sender="alice@example.com",
        subject="hi", body="just checking in", remote_created_at="2026-08-01T00:00:00Z")
    saved, created = store.upsert_message(WS, conn.id, "jira", message_in)
    assert created is True
    assert saved.remote_id == "msg-1" and saved.subject == "hi"

    # replay is idempotent, same identity
    again, created_again = store.upsert_message(WS, conn.id, "jira", message_in)
    assert created_again is False
    assert again.id == saved.id
    assert len(store.list_messages(WS)) == 1

    # durable: a FRESH IntegrationStore over the same db path reads it back
    reopened = IntegrationStore(db_path)
    got = reopened.get_message(WS, saved.id)
    assert got is not None and got.body == "just checking in"


def test_ac8_importing_a_message_never_mints_an_intake_task(tmp_path):
    from prism_service.models.integration import ExternalMessageInput
    from prism_service.services.integration_store import IntegrationStore
    from prism_service.services.task_service import TaskService

    store = IntegrationStore(str(tmp_path / "integrations.db"))
    tasks = TaskService(str(tmp_path / "tasks.db"))
    conn = store.ensure_connection(WS, "jira", "cloud-ac8")

    before = len(tasks.list())
    store.upsert_message(WS, conn.id, "jira", ExternalMessageInput(
        remote_id="msg-2", subject="ordinary mail", body="nothing to do here"))
    after = len(tasks.list())

    assert before == 0 and after == 0, (
        "importing an ExternalMessage must never create a local task")


def test_ac8b_external_message_has_no_path_into_the_intake_protocol():
    """Structural guard: the ExternalMessage/ExternalMessageInput block
    itself must never reference the WorkItemAdapter/intake machinery, so a
    future edit cannot quietly wire a message into it."""
    source = Path(
        _SERVICE_ROOT / "prism_service" / "models" / "integration.py"
    ).read_text(encoding="utf-8")
    start = source.index("class ExternalMessage:")
    end = source.index("class WorkItemExternalLink:")
    block = source[start:end]
    for forbidden in ("ensure_external_intake", "WorkItemAdapter", "entity_kind"):
        assert forbidden not in block, (
            f"ExternalMessage block references {forbidden!r} -- messages "
            "must stay structurally separate from the intake path")


# ── AC-9: neighbouring suites stay green together ──────────────────────────

_JIRA_IMPORT = "tests/unit/test_jira_work_import.py"
_MIRROR_WIRING = "tests/unit/test_task_mirror_production_wiring.py"
_EXTERNAL_SYNC = "tests/integration/test_external_work_sync.py"
_REGISTRY_ISOLATION = "tests/integration/test_adapter_registration_is_isolated.py"


def test_ac9_neighbouring_suites_stay_green_together():
    """Run the suites this change's blast radius actually touches (the
    unmodified jira import contract, the github production-wiring pin, the
    generic sync engine, and the registry-isolation mechanism) in ONE
    pytest process, exactly the way CLAUDE.md warns a per-file green does
    not prove -- this file is deliberately NOT in the list to avoid this
    test recursively spawning itself."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q",
         _JIRA_IMPORT, _MIRROR_WIRING, _EXTERNAL_SYNC, _REGISTRY_ISOLATION],
        cwd=str(_SERVICE_ROOT),
        capture_output=True,
        text=True,
        timeout=300,
    )
    tail = result.stdout[-6000:] if result.stdout else ""
    assert result.returncode == 0, (
        f"neighbouring suites must stay green alongside this change; rc="
        f"{result.returncode}\nstdout:\n{tail}\nstderr:\n{result.stderr[-2000:]}")
    assert " failed" not in tail and " error" not in tail.lower(), (
        f"neighbouring suites exited 0 but reported failures/errors:\n{tail}")
