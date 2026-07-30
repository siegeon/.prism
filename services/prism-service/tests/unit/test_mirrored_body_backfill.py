"""RED scaffold - already-mirrored tasks get their bodies backfilled (task 82223365).

Slice 4db228ec made a FIRST import write the real GitHub issue body into a
mirrored task's description. But `TaskService.ensure_external_intake`
(task_service.py:427-429) returns an existing row UNCHANGED on every later
pull, by design, so a task imported BEFORE that fix keeps its two-line
mirror-pointer stub forever. This file fails until `_import_one`
(services/work_item_sync.py) backfills the description of an ALREADY-mirrored
task, but ONLY when its current description is byte-identical to the exact
stub it would have been given with an empty body - never a loose match, never
touching the title, never re-firing after one successful backfill.

Pins:
- AC-1: a pre-existing stub-only task gets the real body backfilled into its
  description on a later sync, provenance retained.
- AC-2: a task whose description was hand-edited to arbitrary text (not the
  exact stub) is left completely untouched by a later sync.
- AC-3: a second sync after a successful backfill does not re-fire, duplicate
  the body, or revert the description.
- AC-4: a task whose pulled body is empty is left alone (nothing to backfill).
- AC-5: the fix lives entirely in work_item_sync.py; task_service.py's
  no-clobber guarantee is reused, never weakened.

Modeled on the scripted-adapter + IntegrationStore + TaskService harness in
tests/unit/test_import_carries_issue_body.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

WS_A = "workspace-a"

REAL_BODY = (
    "Where the hole is, from the source\n\n"
    "The importer never forwards the body, so mirrored tasks are two lines "
    "of pointer and nothing else."
)


def _store(tmp_path, name="integrations.db"):
    from prism_service.services.integration_store import IntegrationStore

    return IntegrationStore(str(tmp_path / name))


def _task_svc(tmp_path, name="tasks.db"):
    from prism_service.services.task_service import TaskService

    return TaskService(str(tmp_path / name))


def _scripted_adapter(provider, pages):
    """A no-network adapter carrying full ExternalEntityInput fields,
    including body/display_key/url."""
    from prism_service.services.work_item_sync import PulledPage
    from prism_service.models.integration import ExternalEntityInput

    script = list(pages)

    class _Adapter:
        def __init__(self):
            self.provider = provider
            self.calls = 0

        def pull_page(self, connection, container, cursor, page_token):
            self.calls += 1
            idx = 0 if page_token is None else int(page_token)
            page = script[idx] if idx < len(script) else {"entities": []}
            ents = [
                ExternalEntityInput(
                    entity_kind=e.get("kind", "issue"),
                    remote_id=e["remote_id"],
                    display_key=e.get("display_key", ""),
                    title=e.get("title", ""),
                    body=e.get("body", ""),
                    url=e.get("url", ""),
                    remote_status=e.get("remote_status", "open"),
                    status_category=e.get("status_category", "open"),
                )
                for e in page.get("entities", [])
            ]
            return PulledPage(
                entities=ents,
                next_page_token=page.get("next_page_token"),
                next_cursor=page.get("next_cursor"),
            )

    return _Adapter()


def _sync(store, intake, *adapters):
    from prism_service.services.work_item_sync import WorkItemSyncService

    svc = WorkItemSyncService(store, intake=intake)
    for adapter in adapters:
        svc.register(adapter)
    return svc


def _import_stub_only(tmp_path, url="https://github.com/siegeon/.prism/issues/222"):
    """Seed a task exactly as a PRE-fix import would have: an empty-body
    pull, so the description is the bare mirror-pointer stub and nothing
    else - reproducing the ~25 already-mirrored rows this task backfills."""
    store = _store(tmp_path)
    tasks = _task_svc(tmp_path)
    conn = store.ensure_connection(WS_A, "github", "install-1")
    cont = store.ensure_container(WS_A, conn.id, "repository", ".prism")
    adapter = _scripted_adapter("github", [{"entities": [
        {"remote_id": "gh-222", "display_key": "#222", "title": "raw github headline",
         "body": "", "url": url},
    ]}])
    svc = _sync(store, tasks, adapter)
    svc.pull_container(WS_A, conn, cont)
    link = store.list_links(WS_A)[0]
    task = tasks.get(link.task_id)
    assert "Where the hole" not in task.description  # sanity: really stub-only
    return store, tasks, conn, cont, task


# ── AC-1: a later sync backfills the real body into a stub-only row ────────

def test_stub_only_task_gets_the_real_body_backfilled(tmp_path):
    store, tasks, conn, cont, task = _import_stub_only(tmp_path)
    old_title = task.title
    stub_description = task.description

    adapter2 = _scripted_adapter("github", [{"entities": [
        {"remote_id": "gh-222", "display_key": "#222", "title": "raw github headline",
         "body": REAL_BODY, "url": "https://github.com/siegeon/.prism/issues/222"},
    ]}])
    svc2 = _sync(store, tasks, adapter2)
    svc2.pull_container(WS_A, conn, cont)

    link = store.list_links(WS_A)[0]
    task_after = tasks.get(link.task_id)
    assert "Where the hole is, from the source" in task_after.description
    assert task_after.description != stub_description
    # provenance (the old stub content) still reachable as a trailer
    assert "#222" in task_after.description
    # the safety line: title is NEVER refreshed from the remote
    assert task_after.title == old_title
    assert task_after.title != "raw github headline"


# ── AC-2: a hand-edited description is never overwritten ───────────────────

def test_hand_edited_description_is_left_untouched(tmp_path):
    store, tasks, conn, cont, task = _import_stub_only(tmp_path)
    link = store.list_links(WS_A)[0]
    tasks.update(
        link.task_id,
        title="Gate the ticket's premise, not just its form",
        description="some human note, definitely not the stub",
    )

    adapter2 = _scripted_adapter("github", [{"entities": [
        {"remote_id": "gh-222", "display_key": "#222", "title": "raw github headline",
         "body": REAL_BODY, "url": "https://github.com/siegeon/.prism/issues/222"},
    ]}])
    svc2 = _sync(store, tasks, adapter2)
    svc2.pull_container(WS_A, conn, cont)

    task_after = tasks.get(link.task_id)
    assert task_after.description == "some human note, definitely not the stub"
    assert task_after.title == "Gate the ticket's premise, not just its form"


# ── AC-3: a backfilled row converges - a further sync does not re-fire ─────

def test_backfill_does_not_refire_or_duplicate_on_a_later_sync(tmp_path):
    store, tasks, conn, cont, task = _import_stub_only(tmp_path)

    adapter2 = _scripted_adapter("github", [{"entities": [
        {"remote_id": "gh-222", "display_key": "#222", "title": "raw github headline",
         "body": REAL_BODY, "url": "https://github.com/siegeon/.prism/issues/222"},
    ]}])
    svc2 = _sync(store, tasks, adapter2)
    svc2.pull_container(WS_A, conn, cont)
    link = store.list_links(WS_A)[0]
    backfilled_description = tasks.get(link.task_id).description

    adapter3 = _scripted_adapter("github", [{"entities": [
        {"remote_id": "gh-222", "display_key": "#222", "title": "raw github headline",
         "body": REAL_BODY, "url": "https://github.com/siegeon/.prism/issues/222"},
    ]}])
    svc3 = _sync(store, tasks, adapter3)
    svc3.pull_container(WS_A, conn, cont)

    task_after = tasks.get(link.task_id)
    assert task_after.description == backfilled_description
    assert task_after.description.count("Where the hole is, from the source") == 1


# ── AC-4: an empty pulled body backfills nothing (already "correct") ───────

def test_empty_pulled_body_leaves_the_stub_alone(tmp_path):
    store, tasks, conn, cont, task = _import_stub_only(tmp_path)
    stub_description = task.description

    adapter2 = _scripted_adapter("github", [{"entities": [
        {"remote_id": "gh-222", "display_key": "#222", "title": "raw github headline",
         "body": "", "url": "https://github.com/siegeon/.prism/issues/222"},
    ]}])
    svc2 = _sync(store, tasks, adapter2)
    svc2.pull_container(WS_A, conn, cont)

    link = store.list_links(WS_A)[0]
    task_after = tasks.get(link.task_id)
    assert task_after.description == stub_description
