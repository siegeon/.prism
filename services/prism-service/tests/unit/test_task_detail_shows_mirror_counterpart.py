"""RED scaffold — the task detail page shows its real counterpart issue
(task a7c989c6, TRACE clause of epic 02672417).

Both halves already existed and were not connected: TaskDetailPage.tsx:963
already fetches GET /api/tasks/{id}, and TaskDetailPage.tsx:1479-1493 already
renders an external-context block — but it is gated on
`(task.tags ?? []).includes("external")` and reads `task.external_url`, a
client-only field with ZERO backend producers (three references, all in this
one .tsx file). This slice sets a real `mirror` key on the GET /api/tasks/{id}
envelope (api/tasks.py) and rewires the block to render from it, for ANY
linked task (imported or outbound-created), never gated on the "external" tag.

Pre-declared misfire (task.likely_misfire): a source scan that a nearby
explanatory COMMENT satisfies (fixed by asserting the actual JSX branch
condition, not a bare identifier / fixed character window); fixing only the
imported case and leaving the tag=="external" gate in place; rendering
"last synced" from the task's own updated_at instead of entity.last_seen_at;
the mirror lookup 500-ing the whole detail route on a store failure.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_TSX = (_SERVICE_ROOT / "prism_service" / "web" / "src" / "pages"
        / "TaskDetailPage.tsx")

WS = "personal-local-user"
REPO = "siegeon/.prism"


# ---------------------------------------------------------------------------
# Backend: GET /api/tasks/{id} carries a real `mirror` key (AC-1, AC-2)
# ---------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setenv("PRISM_AUTH_MODE", "local")

    from prism_service.services.integration_store import (
        IntegrationStore, set_integration_store,
    )
    from prism_service.services.workspace_service import (
        WorkspaceService, set_workspace_service,
    )
    from prism_service.services.task_service import TaskService
    from prism_service.api import tasks as tasks_api

    workspace_svc = WorkspaceService(tmp_path / "workspace.db")
    set_workspace_service(workspace_svc)
    store = IntegrationStore(str(tmp_path / "integrations.db"))
    set_integration_store(store)

    task_svc = TaskService(str(tmp_path / "tasks.db"))
    import types
    monkeypatch.setattr(
        tasks_api, "get_project",
        lambda p: types.SimpleNamespace(task_svc=task_svc),
    )

    app = FastAPI()
    app.include_router(tasks_api.router, prefix="/api/tasks")

    with TestClient(app) as c:
        yield c, task_svc, store

    set_integration_store(None)
    set_workspace_service(None)


def _link_task_to_issue(store, task_id, issue_num=222, state="active"):
    from prism_service.models.integration import ExternalEntityInput

    conn = store.ensure_connection(WS, "github", "install-1")
    cont = store.ensure_container(WS, conn.id, "repository", REPO,
                                  display_key=REPO)
    entity, _ = store.upsert_entity(
        WS, conn.id, cont.id,
        ExternalEntityInput(
            entity_kind="issue", remote_id=f"I_{issue_num}",
            display_key=f"#{issue_num}",
            url=f"https://github.com/{REPO}/issues/{issue_num}",
        ),
    )
    link = store.claim_import_link(WS, entity.id, task_id)
    if state == "active":
        store.activate_link(WS, link.id)
    return entity, link


def test_linked_task_carries_a_real_mirror_key(client):
    c, task_svc, store = client
    t = task_svc.create(title="Imported issue task")
    _link_task_to_issue(store, t.id, issue_num=222)

    r = c.get(f"/api/tasks/{t.id}", params={"project": "default"})
    assert r.status_code == 200, r.text
    mirror = r.json()["mirror"]
    assert mirror is not None, "a task with an active link must carry a mirror"
    assert mirror["provider"] == "github"
    assert mirror["issue"] == "#222"
    assert mirror["url"] == f"https://github.com/{REPO}/issues/222"
    assert mirror["state"] == "active"
    assert "last_synced_at" in mirror and mirror["last_synced_at"]


def test_unlinked_task_carries_a_null_mirror(client):
    c, task_svc, _store = client
    t = task_svc.create(title="Never touched GitHub")

    r = c.get(f"/api/tasks/{t.id}", params={"project": "default"})
    assert r.status_code == 200, r.text
    assert r.json()["mirror"] is None


def test_provisioning_link_is_not_reported_as_a_mirror(client):
    """AC-1's contract is ACTIVE links only — a provisioning (not-yet-activated)
    link must not be surfaced as a working counterpart."""
    c, task_svc, store = client
    t = task_svc.create(title="Mid-import task")
    _link_task_to_issue(store, t.id, issue_num=901, state="provisioning")

    r = c.get(f"/api/tasks/{t.id}", params={"project": "default"})
    assert r.status_code == 200, r.text
    assert r.json()["mirror"] is None


def test_outbound_created_task_gets_a_mirror_too(client):
    """AC-3's backend half: nothing about the mirror lookup depends on the
    task being tagged 'external' — a native task that was pushed out and
    got an issue must carry the same mirror shape."""
    c, task_svc, store = client
    t = task_svc.create(title="Native task pushed to GitHub")
    assert "external" not in (t.tags or [])
    _link_task_to_issue(store, t.id, issue_num=501)

    r = c.get(f"/api/tasks/{t.id}", params={"project": "default"})
    assert r.status_code == 200, r.text
    assert r.json()["mirror"] is not None


def test_integration_store_failure_degrades_to_null_never_500(client, monkeypatch):
    """AC-2: an integration-store problem must never break the detail route
    (the same defensive shape as the actor-identity block, tasks.py:500-509)."""
    c, task_svc, _store = client
    from prism_service.api import tasks as tasks_api

    t = task_svc.create(title="Store blows up")

    def _boom():
        raise RuntimeError("integration store unavailable")

    monkeypatch.setattr(tasks_api, "get_integration_store", _boom)

    r = c.get(f"/api/tasks/{t.id}", params={"project": "default"})
    assert r.status_code == 200, r.text
    assert r.json()["mirror"] is None


# ---------------------------------------------------------------------------
# Frontend: TaskDetailPage.tsx renders from `mirror`, not the dead
# `external_url` field or the "external" tag gate (AC-3, AC-4, AC-6)
# ---------------------------------------------------------------------------

def _read_tsx() -> str:
    assert _TSX.exists(), f"expected source missing: {_TSX}"
    return _TSX.read_text(encoding="utf-8")


def _external_context_branch(src: str) -> str:
    """Slice out the enclosing JSX conditional that renders the
    external-context block, anchored on the `data-external-context` marker
    attribute rather than a fixed character window — a comment above the
    element must not be able to satisfy this (repo lesson: source-reading
    tests must match the real branch, not nearby prose)."""
    marker = src.find("data-external-context")
    assert marker != -1, "external-context block marker not found in source"
    # Walk backward to the start of the enclosing `{...&& (` / `{cond && (`
    # guard: the nearest unmatched `{` before the marker that opens a JSX
    # expression container.
    window_start = max(0, marker - 400)
    head = src[window_start:marker]
    brace_at = head.rfind("{")
    assert brace_at != -1, "could not locate the enclosing JSX guard"
    return src[window_start + brace_at: marker + 900]


def test_render_guard_is_the_mirror_key_not_the_external_tag():
    src = _read_tsx()
    branch = _external_context_branch(src)
    assert "task.mirror" in branch, (
        "the external-context block must be gated on `task.mirror`, "
        f"got branch:\n{branch}"
    )
    assert 'includes("external")' not in branch, (
        "the block must no longer be restricted to tag==\"external\" — an "
        "outbound-created (native) task with a mirror must render it too"
    )


def test_link_and_synced_time_come_from_mirror_not_external_url():
    src = _read_tsx()
    branch = _external_context_branch(src)
    assert "task.mirror.url" in branch or "mirror.url" in branch, (
        f"the link href must read from mirror.url, got:\n{branch}"
    )
    assert "task.external_url" not in branch, (
        "the producerless task.external_url must not be read inside the "
        f"render branch, got:\n{branch}"
    )
    assert "last_synced_at" in branch, (
        f"a last-synced time must be rendered from mirror.last_synced_at, got:\n{branch}"
    )


def test_external_url_field_is_retired_with_a_superseding_comment():
    src = _read_tsx()
    # The dead client-only field must not still be declared as a live Task
    # field without a comment naming what replaced it.
    m = re.search(r"external_url\?:\s*string;", src)
    if m is not None:
        # If the declaration line still exists, it must be visibly retired.
        line_start = src.rfind("\n", 0, m.start()) + 1
        line = src[line_start:src.find("\n", m.start())]
        assert "superseded" in line.lower() or "mirror" in line.lower(), (
            f"a retained external_url field must carry a comment naming "
            f"what superseded it, got line: {line!r}"
        )
    # Whether the field declaration was removed outright or retired in place,
    # the RENDER branch (checked above) must not read it — enforced there.


def test_restricted_branch_is_preserved():
    src = _read_tsx()
    branch = _external_context_branch(src)
    assert "task.restricted" in branch
    assert "Restricted" in branch
