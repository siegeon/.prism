"""RED scaffold — a PRISM task becomes a GitHub issue (task 7cf6a2e5).

The CREATE half of the outbound mirror. Before this: github_rest.py has
_get/issues/pulls/close_issue (PATCH) but no POST; work_item_sync.py
orchestrates pull + close_task but nothing turns a PRISM task into a new
GitHub issue. Mirrors push_task_closure's shape exactly (task ae67ed5c):
the sync switch decides first, the write routes through the outbox with
echo suppression, and the entry point takes exactly one task_id — never a
sweep.

Pre-declared misfire (task.likely_misfire): a bulk sweep that floods the
repo with issues for done/cancelled tasks (288 of them), non-idempotence
(a second run creates a duplicate issue), and assigning every issue
(including new ones) to the owner regardless of the owner's explicit
"unassigned until in_progress" rule. AC-2/AC-3/AC-6 are aimed squarely at
those three failure modes; AC-7 mirrors test_push_closes_mirrored_issue.py's
AC-5 (never a sweep).
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

WS = "workspace-push-create"
REPO = "siegeon/.prism"


def _store(tmp_path):
    from prism_service.services.integration_store import IntegrationStore

    return IntegrationStore(str(tmp_path / "integrations.db"))


def _outbox(tmp_path):
    from prism_service.services.integration_outbox import IntegrationOutbox

    return IntegrationOutbox(str(tmp_path / "outbox.db"))


def _push():
    from prism_service.services.work_item_sync import push_task_creation

    return push_task_creation


def _scan():
    from prism_service.services.work_item_sync import scan_active_tasks

    return scan_active_tasks


def _sync_prefs(enabled: bool):
    def _fn(workspace_id, provider):
        return enabled
    return _fn


def _tracked_repo(store):
    """A connection + container to push into — the same shape /track leaves
    behind (api/integrations_connect.py:track_repo)."""
    conn = store.ensure_connection(WS, "github", "install-create")
    cont = store.ensure_container(WS, conn.id, "repository", REPO,
                                  display_key=REPO)
    return conn, cont


def _existing_active_link(store, task_id="task-already-linked"):
    """A container + entity + ACTIVE link — the shape a prior create (or a
    pull) leaves behind."""
    from prism_service.models.integration import ExternalEntityInput

    conn, cont = _tracked_repo(store)
    entity, _ = store.upsert_entity(
        WS, conn.id, cont.id,
        ExternalEntityInput(entity_kind="issue", remote_id="I_priorlink",
                            display_key="#900",
                            url=f"https://github.com/{REPO}/issues/900"))
    link = store.claim_import_link(WS, entity.id, task_id)
    store.activate_link(WS, link.id)
    return task_id


class _NoGithubClient:
    """A GitHub client double that FAILS the test if it is ever reached —
    used everywhere a refusal must happen before any GitHub contact."""

    def create_issue(self, *a, **k):
        raise AssertionError("must never reach GitHub on a refusal path")


class _RecordingWriteTransport:
    def __init__(self, response=None):
        self.calls: list[tuple] = []
        self._response = response or {
            "node_id": "I_new1", "number": 501, "state": "open",
            "title": "", "body": "",
            "html_url": f"https://github.com/{REPO}/issues/501",
            "updated_at": "2026-07-30T00:00:00Z",
        }

    def __call__(self, url, token, body):
        self.calls.append((url, token, dict(body)))
        return self._response


def _bare_adapter(client=None):
    from prism_service.services.github_work import GitHubWorkAdapter

    return GitHubWorkAdapter(client or _NoGithubClient())


# ── AC-1: sync switched off -> refuse before any GitHub/store contact ──

def test_sync_disabled_refuses_before_touching_github(tmp_path):
    push = _push()
    store = _store(tmp_path)
    outbox = _outbox(tmp_path)
    registry = {"github": _bare_adapter()}  # would raise if reached

    result = push(store, outbox, registry, _sync_prefs(False),
                  WS, "task-new", task_status="pending", title="A new task")

    assert result.eligible is False
    assert result.created is False
    assert "off" in result.reason.lower() or "disabled" in result.reason.lower()
    assert outbox.pending_items(WS) == []


# ── AC-2: done/cancelled tasks are refused in code, never touched ──────

def test_done_task_is_refused_before_registry_access(tmp_path):
    push = _push()
    store = _store(tmp_path)
    outbox = _outbox(tmp_path)
    registry = {"github": _bare_adapter()}  # would raise if reached

    result = push(store, outbox, registry, _sync_prefs(True),
                  WS, "task-done", task_status="done", title="Done task")

    assert result.eligible is False
    assert result.created is False
    assert "active" in result.reason.lower()


def test_cancelled_task_is_refused_before_registry_access(tmp_path):
    push = _push()
    store = _store(tmp_path)
    outbox = _outbox(tmp_path)
    registry = {"github": _bare_adapter()}  # would raise if reached

    result = push(store, outbox, registry, _sync_prefs(True),
                  WS, "task-cancelled", task_status="cancelled",
                  title="Cancelled task")

    assert result.eligible is False
    assert result.created is False
    assert "active" in result.reason.lower()


def test_active_status_check_precedes_any_registry_access():
    """Enforced in CODE, not merely by convention — the active-status guard
    must appear before the registry is ever consulted (the misfire this
    task exists to prevent: a run reaching done/cancelled tasks)."""
    import inspect

    from prism_service.services import work_item_sync as wis

    source = inspect.getsource(wis.push_task_creation)
    status_at = source.find("ACTIVE_STATUSES")
    registry_at = source.find("registry.get(")
    assert status_at != -1, "push_task_creation must reference ACTIVE_STATUSES"
    assert registry_at != -1, "push_task_creation must consult the registry"
    assert status_at < registry_at, (
        "the active-status check must precede any registry access")


# ── AC-3: a task with an existing link is refused (single-call idempotence) ──

def test_already_linked_task_is_refused_and_github_never_touched(tmp_path):
    push = _push()
    store = _store(tmp_path)
    outbox = _outbox(tmp_path)
    task_id = _existing_active_link(store)
    registry = {"github": _bare_adapter()}  # would raise if reached

    result = push(store, outbox, registry, _sync_prefs(True),
                  WS, task_id, task_status="pending", title="Already linked")

    assert result.eligible is False
    assert result.created is False
    assert "link" in result.reason.lower()
    assert outbox.pending_items(WS) == []


# ── AC-4: a pure, read-only scan reports exactly what would be created ──

def test_scan_active_tasks_names_would_create_and_every_skip_bucket():
    scan = _scan()
    rows = [
        ("t1", "pending", False),
        ("t2", "in_progress", False),
        ("t3", "done", False),
        ("t4", "cancelled", False),
        ("t5", "blocked", True),
        ("t6", "pending", True),
    ]

    report = scan(rows)

    assert set(report.would_create) == {"t1", "t2"}
    assert report.skipped_done == ["t3"]
    assert report.skipped_cancelled == ["t4"]
    assert set(report.skipped_already_linked) == {"t5", "t6"}


def test_scan_active_tasks_cannot_reach_github_structurally():
    """No store/outbox/registry parameter exists at all — the dry-run
    reporting path is read-only BY CONSTRUCTION, never merely by discipline
    (the 0784729f-shaped misfire: a report that could silently become a
    sweep)."""
    import inspect

    from prism_service.services import work_item_sync as wis

    params = list(inspect.signature(wis.scan_active_tasks).parameters)
    assert not any(p in params for p in ("store", "outbox", "registry")), (
        "scan_active_tasks must take no collaborator capable of reaching "
        "GitHub — it is a pure classifier")


# ── AC-5: a live create reaches the PRODUCTION-registered adapter ─────

def test_push_creates_through_the_production_registered_adapter(
        tmp_path, monkeypatch):
    from prism_service.api.integrations import _adapters

    adapter = _adapters.get("github")
    assert adapter is not None, (
        "production must register a github adapter (task f4dd3687)")

    class _FakeCreds:
        def installation_token(self, _=None):
            return "gho_faketoken"

    monkeypatch.setattr(adapter, "_credentials", _FakeCreds())
    write_transport = _RecordingWriteTransport()
    monkeypatch.setattr(adapter._client, "_write_transport", write_transport)

    push = _push()
    store = _store(tmp_path)
    outbox = _outbox(tmp_path)
    _tracked_repo(store)

    result = push(store, outbox, _adapters, _sync_prefs(True),
                  WS, "task-live1", task_status="pending",
                  title="[prism-sync-test] example", body="desc",
                  dry_run=False)

    assert result.created is True, result.reason
    assert write_transport.calls, (
        "the real GithubRestClient.create_issue must be reached")
    url, token, body = write_transport.calls[0]
    assert url.endswith(f"/repos/{REPO}/issues")
    assert body["title"] == "[prism-sync-test] example"
    assert body["body"] == "desc"
    assert body["assignees"] == [], "pending + no override -> unassigned"
    assert token == "gho_faketoken"

    # Routed through the outbox (not a direct call at the mutation site).
    assert outbox.pending_items(WS) == []
    marker = "I_new1:2026-07-30T00:00:00Z"
    assert outbox.is_echo(WS, marker) is True

    # The counterpart is recorded the same way an import does, so the NEXT
    # call sees it as already linked (AC-3's idempotence, exercised here
    # end to end).
    links = store.list_links(WS, task_id="task-live1")
    assert any(link.state == "active" for link in links)

    write_transport.calls.clear()
    result2 = push(store, outbox, _adapters, _sync_prefs(True),
                   WS, "task-live1", task_status="pending",
                   title="[prism-sync-test] example", dry_run=False)

    assert result2.created is False
    assert result2.eligible is False
    assert "link" in result2.reason.lower()
    assert write_transport.calls == [], (
        "a second call on an already-linked task must never reach GitHub")


# ── AC-6: assignment follows the owner's rule exactly ──────────────────

def test_in_progress_with_an_actor_assigns_that_login(tmp_path, monkeypatch):
    from prism_service.api.integrations import _adapters

    adapter = _adapters.get("github")
    monkeypatch.setattr(adapter, "_credentials", None)
    write_transport = _RecordingWriteTransport()
    monkeypatch.setattr(adapter._client, "_write_transport", write_transport)

    push = _push()
    store = _store(tmp_path)
    outbox = _outbox(tmp_path)
    _tracked_repo(store)

    result = push(store, outbox, _adapters, _sync_prefs(True),
                  WS, "task-inprogress", task_status="in_progress",
                  title="Started task", assignee="siegeon", dry_run=False)

    assert result.created is True, result.reason
    _, _, body = write_transport.calls[0]
    assert body["assignees"] == ["siegeon"]


def test_pending_with_no_actor_goes_up_unassigned(tmp_path, monkeypatch):
    from prism_service.api.integrations import _adapters

    adapter = _adapters.get("github")
    monkeypatch.setattr(adapter, "_credentials", None)
    write_transport = _RecordingWriteTransport()
    monkeypatch.setattr(adapter._client, "_write_transport", write_transport)

    push = _push()
    store = _store(tmp_path)
    outbox = _outbox(tmp_path)
    _tracked_repo(store)

    result = push(store, outbox, _adapters, _sync_prefs(True),
                  WS, "task-pending-noassignee", task_status="pending",
                  title="Fresh task", dry_run=False)

    assert result.created is True, result.reason
    _, _, body = write_transport.calls[0]
    assert body["assignees"] == []


def test_explicit_assignee_override_wins_regardless_of_status(
        tmp_path, monkeypatch):
    """The pre-existing-backlog case: a task that predates this capability
    is pending (never started) but still assigned to the connected account,
    because the caller says so explicitly."""
    from prism_service.api.integrations import _adapters

    adapter = _adapters.get("github")
    monkeypatch.setattr(adapter, "_credentials", None)
    write_transport = _RecordingWriteTransport()
    monkeypatch.setattr(adapter._client, "_write_transport", write_transport)

    push = _push()
    store = _store(tmp_path)
    outbox = _outbox(tmp_path)
    _tracked_repo(store)

    result = push(store, outbox, _adapters, _sync_prefs(True),
                  WS, "task-backfill", task_status="pending",
                  title="Pre-existing backlog item", assignee="siegeon",
                  dry_run=False)

    assert result.created is True, result.reason
    _, _, body = write_transport.calls[0]
    assert body["assignees"] == ["siegeon"]


# ── AC-7: the entry point is scoped to ONE task_id, never a sweep ──────

def test_push_entry_point_never_sweeps_active_tasks():
    import inspect

    from prism_service.services import work_item_sync as wis

    sig = inspect.signature(wis.push_task_creation)
    params = list(sig.parameters)
    assert "task_id" in params
    assert not any(p in params for p in ("task_ids", "tasks", "task_id_list")), (
        "the push entry point must take exactly one task_id, never a "
        "collection — an unattended sweep is the dangerous misfire named on "
        "this task")

    source = inspect.getsource(wis.push_task_creation)
    assert "pending_items(" not in source, (
        "the push entry point must never drain the whole outbox in one call")
