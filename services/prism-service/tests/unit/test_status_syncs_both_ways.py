"""Status changes sync BOTH ways without a hand-run push (task 0a9b511f).

Sibling ae67ed5c shipped the closure push and is done, yet the product still
does not sync, because that slice's oracle said "marking that task done AND
RUNNING THE SCOPED PUSH closes the issue". The manual trigger sat inside the
acceptance criterion. So every assertion here is written to be UNSATISFIABLE
by calling push_task_closure directly or by POSTing the connect/push endpoint.

Same split as test_task_mirror_production_wiring.py, for the same reason:
AC-4 asserts PRODUCTION WIRING and injects nothing; the rest assert behaviour
against a fake network, which is legitimate only because the wiring group
exists to catch what a fake hides.
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


def _settle():
    """Wait for the mirror's off-thread close to finish.

    The observer deliberately runs on its own thread so a provider timeout can
    never make task_update look like PRISM hanging. Tests therefore JOIN that
    thread — they must not call close_task directly, because a direct call is
    exactly the hand-run trigger this slice exists to remove.
    """
    import threading

    for t in threading.enumerate():
        if t.name.startswith("task-mirror-close-"):
            t.join(timeout=10)


class _FakeGitHub:
    """Fake network, never fake wiring. Records every close so AC-8 can count."""

    provider = "github"

    def __init__(self) -> None:
        self.closed: list[int] = []

    def create(self, connection, container, title, body="", assignee=""):
        from prism_service.models.integration import ExternalEntityInput

        return ExternalEntityInput(
            entity_kind="issue", remote_id="I_node_1", display_key="#4242",
            title=title, body=body,
            url=f"https://github.com/{REPO}/issues/4242",
            remote_status="open", status_category="open",
            remote_updated_at="2026-08-02T04:00:00Z")

    def close(self, connection, container, entity, **_kw):
        number = int(str(getattr(entity, "display_key", "#0")).lstrip("#") or 0)
        self.closed.append(number)
        return {"state": "closed", "number": number}


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """Real store, real outbox, real TaskService, fake network only."""
    from prism_service.services import task_mirror
    from prism_service.services.integration_outbox import IntegrationOutbox
    from prism_service.services.integration_store import IntegrationStore
    from prism_service.services.task_service import TaskService

    scope = "personal-local-user"
    store = IntegrationStore(str(tmp_path / "integrations.db"))
    outbox = IntegrationOutbox(str(tmp_path / "outbox.db"))
    connection = store.ensure_connection(scope, "github", "siegeon",
                                         display_name="siegeon")
    store.ensure_container(scope, connection.id, "repository", REPO,
                           display_key=REPO, display_name=REPO,
                           url=f"https://github.com/{REPO}")
    svc = TaskService(db_path=str(tmp_path / "tasks.db"), project="prism")
    fake = _FakeGitHub()

    monkeypatch.setattr(task_mirror, "personal_scope", lambda: scope)
    monkeypatch.setattr(
        "prism_service.project_context.get_project",
        lambda project: type("Ctx", (), {"task_svc": svc})())
    monkeypatch.setattr(
        "prism_service.services.integration_store.get_integration_store",
        lambda: store)
    monkeypatch.setattr(
        "prism_service.services.integration_outbox.get_outbox", lambda: outbox)
    monkeypatch.setattr(
        "prism_service.services.sync_prefs.get_sync_preferences",
        lambda: type("P", (), {"enabled": staticmethod(lambda w, p: True)})())
    monkeypatch.setitem(
        __import__("prism_service.api.integrations", fromlist=["_adapters"])
        ._adapters, "github", fake)
    return svc, fake


# ── AC-4: the hook exists at all, and production registers it ─────────────
def test_ac4_task_service_exposes_a_status_observer_hook():
    """Today task_service has ONLY add_create_observer/remove/create_observers.
    Without a status hook there is nowhere to hang the closure push, which is
    the whole reason the outbound leg is unreachable."""
    from prism_service.services import task_service

    for name in ("add_status_observer", "remove_status_observer",
                 "status_observers"):
        assert hasattr(task_service, name), (
            f"task_service has no {name}; a status change cannot notify the "
            "mirror, so marking a task done can never close its issue")


def test_ac4b_updating_status_notifies_observers_with_the_old_value(tmp_path):
    """The ONE chokepoint: every caller (REST, MCP verb, conductor) goes
    through TaskService.update, so an observer here cannot be bypassed."""
    from prism_service.services import task_service
    from prism_service.services.task_service import TaskService

    seen = []
    svc = TaskService(db_path=str(tmp_path / "tasks.db"), project="prism")
    task = svc.create(title="will finish")

    observer = lambda project, t, old: seen.append((project, t.id, old, t.status))  # noqa: E731
    task_service.add_status_observer(observer)
    try:
        svc.update(task.id, status="done")
    finally:
        task_service.remove_status_observer(observer)

    assert seen == [("prism", task.id, "pending", "done")]


def test_ac4c_install_registers_the_closure_observer_too():
    """install() is what production calls (main.py:413-414). It must wire BOTH
    legs, or the create leg works and the closure leg stays unreachable -- the
    exact asymmetry this task exists to remove."""
    from prism_service.services import task_mirror, task_service

    task_mirror.install()
    assert task_mirror._on_task_status_changed in task_service.status_observers()


def test_ac4d_status_reports_the_closure_leg_link_by_link():
    """When a done task does NOT close its issue, which link broke has to be
    answerable from the running process, exactly as it already is for create."""
    from prism_service.services import task_mirror

    info = task_mirror.status()
    assert "closure_observer_installed" in info, (
        "mirror status hides whether the closure leg is wired")


# ── AC-1: marking a task done closes its issue, with NO hand-run push ─────
def test_ac1_marking_a_task_done_closes_its_mirrored_issue(wired):
    """The whole point. Nothing here calls push_task_closure or the connect
    push endpoint; the ONLY trigger is a normal status update."""
    from prism_service.services import task_mirror

    svc, fake = wired
    task = svc.create(title="finish me")
    assert task_mirror.mirror_task("prism", task.id).created is True

    task_mirror.install()
    svc.update(task.id, status="done")
    _settle()

    assert fake.closed == [4242], (
        "a done task did not close its issue through the observer path")


# ── AC-8: the observer does not chatter ───────────────────────────────────
def test_ac8_a_non_status_edit_writes_nothing_to_the_provider(wired):
    from prism_service.services import task_mirror

    svc, fake = wired
    task = svc.create(title="rename me")
    task_mirror.mirror_task("prism", task.id)
    task_mirror.install()

    svc.update(task.id, priority=7, title="renamed")
    _settle()

    assert fake.closed == [], "a non-status edit reached the provider"


def test_ac8b_marking_done_twice_closes_once(wired):
    from prism_service.services import task_mirror

    svc, fake = wired
    task = svc.create(title="finish me once")
    task_mirror.mirror_task("prism", task.id)
    task_mirror.install()

    svc.update(task.id, status="done")
    svc.update(task.id, status="done")
    _settle()

    assert fake.closed == [4242], "the closure leg wrote twice for one task"


# ── scope: github AND jira now, board-wins mapping (task 64ba4755) ────────
#
# RETIRED in place under task 64ba4755 (owner 2026-08-10, superseding the
# 2026-08-02 "we have not done jira yet" scoping): Jira's read-mostly round
# trip is now built (FR-1..FR-6), so extending the reconcile set past
# github is no longer speculation — it is this slice's own FR-7. The old
# name is kept as a comment so the history of what changed stays legible.
def test_inbound_reconcile_now_includes_jira_with_board_wins_mapping(tmp_path):
    from prism_service.services.work_item_sync import STATUS_RECONCILE_PROVIDERS

    assert "github" in STATUS_RECONCILE_PROVIDERS
    assert "jira" in STATUS_RECONCILE_PROVIDERS, (
        "task 64ba4755 FR-7: Jira's own inbound round trip is built, so it "
        "must join the reconcile set")


BOARD_WINS_CASES = [
    # (raw remote status name, status_category, expected task status)
    ("Done", "done", "done"),                    # statusCategory done wins first
    ("Blocked", "unknown", "blocked"),
    ("To Do", "unknown", "pending"),
    ("Open", "unknown", "pending"),
    ("Backlog", "unknown", "pending"),
    ("Reopened", "unknown", "pending"),
    ("Dev", "unknown", "in_progress"),
    ("In Progress", "in_progress", "in_progress"),
    ("Code Review", "unknown", "in_progress"),
    ("In Review", "unknown", "in_progress"),
    ("QA", "unknown", "in_progress"),
    ("Validation", "unknown", "in_progress"),
]


@pytest.mark.parametrize("raw_name,status_category,expected", BOARD_WINS_CASES)
def test_ac7_board_wins_mapping_table(tmp_path, raw_name, status_category, expected):
    """FR-7: statusCategory done wins first; then the board-name map. A
    table-driven pin over every mapped name (plan AC-7)."""
    from prism_service.models.integration import ExternalEntityInput
    from prism_service.services.integration_store import IntegrationStore
    from prism_service.services.task_service import TaskService
    from prism_service.services.work_item_sync import PulledPage, WorkItemSyncService

    scope = "workspace-board-wins"
    store = IntegrationStore(str(tmp_path / "integrations.db"))
    tasks = TaskService(str(tmp_path / "tasks.db"))
    conn = store.ensure_connection(scope, "jira", "cloud-1")
    cont = store.ensure_container(scope, conn.id, "jira_project", "PROJ")

    class _OneIssueAdapter:
        provider = "jira"

        def pull_page(self, connection, container, cursor, page_token):
            entity = ExternalEntityInput(
                entity_kind="jira_issue", remote_id="issue-1", display_key="PROJ-1",
                title="board case", remote_status=raw_name,
                status_category=status_category)
            return PulledPage(entities=[entity])

    svc = WorkItemSyncService(store, intake=tasks)
    svc.register(_OneIssueAdapter())
    svc.pull_container(scope, conn, cont)

    entity = store.list_entities(scope)[0]
    link = store.list_links(scope, entity_id=entity.id)[0]
    task = tasks.get(link.task_id)
    assert task.status == expected, (
        f"{raw_name!r} (status_category={status_category!r}) should map to "
        f"{expected!r}, got {task.status!r}")
    assert task.workflow_step == "" and task.gate_state == "none"


def test_ac7_an_unmapped_board_name_writes_nothing(tmp_path):
    """An unrecognized status name (not statusCategory done, not in the
    board map) must leave the local task exactly as it was — never guess."""
    from prism_service.models.integration import ExternalEntityInput
    from prism_service.services.integration_store import IntegrationStore
    from prism_service.services.task_service import TaskService
    from prism_service.services.work_item_sync import PulledPage, WorkItemSyncService

    scope = "workspace-board-wins-unmapped"
    store = IntegrationStore(str(tmp_path / "integrations.db"))
    tasks = TaskService(str(tmp_path / "tasks.db"))
    conn = store.ensure_connection(scope, "jira", "cloud-1")
    cont = store.ensure_container(scope, conn.id, "jira_project", "PROJ")

    class _OneIssueAdapter:
        provider = "jira"

        def pull_page(self, connection, container, cursor, page_token):
            entity = ExternalEntityInput(
                entity_kind="jira_issue", remote_id="issue-2", display_key="PROJ-2",
                title="unmapped", remote_status="Some Custom Workflow Status",
                status_category="unknown")
            return PulledPage(entities=[entity])

    svc = WorkItemSyncService(store, intake=tasks)
    svc.register(_OneIssueAdapter())
    svc.pull_container(scope, conn, cont)

    entity = store.list_entities(scope)[0]
    link = store.list_links(scope, entity_id=entity.id)[0]
    task = tasks.get(link.task_id)
    assert task.status == "pending", (
        "an unmapped board status must never be guessed at; the task's "
        "original pending status must survive untouched")
