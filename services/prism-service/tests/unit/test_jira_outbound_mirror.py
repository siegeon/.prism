"""A PRISM task becomes a Jira ticket (task 88a7da0b).

Outbound Jira mirroring: FR-1..FR-11 in the task's plan_doc. Same discipline
as test_task_mirror_production_wiring.py -- only the HTTP transport is faked
(NFR-5); the JiraRestClient, JiraWorkAdapter, task_mirror observers and the
adapter registry stay the REAL production objects, so a green run here means
production, not a mock, satisfies the AC.

jira_rest.py does not exist yet, and JiraWorkAdapter/task_mirror/
work_item_sync do not yet carry the write verbs this file exercises, so
every test below fails on collection (ImportError) or on a concrete
assertion today. That is RED with a trace, by design (task 88a7da0b,
write_failing_tests step).
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

SCOPE = "personal-local-user"
SITE = "https://bespokelabsllc.atlassian.net"
SECRET = "atlassian-api-token-SECRET-VALUE"


@pytest.fixture(autouse=True)
def _isolate_observers():
    """Same discipline as test_task_mirror_production_wiring.py's fixture of
    the same name: test_ac5 below calls ``task_mirror.install()`` (to
    exercise a REAL status-change observer), which appends to the
    MODULE-LEVEL ``task_service._CREATE_OBSERVERS``/``_STATUS_OBSERVERS``
    lists -- nothing ever removes them. Left unguarded, that leaked observer
    fires a background thread on every later test's ``svc.create()``/
    ``svc.update()`` call, and can race into test_ac7b/test_ac7c's
    monkeypatched ``mirror_task``/``close_task``, appending an extra call
    the assertion never expects. Snapshot-and-restore around every test."""
    from prism_service.services import task_service

    created = list(task_service._CREATE_OBSERVERS)
    status = list(task_service._STATUS_OBSERVERS)
    try:
        yield
    finally:
        task_service._CREATE_OBSERVERS[:] = created
        task_service._STATUS_OBSERVERS[:] = status


def _join_mirror_threads() -> None:
    """Wait for the observers' off-thread runs (task_mirror.py:262-274,
    292-305) to finish, exactly as test_status_syncs_both_ways._settle()
    does -- a direct call to mirror_task/close_task is the hand-run trigger
    this feature family exists to remove."""
    for t in threading.enumerate():
        if t.name.startswith("task-mirror-"):
            t.join(timeout=10)


def _credential():
    from prism_service.services.jira_auth import JiraCredential

    return JiraCredential(kind="api_token", secret=SECRET, email="owner@example.com",
                          site_url=SITE, cloud_id="cloud-1")


class _RecordingTransport:
    """Fakes ONLY the HTTP call inside JiraRestClient (NFR-5, mirroring
    _FakeGitHub's discipline at test_task_mirror_production_wiring.py:215).
    ``calls`` is (method, url, body) so AC-1/AC-3/AC-8/AC-12 can count
    exactly what reached the wire."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []
        self.create_result: dict = {"id": "10001", "key": "PRIS-1", "self": ""}
        self.transitions_result: dict = {"transitions": []}
        self.raise_get = False
        self.raise_post = False

    def get(self, url, credential):
        self.calls.append(("GET", url, None))
        if self.raise_get:
            raise RuntimeError(f"jira GET failed, token was {credential.secret}")
        return self.transitions_result

    def post(self, url, credential, body):
        self.calls.append(("POST", url, body))
        if self.raise_post:
            raise RuntimeError(f"jira POST failed, token was {credential.secret}")
        if url.endswith("/transitions") and "transition" in (body or {}):
            return {}
        return self.create_result


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """Real IntegrationStore/IntegrationOutbox/TaskService, real
    JiraWorkAdapter over a real JiraRestClient -- only the transport (the
    network edge) is fake. Registers under _adapters["jira"] exactly as
    api/integrations.py's register_builtin_adapters will in production."""
    from prism_service.services import task_mirror
    from prism_service.services.integration_outbox import IntegrationOutbox
    from prism_service.services.integration_store import IntegrationStore
    from prism_service.services.jira_rest import JiraRestClient
    from prism_service.services.jira_work import JiraWorkAdapter
    from prism_service.services.task_service import TaskService

    store = IntegrationStore(str(tmp_path / "integrations.db"))
    outbox = IntegrationOutbox(str(tmp_path / "outbox.db"))
    conn = store.ensure_connection(SCOPE, "jira", "cloud-1", display_name="cloud-1")
    store.ensure_container(SCOPE, conn.id, "jira_project", "PRIS",
                           display_key="PRIS", display_name="PRIS", url=f"{SITE}/browse/PRIS")
    svc = TaskService(db_path=str(tmp_path / "tasks.db"), project="prism")

    credential = _credential()
    transport = _RecordingTransport()
    client = JiraRestClient(transport=transport.get, write_transport=transport.post)
    adapter = JiraWorkAdapter(
        client=client, access_token_provider=lambda connection: credential,
        site_url_provider=lambda connection: SITE, write_client=client)

    monkeypatch.setattr(task_mirror, "personal_scope", lambda: SCOPE)
    monkeypatch.setattr(
        "prism_service.project_context.get_project",
        lambda project: type("Ctx", (), {"task_svc": svc})())
    monkeypatch.setattr(
        "prism_service.services.integration_store.get_integration_store", lambda: store)
    monkeypatch.setattr(
        "prism_service.services.integration_outbox.get_outbox", lambda: outbox)
    monkeypatch.setattr(
        "prism_service.services.sync_prefs.get_sync_preferences",
        lambda: type("P", (), {"enabled": staticmethod(lambda w, p: True)})())
    monkeypatch.setitem(
        __import__("prism_service.api.integrations", fromlist=["_adapters"])
        ._adapters, "jira", adapter)
    return svc, store, outbox, transport


# ── AC-1 / AC-2: creating a task creates a Jira issue carrying its content,
#    visible on the task through the pre-existing board link-out badge ─────
def test_ac1_creating_a_task_posts_the_real_issue_create_call(wired):
    from prism_service.services import task_mirror

    svc, *_ , transport = wired
    task = svc.create(title="ship the jira mirror", description="carries this body")
    outcome = task_mirror.mirror_task("prism", task.id, provider="jira")

    assert outcome.created is True, outcome.reason
    posts = [c for c in transport.calls if c[0] == "POST" and c[1].endswith("/issue")]
    assert len(posts) == 1, f"expected exactly one issue-create POST, got {transport.calls!r}"
    body = posts[0][2]
    assert body["fields"]["summary"] == "ship the jira mirror"
    adf = body["fields"]["description"]
    assert adf["type"] == "doc"
    assert any("carries this body" in str(node) for node in adf["content"]), (
        f"task description never reached the ADF body: {adf!r}")


def test_ac2_the_mirrored_task_renders_the_board_link_out_badge(wired):
    from prism_service.api.tasks import _mirror_url
    from prism_service.services import task_mirror

    svc, *_ = wired
    task = svc.create(title="visible on the board")
    outcome = task_mirror.mirror_task("prism", task.id, provider="jira")
    assert outcome.created is True, outcome.reason

    after = svc.get(task.id)
    assert "jira" in [t.lower() for t in after.tags], (
        "no provider tag: TasksPage.mirrorOf returns null and the badge never renders")
    assert _mirror_url(after.description) == f"{SITE}/browse/PRIS-1"


# ── AC-3: create-leg idempotence is PROVIDER-SCOPED (FR-9) ────────────────
def test_ac3_mirroring_the_same_task_twice_creates_one_jira_issue(wired):
    from prism_service.services import task_mirror

    svc, *_ , transport = wired
    task = svc.create(title="mirror me once")
    first = task_mirror.mirror_task("prism", task.id, provider="jira")
    second = task_mirror.mirror_task("prism", task.id, provider="jira")

    assert first.created is True
    assert second.created is False
    assert "already has an active external link" in second.reason
    posts = [c for c in transport.calls if c[0] == "POST" and c[1].endswith("/issue")]
    assert len(posts) == 1, "a repeated mirror_task call minted a second issue"


def test_ac3b_a_github_linked_task_is_still_eligible_for_its_jira_issue(
    tmp_path, monkeypatch,
):
    """THE key planning discovery (mx-b66807): the pre-FR-9 guard refuses on
    ANY active link, so a task GitHub already linked would refuse Jira
    forever. Provider-scoping (FR-9) is what makes the owner's live round
    trip possible when both connectors are on at once."""
    from prism_service.models.integration import ExternalEntityInput
    from prism_service.services.integration_outbox import IntegrationOutbox
    from prism_service.services.integration_store import IntegrationStore
    from prism_service.services.work_item_sync import push_task_creation

    store = IntegrationStore(str(tmp_path / "integrations.db"))
    outbox = IntegrationOutbox(str(tmp_path / "outbox.db"))

    gh_conn = store.ensure_connection(SCOPE, "github", "siegeon")
    store.ensure_container(SCOPE, gh_conn.id, "repository", "siegeon/.prism",
                           display_key="siegeon/.prism")
    jira_conn = store.ensure_connection(SCOPE, "jira", "cloud-1")
    store.ensure_container(SCOPE, jira_conn.id, "jira_project", "PRIS", display_key="PRIS")

    class _GitHub:
        provider = "github"

        def create(self, connection, container, title, body="", assignee=""):
            return ExternalEntityInput(entity_kind="issue", remote_id="I_1",
                                       display_key="#1", title=title,
                                       url="https://github.com/siegeon/.prism/issues/1",
                                       status_category="open")

    class _Jira:
        provider = "jira"

        def create(self, connection, container, title, body="", assignee=""):
            return ExternalEntityInput(entity_kind="jira_issue", remote_id="10001",
                                       display_key="PRIS-1", title=title,
                                       url=f"{SITE}/browse/PRIS-1", status_category="open")

    registry = {"github": _GitHub(), "jira": _Jira()}
    always_on = lambda workspace, provider: True  # noqa: E731

    first = push_task_creation(store, outbox, registry, always_on, SCOPE,
                               "task-1", "pending", title="cross-provider task",
                               provider="github")
    second = push_task_creation(store, outbox, registry, always_on, SCOPE,
                                "task-1", "pending", title="cross-provider task",
                                provider="jira")

    assert first.created is True, first.reason
    assert second.created is True, (
        f"FR-9: a github-linked task must still be eligible for a jira issue, "
        f"got refused with {second.reason!r}")


# ── AC-4: an imported/external task never mints a Jira issue ──────────────
def test_ac4_imported_tasks_are_never_pushed_to_jira(wired, monkeypatch):
    from prism_service.services import task_mirror

    svc, *_ , transport = wired
    task = svc.create(title="came from somewhere else", tags=["jira", "external"])

    outcome = task_mirror.mirror_task("prism", task.id, provider="jira")
    assert outcome.attempted is False
    assert "imported" in outcome.reason
    assert transport.calls == [], "an imported task reached the jira transport"


# ── AC-5 / AC-6: the status leg transitions to a done-category status, or
#    refuses rather than guessing a workflow path ──────────────────────────
def test_ac5_marking_a_task_done_transitions_the_jira_issue(wired):
    from prism_service.services import task_mirror

    svc, *_ , transport = wired
    transport.transitions_result = {"transitions": [
        {"id": "11", "to": {"statusCategory": {"key": "indeterminate"}}},
        {"id": "31", "to": {"statusCategory": {"key": "done"}}},
    ]}

    task = svc.create(title="finish me")
    assert task_mirror.mirror_task("prism", task.id, provider="jira").created is True

    task_mirror.install()
    svc.update(task.id, status="done")
    _join_mirror_threads()

    gets = [c for c in transport.calls if c[0] == "GET" and c[1].endswith("/transitions")]
    posts = [c for c in transport.calls if c[0] == "POST" and c[1].endswith("/transitions")]
    assert gets, "the closure leg never fetched the issue's transitions"
    assert posts and posts[0][2] == {"transition": {"id": "31"}}, (
        f"expected the done-category transition (id 31) posted, got {posts!r}")


def test_ac6_no_done_category_transition_refuses_and_posts_nothing(wired):
    from prism_service.services import task_mirror

    svc, *_ , transport = wired
    transport.transitions_result = {"transitions": [
        {"id": "11", "to": {"statusCategory": {"key": "indeterminate"}}},
    ]}

    task = svc.create(title="stuck workflow")
    assert task_mirror.mirror_task("prism", task.id, provider="jira").created is True

    outcome = task_mirror.close_task("prism", task.id, provider="jira")
    assert outcome.created is False
    assert outcome.reason, "a refusal must carry a reason, never a silent no-op"
    posts = [c for c in transport.calls if c[0] == "POST" and c[1].endswith("/transitions")]
    assert posts == [], "no done-category transition existed; nothing may be posted"


# ── AC-7 (FR-8): both observers dispatch by the NAMED tuple, never by
#    iterating whatever providers happen to be registered (mx-804f8e) ──────
def test_ac7_mirror_providers_is_the_named_github_jira_tuple():
    from prism_service.services import task_mirror

    assert task_mirror.MIRROR_PROVIDERS == ("github", "jira")


def test_ac7b_create_observer_dispatches_only_named_providers(monkeypatch):
    from prism_service.api import integrations
    from prism_service.services import task_mirror

    calls: list[str] = []

    def _fake_mirror(project, task_id, provider="github", dry_run=False):
        calls.append(provider)
        return task_mirror.MirrorOutcome(task_id=task_id)

    monkeypatch.setattr(task_mirror, "mirror_task", _fake_mirror)
    monkeypatch.setattr(task_mirror, "mirror_enabled", lambda: True)
    monkeypatch.setitem(integrations._adapters, "gitlab", object())

    task = type("T", (), {"id": "t-created-1"})()
    task_mirror._on_task_created("prism", task)
    _join_mirror_threads()

    assert sorted(calls) == ["github", "jira"], (
        "the create observer must dispatch by MIRROR_PROVIDERS, not by "
        f"whatever is registered in _adapters; got {calls!r}")


def test_ac7c_status_observer_dispatches_only_named_providers(monkeypatch):
    from prism_service.api import integrations
    from prism_service.services import task_mirror

    calls: list[str] = []

    def _fake_close(project, task_id, provider="github", dry_run=False):
        calls.append(provider)
        return task_mirror.MirrorOutcome(task_id=task_id)

    monkeypatch.setattr(task_mirror, "close_task", _fake_close)
    monkeypatch.setattr(task_mirror, "mirror_enabled", lambda: True)
    monkeypatch.setitem(integrations._adapters, "gitlab", object())

    task = type("T", (), {"id": "t-done-1", "status": "done"})()
    task_mirror._on_task_status_changed("prism", task, old_status="in_progress")
    _join_mirror_threads()

    assert sorted(calls) == ["github", "jira"], (
        f"the status observer must dispatch by MIRROR_PROVIDERS; got {calls!r}")


def test_ac7d_one_providers_observer_failure_never_blocks_the_other(monkeypatch):
    """NFR-6: each provider's dispatch is its own try/except."""
    from prism_service.services import task_mirror

    calls: list[str] = []

    def _fake_mirror(project, task_id, provider="github", dry_run=False):
        if provider == "github":
            raise RuntimeError("github is down")
        calls.append(provider)
        return task_mirror.MirrorOutcome(task_id=task_id)

    monkeypatch.setattr(task_mirror, "mirror_task", _fake_mirror)
    monkeypatch.setattr(task_mirror, "mirror_enabled", lambda: True)

    task = type("T", (), {"id": "t-created-2"})()
    task_mirror._on_task_created("prism", task)
    _join_mirror_threads()

    assert calls == ["jira"], (
        "a github failure must not prevent the jira leg from running")


# ── AC-8: the sync switch off stops both outbound legs ────────────────────
def test_ac8_sync_switch_off_stops_the_create_leg(wired, monkeypatch):
    from prism_service.services import task_mirror

    svc, store, outbox, transport = wired
    monkeypatch.setattr(
        "prism_service.services.sync_prefs.get_sync_preferences",
        lambda: type("P", (), {"enabled": staticmethod(lambda w, p: False)})())

    task = svc.create(title="switch is off")
    outcome = task_mirror.mirror_task("prism", task.id, provider="jira")

    assert outcome.created is False
    assert outcome.reason == "syncing with jira is turned off"
    assert transport.calls == []


def test_ac8b_sync_switch_off_stops_the_closure_leg(wired, monkeypatch):
    from prism_service.services import task_mirror

    svc, store, outbox, transport = wired
    task = svc.create(title="linked then switched off")
    assert task_mirror.mirror_task("prism", task.id, provider="jira").created is True
    transport.calls.clear()

    monkeypatch.setattr(
        "prism_service.services.sync_prefs.get_sync_preferences",
        lambda: type("P", (), {"enabled": staticmethod(lambda w, p: False)})())
    outcome = task_mirror.close_task("prism", task.id, provider="jira")

    assert outcome.created is False
    assert outcome.reason == "syncing with jira is turned off"
    assert transport.calls == []


# ── AC-11: the api_token secret never reaches an error, a repr, or a
#    MirrorOutcome.reason on the way back from a failed call ──────────────
def test_ac11_a_failed_create_never_leaks_the_token(wired):
    from prism_service.services import task_mirror

    svc, *_ , transport = wired
    transport.raise_post = True
    task = svc.create(title="will fail to create")

    outcome = task_mirror.mirror_task("prism", task.id, provider="jira")
    assert SECRET not in outcome.reason, "the api token leaked into MirrorOutcome.reason"


def test_ac11b_a_failed_transitions_fetch_never_leaks_the_token(wired):
    from prism_service.services import task_mirror
    from prism_service.services.work_item_sync import AdapterError

    svc, *_ , transport = wired
    task = svc.create(title="will fail to close")
    assert task_mirror.mirror_task("prism", task.id, provider="jira").created is True

    transport.raise_get = True
    try:
        outcome = task_mirror.close_task("prism", task.id, provider="jira")
    except AdapterError as exc:
        assert SECRET not in str(exc), "the api token leaked into a raised AdapterError"
    else:
        assert SECRET not in outcome.reason, (
            "the api token leaked into MirrorOutcome.reason")


# ── AC-12: nothing sweeps the existing backlog as a side effect ───────────
def test_ac12_mirroring_one_task_never_touches_the_others(wired):
    from prism_service.services import task_mirror

    svc, store, outbox, transport = wired
    target = svc.create(title="the one being mirrored")
    others = [svc.create(title=f"unrelated task {i}") for i in range(3)]

    outcome = task_mirror.mirror_task("prism", target.id, provider="jira")
    assert outcome.created is True

    posts = [c for c in transport.calls if c[0] == "POST" and c[1].endswith("/issue")]
    assert len(posts) == 1, (
        f"mirroring one task must make exactly one create call, got {len(posts)}")
    for other in others:
        links = [l for l in store.list_links(SCOPE, task_id=other.id) if l.state == "active"]
        assert links == [], f"an unrelated task {other.id} was linked as a side effect"


# ── Regression (live PRIS-8, 2026-08-11): closure resolves the PROVIDER'S
# own link. A task mirrored to BOTH github and jira closed its github issue
# while the jira leg refused with "linked entity is not a jira issue":
# push_task_closure took active_links[0] regardless of provider. It must
# select the link whose connection matches the requested provider.
def test_closure_picks_the_providers_own_link_among_many(wired):
    from prism_service.api.integrations import _adapters
    from prism_service.models.integration import ExternalEntityInput
    from prism_service.services import task_mirror
    from prism_service.services.work_item_sync import push_task_closure

    svc, store, outbox, transport = wired
    task = svc.create(title="mirrored to two providers")

    # A github link FIRST, so a provider-blind active_links[0] picks it.
    gconn = store.ensure_connection(SCOPE, "github", "install-x")
    gcont = store.ensure_container(SCOPE, gconn.id, "repository", "o/r",
                                   display_key="o/r")
    gentity, _ = store.upsert_entity(
        SCOPE, gconn.id, gcont.id,
        ExternalEntityInput(entity_kind="issue", remote_id="I_gh1",
                            display_key="#1"))
    glink = store.claim_import_link(SCOPE, gentity.id, task.id)
    store.activate_link(SCOPE, glink.id)

    assert task_mirror.mirror_task(
        "prism", task.id, provider="jira").created is True

    transport.transitions_result = {"transitions": [
        {"id": "31", "name": "Done",
         "to": {"statusCategory": {"key": "done"}}}]}
    result = push_task_closure(
        store, outbox, _adapters, lambda ws, p: True,
        SCOPE, task.id, True, provider="jira")
    assert result.closed is True, (
        f"jira closure must resolve the jira link even when a github link "
        f"exists and sorts first; refused with: {result.reason}")
    trans = [c for c in transport.calls if "transitions" in c[1]]
    assert trans, "the jira transitions endpoint was never called"
