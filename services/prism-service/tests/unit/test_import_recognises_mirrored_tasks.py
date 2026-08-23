"""RED — inbound import must recognise a task it already mirrors outbound
(task 4fa13457).

Today ``WorkItemSyncService._import_one`` computes a DETERMINISTIC task_id
from (workspace, connection, entity_kind, remote_id) and materializes an
intake row under THAT id, ignoring whatever task the entity's
``work_item_external_link`` already points at. A task created locally and
pushed outbound (``push_task_creation``) links its entity to the ORIGINAL
task id, which is a random uuid4 -- never the deterministic hash. So the
very next inbound pull of that same issue creates a SECOND, duplicate local
task under the deterministic id, even though the entity was already mirrored.

This pins the fix's observable contract: after importing an issue that an
existing local task already mirrors outbound, the task count referencing
that issue is exactly 1 -- the pre-existing task, never a new row.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

WS = "workspace-mirror"


def _store(tmp_path):
    from prism_service.services.integration_store import IntegrationStore

    return IntegrationStore(str(tmp_path / "integrations.db"))


def _task_svc(tmp_path):
    from prism_service.services.task_service import TaskService

    return TaskService(str(tmp_path / "tasks.db"))


class _FakeGithubAdapter:
    """Hands back one page containing the SAME issue a task was already
    pushed to, on every pull -- exactly what an inbound sync would see for
    an issue this workspace already mirrors outbound."""

    provider = "github"

    def __init__(self, entity_input):
        self._entity_input = entity_input

    def pull_page(self, connection, container, cursor, page_token):
        from prism_service.services.work_item_sync import PulledPage

        return PulledPage(entities=[self._entity_input], next_page_token=None,
                          next_cursor="cursor-1")


def test_inbound_import_links_existing_outbound_mirror_instead_of_creating(tmp_path):
    from prism_service.models.integration import ExternalEntityInput, task_id_for
    from prism_service.services.work_item_sync import WorkItemSyncService

    store = _store(tmp_path)
    tasks = _task_svc(tmp_path)

    conn = store.ensure_connection(WS, "github", "acme/widgets")
    cont = store.ensure_container(WS, conn.id, "github_repo", "acme/widgets")

    # 1. A task that already exists locally and was pushed outbound: its
    #    description carries the mirror pointer (task 900a4fb9's provenance
    #    convention), and its issue's entity/link were minted by
    #    push_task_creation under the ORIGINAL (random) task id -- never the
    #    deterministic task_id_for(...) hash inbound import would compute.
    local_task = tasks.create(
        title="Fix the widget renderer",
        description="Mirrored to github acme/widgets #42.",
    )

    entity_input = ExternalEntityInput(
        entity_kind="issue",
        remote_id="42",
        display_key="#42",
        title="Fix the widget renderer",
        body="",
        remote_status="open",
        status_category="",
    )
    entity, _created = store.upsert_entity(WS, conn.id, cont.id, entity_input)
    link = store.claim_import_link(WS, entity.id, local_task.id)
    store.activate_link(WS, link.id)

    deterministic_task_id = task_id_for(WS, conn.id, "issue", "42")
    assert deterministic_task_id != local_task.id, (
        "the test premise requires the deterministic id to differ from the "
        "pre-existing task's real id -- otherwise this is not exercising "
        "the mismatch at all")

    # 2. Inbound import pulls the SAME issue #42.
    svc = WorkItemSyncService(store, intake=tasks)
    svc.register(_FakeGithubAdapter(entity_input))
    svc.pull_container(WS, conn, cont)

    # 3. Exactly one task references issue #42 -- the pre-existing one.
    #    No new row was minted under the deterministic hash id.
    all_tasks = tasks.list()
    referencing = [
        t for t in all_tasks
        if t.id == local_task.id or t.id == deterministic_task_id
    ]
    assert len(referencing) == 1, (
        f"expected exactly 1 task referencing issue #42, found "
        f"{len(referencing)}: {[t.id for t in referencing]} -- inbound "
        "import must recognise the entity's existing outbound mirror link "
        "instead of materializing a second task under the deterministic id")
    assert referencing[0].id == local_task.id

    surviving_link = store.list_links(WS, entity_id=entity.id)[0]
    assert surviving_link.task_id == local_task.id, (
        "the entity's link must keep pointing at the pre-existing task, "
        "never get reassigned to the freshly-computed deterministic id")
