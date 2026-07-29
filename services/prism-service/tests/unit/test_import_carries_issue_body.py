"""RED scaffold - imported issue bodies land on the task (task 4db228ec).

Today `_import_one` (services/work_item_sync.py) only ever writes a two-line
mirror-pointer stub into the description, never the real GitHub issue body
that github_work._issue_input already captured on ExternalEntityInput.body
and that store.upsert_entity already persists. This file fails (import
error / assertion error) until _import_one forwards entity_input.body into
the description it passes to ensure_external_intake.

Pins:
- AC-1: a fresh import's description carries the real body verbatim, plus
  the mirror provenance (display_key + url).
- AC-2: syncing the same remote item twice does not duplicate the body.
- AC-3: a human-edited local task survives a re-sync unchanged, even though
  the remote body/title changed underneath it (the no-clobber contract in
  ensure_external_intake, task_service.py:409-442, must not be bypassed).

Modeled on the scripted-adapter + IntegrationStore + TaskService harness in
tests/integration/test_external_work_sync.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

WS_A = "workspace-a"

ISSUE_BODY = (
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
    including body/display_key/url (the ones _import_one must forward)."""
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


def _import_task(tmp_path, body=ISSUE_BODY, url="https://github.com/siegeon/.prism/issues/222"):
    store = _store(tmp_path)
    tasks = _task_svc(tmp_path)
    conn = store.ensure_connection(WS_A, "github", "install-1")
    cont = store.ensure_container(WS_A, conn.id, "repository", ".prism")
    adapter = _scripted_adapter("github", [{"entities": [
        {"remote_id": "gh-222", "display_key": "#222", "title": "raw github headline",
         "body": body, "url": url},
    ]}])
    svc = _sync(store, tasks, adapter)
    svc.pull_container(WS_A, conn, cont)
    link = store.list_links(WS_A)[0]
    task = tasks.get(link.task_id)
    return store, tasks, conn, cont, task


# ── AC-1: the real body lands in the description, provenance retained ──

def test_imported_description_carries_the_real_body_and_provenance(tmp_path):
    _, _, _, _, task = _import_task(tmp_path)

    assert "Where the hole is, from the source" in task.description
    assert "#222" in task.description
    assert "https://github.com/siegeon/.prism/issues/222" in task.description


# ── AC-2: a second sync of the same remote item does not duplicate the body ──

def test_second_sync_does_not_duplicate_the_body(tmp_path):
    store, tasks, conn, cont, task = _import_task(tmp_path)

    adapter2 = _scripted_adapter("github", [{"entities": [
        {"remote_id": "gh-222", "display_key": "#222", "title": "raw github headline",
         "body": ISSUE_BODY, "url": "https://github.com/siegeon/.prism/issues/222"},
    ]}])
    svc2 = _sync(store, tasks, adapter2)
    svc2.pull_container(WS_A, conn, cont)

    link = store.list_links(WS_A)[0]
    task_after = tasks.get(link.task_id)
    assert task_after.description.count("Where the hole is, from the source") == 1


# ── AC-3: a human-edited local row is untouched by a re-sync ────────────

def test_human_edited_task_survives_a_resync(tmp_path):
    store, tasks, conn, cont, task = _import_task(tmp_path)

    link = store.list_links(WS_A)[0]
    tasks.update(
        link.task_id,
        title="Gate the ticket's premise, not just its form",
        description="hand-enriched local text, not the mirror stub",
    )

    adapter2 = _scripted_adapter("github", [{"entities": [
        {"remote_id": "gh-222", "display_key": "#222", "title": "a totally different remote headline",
         "body": "a totally different remote body", "url": "https://github.com/siegeon/.prism/issues/222"},
    ]}])
    svc2 = _sync(store, tasks, adapter2)
    svc2.pull_container(WS_A, conn, cont)

    task_after = tasks.get(link.task_id)
    assert task_after.title == "Gate the ticket's premise, not just its form"
    assert task_after.description == "hand-enriched local text, not the mirror stub"
