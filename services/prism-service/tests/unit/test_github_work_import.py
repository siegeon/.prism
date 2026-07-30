"""RED scaffold — GitHub Issues/PRs import adapter (task 1f181b49).

Drives the not-yet-built GitHubWorkAdapter + GitHubAppCredentials through the
provider-neutral integration core against official-shape contract fixtures (no
network). Pins: node_id identity + idempotency, PRs are not imported as issues,
installation/repository scope enforcement, validated single-known-key PR
linkage, and no installation secret ever persisted.

Prism modules import INSIDE the test bodies so the file collects and fails at
runtime (red = rc 1) before github_work/github_app exist.
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

_FIXTURES = _HERE.parent.parent / "fixtures" / "github"
WS_A = "workspace-a"
WS_B = "workspace-b"


def _fixture(name):
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


class _FixtureClient:
    """Stands in for the GitHub REST client — returns canned payloads and
    records the token it was handed (to prove the secret flows for auth but is
    never persisted)."""

    def __init__(self):
        self.tokens_seen = []

    def issues(self, connection, container, token):
        self.tokens_seen.append(token)
        return _fixture("issues.json")

    def pulls(self, connection, container, token):
        self.tokens_seen.append(token)
        return _fixture("pulls.json")


def _store(tmp_path):
    from prism_service.services.integration_store import IntegrationStore

    return IntegrationStore(str(tmp_path / "integrations.db"))


def _task_svc(tmp_path):
    from prism_service.services.task_service import TaskService

    return TaskService(str(tmp_path / "tasks.db"))


def _synced(tmp_path, adapter):
    """Wire the adapter through the real sync core and pull once. Returns
    (store, task_svc, connection, container)."""
    from prism_service.services.work_item_sync import WorkItemSyncService

    store = _store(tmp_path)
    tasks = _task_svc(tmp_path)
    conn = store.ensure_connection(WS_A, "github", "install-1")
    cont = store.ensure_container(WS_A, conn.id, "repository", "R_repo1")
    svc = WorkItemSyncService(store, intake=tasks)
    svc.register(adapter)
    svc.pull_container(WS_A, conn, cont)
    return store, tasks, conn, cont


def _adapter(client, credentials=None, installation=None):
    from prism_service.services.github_work import GitHubWorkAdapter

    return GitHubWorkAdapter(client, credentials=credentials, installation=installation)


# ── AC-1: issue import is exact-node_id idempotent ─────────────────────

def test_issue_import_is_idempotent_by_node_id(tmp_path):
    adapter = _adapter(_FixtureClient())
    store, tasks, conn, cont = _synced(tmp_path, adapter)

    issues = [e for e in store.list_entities(WS_A) if e.entity_kind == "issue"]
    assert {e.remote_id for e in issues} == {"I_kwDOAAA001", "I_kwDOAAA002"}

    # replay — must not duplicate
    from prism_service.services.work_item_sync import WorkItemSyncService
    svc = WorkItemSyncService(store, intake=tasks)
    svc.register(adapter)
    svc.pull_container(WS_A, conn, cont)

    issues2 = [e for e in store.list_entities(WS_A) if e.entity_kind == "issue"]
    assert len(issues2) == 2
    # one pending intake task per imported entity, none advanced into workflow
    imported = [t for t in tasks.list() if "external" in t.tags]
    assert all(t.status == "pending" and t.workflow_step == "" for t in imported)


# ── AC-2: pull requests are NOT imported as issues ─────────────────────

def test_pull_requests_are_not_imported_as_issues(tmp_path):
    adapter = _adapter(_FixtureClient())
    store, _tasks, _conn, _cont = _synced(tmp_path, adapter)

    issues = [e for e in store.list_entities(WS_A) if e.entity_kind == "issue"]
    prs = [e for e in store.list_entities(WS_A) if e.entity_kind == "pull_request"]
    # PR_kwDOAAA009 appears in the issues endpoint (with a pull_request key) —
    # it must NOT become an issue entity.
    assert "PR_kwDOAAA009" not in {e.remote_id for e in issues}
    assert {e.remote_id for e in prs} == {"PR_kwDOAAA009", "PR_kwDOAAA010"}
    # merged PR normalizes to done; open PR stays open.
    by_id = {e.remote_id: e for e in prs}
    assert by_id["PR_kwDOAAA010"].status_category == "done"
    assert by_id["PR_kwDOAAA009"].status_category == "open"


# ── AC-6: pull requests never become their own intake task (task 0665c829) ─
#
# github_rest.py/github_work.py already correctly TAG pull requests with
# entity_kind="pull_request" (AC-2 above). This pins the layer ABOVE that:
# the orchestrator must not turn a pull_request-kind entity into a visible
# PRISM task, even though pull_page() deliberately returns PRs (so a future
# github_work.link_pull_request() call has something to match against).

def test_pull_requests_never_become_intake_tasks(tmp_path):
    adapter = _adapter(_FixtureClient())
    store, tasks, conn, cont = _synced(tmp_path, adapter)

    imported = [t for t in tasks.list() if "external" in t.tags]
    # issues.json has 2 real issues (one open, one closed) plus a THIRD row
    # shaped like a PR (carries a "pull_request" key) that must never surface
    # as its own issue OR task. pulls.json adds 2 more real PRs. None of the
    # 2 PR titles may appear as a task; exactly the 2 issues do.
    assert len(imported) == 2
    titles = {t.title for t in imported}
    assert "Fix the startup crash" not in titles
    assert "Refactor the config loader" not in titles
    assert titles == {"Crash on startup when config missing", "Add dark mode"}


# ── AC-3: installation / repository scope is enforced ──────────────────

def test_installation_and_repository_scope_is_enforced(tmp_path):
    from prism_service.services.github_app import (
        GitHubInstallation,
        InstallationScopeError,
        assert_repo_in_scope,
    )

    inst = GitHubInstallation(
        workspace_id=WS_A, installation_id="install-1",
        account_login="acme", repository_node_ids=("R_repo1",))

    assert_repo_in_scope(inst, "R_repo1")  # in scope: no raise
    with pytest.raises(InstallationScopeError):
        assert_repo_in_scope(inst, "R_other")
    # an installation bound to workspace A cannot be used under workspace B
    with pytest.raises(InstallationScopeError):
        assert_repo_in_scope(inst, "R_repo1", workspace_id=WS_B)


# ── AC-4: PR linkage is validated (single known key only) ──────────────

def test_pr_links_to_a_single_known_task_only(tmp_path):
    from prism_service.services.github_work import link_pull_request
    from prism_service.models.integration import ExternalEntityInput

    store = _store(tmp_path)
    tasks = _task_svc(tmp_path)
    conn = store.ensure_connection(WS_A, "github", "install-1")
    cont = store.ensure_container(WS_A, conn.id, "repository", "R_repo1")

    real = tasks.create(title="the real local task")
    key = real.id[:8]

    def _pr(remote_id):
        entity, _ = store.upsert_entity(
            WS_A, conn.id, cont.id,
            ExternalEntityInput(entity_kind="pull_request", remote_id=remote_id))
        return entity

    known = _pr("PR_known")
    linked = link_pull_request(store, WS_A, tasks, known, text=f"delivers [task:{key}]")
    assert linked is not None and linked.task_id == real.id

    # unknown key → no link
    unknown = _pr("PR_unknown")
    assert link_pull_request(store, WS_A, tasks, unknown, text="[task:deadbeef]") is None
    # no key at all → no link
    none = _pr("PR_none")
    assert link_pull_request(store, WS_A, tasks, none, text="just a refactor") is None
    # ambiguous (two keys) → no link
    ambiguous = _pr("PR_ambi")
    other = tasks.create(title="other")
    assert link_pull_request(
        store, WS_A, tasks, ambiguous,
        text=f"[task:{key}] and [task:{other.id[:8]}]") is None


# ── AC-5: installation secrets never persisted ─────────────────────────

def test_installation_secret_never_persisted(tmp_path):
    from prism_service.services.github_app import GitHubAppCredentials, GitHubInstallation

    secret = "ghs_INSTALLATION_SECRET_TOKEN_42"
    creds = GitHubAppCredentials(token_resolver=lambda inst: secret)
    inst = GitHubInstallation(
        workspace_id=WS_A, installation_id="install-1",
        account_login="acme", repository_node_ids=("R_repo1",))
    client = _FixtureClient()
    adapter = _adapter(client, credentials=creds, installation=inst)

    store, _tasks, _conn, _cont = _synced(tmp_path, adapter)

    # The token WAS used for auth...
    assert secret in client.tokens_seen
    # ...but appears nowhere in the persisted store or any serialized entity.
    raw = sqlite3.connect(str(tmp_path / "integrations.db"))
    try:
        dump = "\n".join(raw.iterdump())
    finally:
        raw.close()
    assert secret not in dump
    for entity in store.list_entities(WS_A):
        assert secret not in json.dumps(entity.__dict__, default=str)
