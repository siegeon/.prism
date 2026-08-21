"""RED scaffold — Jira import carries the real issue description (task
2ed7186a). Pins: an ADF description converts to readable plain
text/markdown that LEADS the task description, the mirror provenance line
TRAILS it (matching the GitHub precedent, task 4db228ec), an unrecognized
ADF node degrades to its nested text instead of being dropped or raising,
and a missing/null description keeps today's provenance-only stub
unchanged. No raw ADF JSON may ever appear in the resulting description.

Prism modules import INSIDE helpers/tests so the file collects and fails at
runtime (red = rc 1) before `_adf_to_text`/`_description_text` exist and
before `_issue_input` wires `body` through.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_FIXTURES = _HERE.parent.parent / "fixtures" / "jira"
WS_A = "workspace-a"


def _fixture(name):
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


class _FakeJiraClient:
    """Returns a canned /search/jql page for a single cloud_id; records
    every params/fields the adapter passed through search_jql."""

    def __init__(self, page):
        self._page = page
        self.calls = []

    def search_jql(self, cloud_id, access_token, jql, page_token=None, max_results=50):
        self.calls.append({"cloud_id": cloud_id, "jql": jql})
        return self._page


def _store(tmp_path):
    from prism_service.services.integration_store import IntegrationStore

    return IntegrationStore(str(tmp_path / "integrations.db"))


def _task_svc(tmp_path):
    from prism_service.services.task_service import TaskService

    return TaskService(str(tmp_path / "tasks.db"))


def _adapter(client, token="access-token"):
    from prism_service.services.jira_work import JiraWorkAdapter

    return JiraWorkAdapter(client, access_token_provider=lambda connection: token)


def _sync(store, tasks, adapter):
    from prism_service.services.work_item_sync import WorkItemSyncService

    svc = WorkItemSyncService(store, intake=tasks)
    svc.register(adapter)
    return svc


def _import_single_issue(tmp_path, fixture_name):
    """Pulls one fixture through the full adapter + sync stack and returns
    the resulting local task's description."""
    client = _FakeJiraClient(_fixture(fixture_name))
    store = _store(tmp_path)
    tasks = _task_svc(tmp_path)
    conn = store.ensure_connection(WS_A, "jira", "cloud-1")
    cont = store.ensure_container(WS_A, conn.id, "jira_project", "PROJ")
    _sync(store, tasks, _adapter(client)).pull_container(WS_A, conn, cont)

    entity = store.list_entities(WS_A)[0]
    link = store.list_links(WS_A, entity_id=entity.id)[0]
    task = tasks.get(link.task_id)
    return task.description


def test_description_leads_with_provenance_trailing(tmp_path):
    description = _import_single_issue(tmp_path, "search_jql_with_description.json")

    # site_url is not wired in this harness, so the adapter's default
    # site_url_provider yields "" and the browse-URL line of provenance is
    # absent; assert on the parts that ARE stable across that config.
    assert description.startswith(
        "Paging past page 3 repeats the first result set instead of advancing."
    ), description
    assert "Repro steps:" in description
    assert "- Open the search page and run any query." in description
    assert "- Click next until page 4." in description
    # the two bullet lines are adjacent, one per line — no blank line
    # splitting them apart.
    assert (
        "- Open the search page and run any query.\n"
        "- Click next until page 4."
    ) in description
    assert description.endswith("Mirrored from jira PROJ PRIS-9."), description
    # body leads, provenance trails — never the other way around, and never
    # raw ADF JSON (no braces, no "type": markers) anywhere in the output.
    assert description.index("Paging past page 3") < description.index("Mirrored from jira")
    assert "{" not in description
    assert '"type"' not in description
    assert '"content"' not in description


def test_unrecognized_adf_node_degrades_to_its_text(tmp_path):
    description = _import_single_issue(tmp_path, "search_jql_unknown_adf_node.json")

    assert "This panel node type is not explicitly handled by the walker." in description
    assert "{" not in description
    assert '"type"' not in description


def test_missing_description_keeps_todays_stub(tmp_path):
    description = _import_single_issue(tmp_path, "search_jql_no_description.json")

    assert description == "Mirrored from jira PROJ PRIS-11.", description
    assert "\n\n" not in description, (
        "a missing description must produce the bare provenance stub, "
        "never a leading blank body separator"
    )
