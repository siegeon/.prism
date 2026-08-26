"""Imported GitHub issues ALIGN and keep their original text (task 683e65eb,
epic df0eed4a; owner decision 2026-08-26: the act is called ALIGN, not
converge).

``WorkItemSyncService._import_one`` (services/work_item_sync.py) already
routes every task write through ``TaskService`` — but its FIRST-import path
(``ensure_external_intake``, task_service.py:758) writes description with a
raw INSERT and never runs ``ste.apply`` (STE style fixes + lexicon.align),
so a freshly imported body used to land unaligned and with no record of
what it looked like before. Task 683e65eb closes that gap: the create path
now runs one ``TaskService.update()`` call so the SAME normaliser every
other task field goes through also aligns the body, and its own
before/after bookkeeping records the ORIGINAL text on the ste_normalise
history row.

Pins:
- AC-1: a fresh import's description is the STE+lexicon aligned form of the
  real GitHub issue body; the ste_normalise history row carries the
  UNALIGNED original body under ``before``; channel/channel_ref provenance
  survives the align pass untouched.
- AC-2: a second import of the SAME unchanged issue writes nothing — no
  updated_at churn, no new history row (the existing no-clobber path in
  ensure_external_intake already gives this for free once the row exists).
- AC-3: a second, distinct issue aligns ITS OWN body and records its own
  ``before`` — the align-on-create path is general, not a one-body fix.
- AC-4 (the "no ping-pong" half): pushing a LOCAL task creates a GitHub
  issue carrying the task's ALREADY-aligned description (pushed once,
  verbatim), and pulling that same issue straight back changes nothing —
  no new history row, no updated_at churn. This is the existing
  claim_import_link/no-clobber path (work_item_sync.py:223-232,
  task_service.py:758-795) doing its job; no production change was needed
  for this half.

NOT implemented, and deliberately so: re-aligning an EXISTING imported
task's description when the SAME task's upstream body changes on a later
sync. Doing that unconditionally would violate the no-clobber contract
already pinned by
test_import_carries_issue_body.py::test_human_edited_task_survives_a_resync
(task 4db228ec AC-3) — nothing in the schema distinguishes "this
description still matches what the mirror last wrote" from "a person
edited it", so any comparison-based re-align on an EXISTING row would
clobber real edits too. See the report for task 683e65eb.

Modeled on the scripted-adapter + IntegrationStore + TaskService harness in
tests/unit/test_import_carries_issue_body.py, and the fake create()
adapter shape in tests/unit/test_switch_on_pushes_backlog.py.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

WS_A = "workspace-a"

ISSUE_BODY = "Please open a ticket for the PR; it's blocking us"


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


def _import_task(tmp_path, body=ISSUE_BODY, remote_id="gh-501",
                 display_key="#501",
                 url="https://github.com/siegeon/.prism/issues/501"):
    store = _store(tmp_path)
    tasks = _task_svc(tmp_path)
    conn = store.ensure_connection(WS_A, "github", "install-1")
    cont = store.ensure_container(WS_A, conn.id, "repository", ".prism")
    adapter = _scripted_adapter("github", [{"entities": [
        {"remote_id": remote_id, "display_key": display_key,
         "title": "raw github headline", "body": body, "url": url},
    ]}])
    svc = _sync(store, tasks, adapter)
    svc.pull_container(WS_A, conn, cont)
    # Resolve THIS entity's own link (several tests import more than one
    # issue into the same store) rather than assuming list_links()[0] —
    # the link for the entity this call just imported is the last one
    # claimed, since claim_import_link only ever appends.
    link = store.list_links(WS_A)[-1]
    task = tasks.get(link.task_id)
    return store, tasks, conn, cont, task


def _ste_normalise_rows(tasks, task_id):
    return [h for h in tasks.history(task_id) if h.action == "ste_normalise"]


def _before_payload(row):
    marker = " before="
    idx = row.details.index(marker) + len(marker)
    return json.loads(row.details[idx:])


# ── AC-1: a fresh import aligns the body and keeps the original ─────────

def test_fresh_import_aligns_the_body_and_keeps_the_original_under_before(tmp_path):
    from prism_service.services import ste

    store, tasks, conn, cont, task = _import_task(tmp_path)
    expected, _findings = ste.apply(ISSUE_BODY)

    assert task.description == expected
    assert "Task" in task.description
    assert "PullRequest" in task.description
    assert "It is blocking us" in task.description

    # The provenance channel survives the align pass untouched.
    assert task.channel == "github"
    assert task.channel_ref == "https://github.com/siegeon/.prism/issues/501"

    rows = _ste_normalise_rows(tasks, task.id)
    assert len(rows) == 1
    before = _before_payload(rows[0])
    assert before["description"] == ISSUE_BODY


# ── AC-2: a second import of the SAME unchanged issue writes nothing ────

def test_second_import_of_unchanged_issue_writes_nothing(tmp_path):
    store, tasks, conn, cont, task = _import_task(tmp_path)
    task_id = task.id
    before_updated_at = tasks.get(task_id).updated_at
    before_history_count = len(tasks.history(task_id))

    adapter2 = _scripted_adapter("github", [{"entities": [
        {"remote_id": "gh-501", "display_key": "#501",
         "title": "raw github headline", "body": ISSUE_BODY,
         "url": "https://github.com/siegeon/.prism/issues/501"},
    ]}])
    svc2 = _sync(store, tasks, adapter2)
    svc2.pull_container(WS_A, conn, cont)

    after = tasks.get(task_id)
    assert after.updated_at == before_updated_at
    assert len(tasks.history(task_id)) == before_history_count


# ── AC-3: a second, distinct issue aligns its OWN body, own before ──────

def test_a_second_distinct_issue_aligns_its_own_body_and_records_its_own_before(tmp_path):
    from prism_service.services import ste

    store, tasks, conn, cont, task_a = _import_task(tmp_path)

    other_body = "The story isn't ready for a review yet"
    adapter2 = _scripted_adapter("github", [{"entities": [
        {"remote_id": "gh-777", "display_key": "#777",
         "title": "a second issue", "body": other_body,
         "url": "https://github.com/siegeon/.prism/issues/777"},
    ]}])
    svc2 = _sync(store, tasks, adapter2)
    svc2.pull_container(WS_A, conn, cont)

    links = store.list_links(WS_A)
    task_b = next(t for t in (tasks.get(l.task_id) for l in links)
                  if t.id != task_a.id)

    expected_b, _findings = ste.apply(other_body)
    assert task_b.description == expected_b
    assert task_b.description != task_a.description

    rows_b = _ste_normalise_rows(tasks, task_b.id)
    assert len(rows_b) == 1
    before_b = _before_payload(rows_b[0])
    assert before_b["description"] == other_body


# ── AC-4: no ping-pong — push once, pull back, nothing churns ───────────

class _FakeCreateAdapter:
    """Stands in for the network only, mirroring the create() double in
    test_switch_on_pushes_backlog.py — records the exact body handed to
    it, so the test can assert the OUTBOUND leg sends the aligned text."""

    provider = "github"

    def __init__(self):
        self.calls: list[dict] = []

    def create(self, connection, container, title, body="", assignee=""):
        from prism_service.models.integration import ExternalEntityInput

        self.calls.append({"title": title, "body": body})
        return ExternalEntityInput(
            entity_kind="issue", remote_id="I_push501", display_key="#901",
            title=title, body=body,
            url="https://github.com/siegeon/.prism/issues/901",
            remote_status="open", status_category="open",
            remote_updated_at="2026-08-26T00:00:00Z")


def test_push_then_pull_back_is_a_no_op_round_trip(tmp_path):
    from prism_service.services.integration_outbox import IntegrationOutbox
    from prism_service.services.work_item_sync import push_task_creation

    store = _store(tmp_path)
    tasks = _task_svc(tmp_path)
    outbox = IntegrationOutbox(str(tmp_path / "outbox.db"))

    # A LOCAL task, created (not imported) — its description already ran
    # through TaskService.create()'s own STE+lexicon align pass.
    local_task = tasks.create(title="Local task", description=ISSUE_BODY)
    assert local_task.description != ISSUE_BODY  # sanity: really aligned

    conn = store.ensure_connection(WS_A, "github", "install-push")
    cont = store.ensure_container(WS_A, conn.id, "repository", ".prism")
    adapter = _FakeCreateAdapter()

    result = push_task_creation(
        store, outbox, {"github": adapter}, lambda ws, provider: True,
        WS_A, local_task.id, task_status="pending",
        title=local_task.title, body=local_task.description, assignee="")

    assert result.created is True
    # The aligned text is sent ONCE, verbatim — never the raw original.
    assert adapter.calls[0]["body"] == local_task.description

    after_push = tasks.get(local_task.id)
    before_updated_at = after_push.updated_at
    before_history_count = len(tasks.history(local_task.id))

    # GitHub echoes the SAME body back on the next inbound pull.
    pull_adapter = _scripted_adapter("github", [{"entities": [
        {"remote_id": "I_push501", "display_key": "#901",
         "title": local_task.title, "body": after_push.description,
         "url": "https://github.com/siegeon/.prism/issues/901"},
    ]}])
    svc2 = _sync(store, tasks, pull_adapter)
    svc2.pull_container(WS_A, conn, cont)

    after_pull = tasks.get(local_task.id)
    assert after_pull.description == after_push.description
    assert after_pull.updated_at == before_updated_at
    assert len(tasks.history(local_task.id)) == before_history_count
