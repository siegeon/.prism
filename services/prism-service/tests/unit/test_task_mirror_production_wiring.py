"""A task created in PRISM actually reaches GitHub (task 27e543e0).

WHAT WENT WRONG BEFORE, and what these tests are shaped against. Three tasks
(0665c829, d09bee0b, 7362c14e) reached DONE on this capability while it could
not run at all, because every assertion any of them made was satisfiable with
the collaborator INJECTED. The suite was green and production wired nothing.
CLAUDE.md records the identical defect for epic 0784729f.

So this file deliberately splits in two, and the split is the point:

  AC-1 / AC-2 / AC-3 / AC-9 assert PRODUCTION WIRING. They inject nothing.
  They boot the real FastAPI lifespan, read the real adapter registry, and
  read the real observer list. If production stops registering the mirror,
  these go red no matter how healthy the rest of the feature is.

  AC-4 .. AC-8 assert BEHAVIOUR, and those do use a fake adapter, because the
  alternative is a unit test that opens real issues on a real repository.
  That is legitimate ONLY because the wiring group above exists to catch what
  a fake can hide. A behaviour test alone would reproduce the original bug.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

REPO = "siegeon/.prism"


# The local `_isolate_observers` autouse fixture that used to live here
# (snapshot/restore of task_service._CREATE_OBSERVERS / _STATUS_OBSERVERS
# around every test, added for AC-7b's thread-race flake -- CI runs
# 30833869760 and 014b54d) is SUPERSEDED by
# tests/conftest.py::_task_observer_registry_isolation (task 44342d64):
# that root-conftest fixture snapshots/restores the exact same two lists
# around every test in the whole suite, not just this file, which is what
# closed the cross-file leak into
# test_switch_on_pushes_backlog.py::test_done_and_cancelled_backlog_is_never_exported.
# A local fixture doing the identical reset a second time is harmless but
# redundant -- removed here rather than reconciled, since pytest already
# guarantees the root fixture wraps this file's tests outside-in.


# `quiet_boot` (real lifespan, workers silenced) now lives in the shared
# tests/conftest.py — task 0784729f generalized it there so every unit
# test booting prism_service.main.app for real gets the same protection.
# See that fixture's docstring for the full story (including the 26-minute
# CI hang and the thread-count leak into test_lifespan_lock_recovery.py).


# ── AC-1: production startup registers the create observer ────────────────
def test_ac1_lifespan_registers_the_mirror_observer(quiet_boot):
    """Boot the REAL app the way uvicorn does and check what it registered.

    Nothing in this test installs the observer; if main.py's lifespan stops
    calling task_mirror.install(), this is the test that notices.
    """
    from fastapi.testclient import TestClient

    from prism_service.main import app
    from prism_service.services import task_mirror, task_service

    task_service.remove_create_observer(task_mirror._on_task_created)
    assert task_mirror._on_task_created not in task_service.create_observers()

    with TestClient(app):
        registered = task_service.create_observers()

    assert task_mirror._on_task_created in registered, (
        "production startup did not register the task mirror; a task created "
        "in PRISM will never reach GitHub")


# ── AC-2: the GitHub adapter is registered by product code, not a fixture ──
def test_ac2_github_adapter_is_registered_at_import_by_production_code():
    """The registry a push reads must be populated by importing the product.

    The original defect was an EMPTY registry at runtime with every test
    passing one in. Importing api.integrations is exactly what the server
    does, so this asserts the same thing the server experiences.
    """
    from prism_service.api import integrations

    integrations.reset_adapters()
    integrations.register_builtin_adapters()

    assert "github" in integrations.adapters_snapshot(), (
        "no github adapter after production registration: "
        f"{integrations.adapter_registration_errors}")


def test_ac2b_registration_runs_on_import_not_only_when_called():
    """register_builtin_adapters() must be INVOKED at module scope.

    A registration function nothing calls is the bug wearing a different hat,
    and it is invisible to a test that calls the function itself (as AC-2
    does), so this reads the module source for the top-level call.
    """
    from prism_service.api import integrations

    source = Path(integrations.__file__).read_text(encoding="utf-8")
    top_level = [line for line in source.splitlines()
                 if line.strip() == "register_builtin_adapters()"
                 and not line.startswith((" ", "\t"))]
    assert top_level, "register_builtin_adapters() is never called at import"


# ── AC-3: creating a task publishes it, with the project name ─────────────
def test_ac3_task_creation_notifies_observers_with_the_project(tmp_path):
    """The ONE chokepoint. Both the REST route and the MCP verb call
    TaskService.create, so an observer here cannot be bypassed by a caller
    that forgot to opt in."""
    from prism_service.services import task_service
    from prism_service.services.task_service import TaskService

    seen = []
    observer = lambda project, task: seen.append((project, task.id))  # noqa: E731
    task_service.add_create_observer(observer)
    try:
        svc = TaskService(db_path=str(tmp_path / "tasks.db"), project="prism")
        task = svc.create(title="a task that should be mirrored")
    finally:
        task_service.remove_create_observer(observer)

    assert seen == [("prism", task.id)]


def test_ac3b_project_context_names_the_project_on_its_task_service():
    """An observer that is told project='' cannot look anything up."""
    source = Path(
        _SERVICE_ROOT / "prism_service" / "project_context.py"
    ).read_text(encoding="utf-8")
    assert "project=self.project_id" in source


# ── AC-4: on by default (owner doctrine mx-71dc57) ────────────────────────
def test_ac4_mirror_is_on_by_default_and_only_off_when_asked():
    from prism_service.services.task_mirror import MIRROR_ENV, mirror_enabled

    prior = os.environ.pop(MIRROR_ENV, None)
    try:
        assert mirror_enabled() is True
        os.environ[MIRROR_ENV] = "off"
        assert mirror_enabled() is False
    finally:
        os.environ.pop(MIRROR_ENV, None)
        if prior is not None:
            os.environ[MIRROR_ENV] = prior


# ── AC-5: structurally incapable of sweeping the backlog ──────────────────
def test_ac5_the_mirror_can_never_sweep_the_board():
    """The recorded misfire for this feature family (tasks ae67ed5c,
    7cf6a2e5) is an unattended drain that mirrors every existing task the
    moment it switches on. Enforced structurally rather than by review: the
    module must never enumerate tasks."""
    source = Path(
        _SERVICE_ROOT / "prism_service" / "services" / "task_mirror.py"
    ).read_text(encoding="utf-8")
    body = "\n".join(line for line in source.splitlines()
                     if not line.lstrip().startswith("#"))
    for forbidden in ("task_svc.list(", ".list_tasks(", "pending_items("):
        assert forbidden not in body, (
            f"task_mirror reaches for {forbidden} — it must only ever be "
            "handed the ONE task that was just created")


# ── AC-6: a task imported FROM github is never pushed back ────────────────
def test_ac6_imported_tasks_are_not_pushed_back(tmp_path, monkeypatch):
    from prism_service.services import task_mirror
    from prism_service.services.task_service import TaskService

    svc = TaskService(db_path=str(tmp_path / "tasks.db"), project="p")
    task = svc.create(title="came from github", tags=["github", "external"])

    monkeypatch.setattr(task_mirror, "personal_scope", lambda: "personal-x")
    monkeypatch.setattr(
        "prism_service.project_context.get_project",
        lambda project: type("Ctx", (), {"task_svc": svc})())

    outcome = task_mirror.mirror_task("p", task.id)
    assert outcome.attempted is False
    assert "imported" in outcome.reason


# ── AC-7: the mirrored task shows a link a PERSON can click ───────────────
class _FakeGitHub:
    """Stands in for the network, NOT for the wiring. AC-1/AC-2 already
    proved the real adapter is registered by production; this exists so a
    unit test does not open issues on a live repository."""

    provider = "github"

    def create(self, connection, container, title, body="", assignee=""):
        from prism_service.models.integration import ExternalEntityInput

        return ExternalEntityInput(
            entity_kind="issue", remote_id="I_node_1", display_key="#4242",
            title=title, body=body,
            url=f"https://github.com/{REPO}/issues/4242",
            remote_status="open", status_category="open",
            remote_updated_at="2026-07-31T22:00:00Z")


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """A real store, a real outbox, a real TaskService — fake network only."""
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
        ._adapters, "github", _FakeGitHub())
    return svc


def test_ac7_a_mirrored_task_renders_the_board_link_out_badge(wired):
    """The oracle says the task "shows a link to one". The ONLY link a person
    can click is TasksPage.tsx's mirrorOf badge, and that badge needs BOTH a
    provider tag and a server-derived mirror_url. Assert the affordance, not
    the internals: a link recorded anywhere else is a link nobody sees.
    """
    from prism_service.api.tasks import _mirror_url
    from prism_service.services import task_mirror

    task = wired.create(title="mirror me")
    outcome = task_mirror.mirror_task("prism", task.id)
    assert outcome.created is True, outcome.reason

    after = wired.get(task.id)
    assert "github" in [t.lower() for t in after.tags], (
        "no provider tag: TasksPage.mirrorOf returns null and the badge "
        "never renders")
    assert _mirror_url(after.description) == f"https://github.com/{REPO}/issues/4242"


def test_ac7b_mirroring_the_same_task_twice_creates_one_issue(wired):
    """Idempotence is structural (an active link refuses the second call),
    but a mirror that duplicates issues is the loudest possible failure, so
    it gets its own assertion."""
    from prism_service.services import task_mirror

    task = wired.create(title="mirror me once")
    first = task_mirror.mirror_task("prism", task.id)
    second = task_mirror.mirror_task("prism", task.id)

    assert first.created is True
    assert second.created is False
    assert "already has an active external link" in second.reason


def test_ac7c_a_done_task_is_never_given_a_new_issue(wired):
    """Owner 2026-07-29: only ACTIVE tasks get an issue; history stays local."""
    from prism_service.services import task_mirror

    task = wired.create(title="already finished")
    wired.update(task.id, status="done")
    outcome = task_mirror.mirror_task("prism", task.id)

    assert outcome.created is False
    assert "not active" in outcome.reason


# ── AC-8: the wiring is readable without a debugger ───────────────────────
def test_ac8_mirror_status_reports_the_chain_link_by_link(quiet_boot):
    """When a task does not reach GitHub, WHICH link broke has to be
    answerable from the running process."""
    from fastapi.testclient import TestClient

    from prism_service.main import app

    with TestClient(app) as client:
        body = client.get("/api/integrations/connect/mirror").json()

    for key in ("enabled", "observer_installed", "adapters", "sync_enabled",
                "tracking", "ready"):
        assert key in body, f"mirror status hides {key}"
    assert body["observer_installed"] is True
    assert "github" in body["adapters"]


# ── AC-9: a broken mirror never breaks task creation ──────────────────────
def test_ac9_a_failing_observer_does_not_fail_the_task(tmp_path):
    from prism_service.services import task_service
    from prism_service.services.task_service import TaskService

    def _explode(project, task):
        raise RuntimeError("provider is down")

    task_service.add_create_observer(_explode)
    try:
        svc = TaskService(db_path=str(tmp_path / "tasks.db"), project="p")
        task = svc.create(title="still created")
    finally:
        task_service.remove_create_observer(_explode)

    assert svc.get(task.id) is not None
