"""Recall-log attribution readers — the Knowledge · Understand rail + the
Understand 'Cited by' extension (ws3 delta round).

Pins the two new server readers and the OkfHost join that back them:

  * MemoryService.concepts_recalled_by_task — recall_log.task_id -> distinct
    entry rows (backs the Task detail rail);
  * MemoryService.tasks_that_recalled — recall_log.entry_id -> distinct task
    rows with outcome (backs the concept 'Cited by' extension);
  * OkfHost.task_concepts / .concept_recallers — the same, resolved against
    the live bundle so only surviving concepts surface.

All are guarded: empty/unknown ids and a store with no recalls yield [].
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _mem(tmp_path):
    from prism_service.services.memory_service import MemoryService
    return MemoryService(str(tmp_path / "mulch"))


def _log(svc, entry_id, task_id, when, domain="conductor"):
    """Insert one recall_log row via the internal logger (SimpleNamespace
    stands in for an ExpertiseEntry — _log_recall only reads .id/.domain)."""
    svc._log_recall(SimpleNamespace(id=entry_id, domain=domain), "q", when, task_id)


# ── MemoryService.concepts_recalled_by_task ────────────────────────────────

def test_concepts_recalled_by_task_groups_distinct_entries(tmp_path):
    svc = _mem(tmp_path)
    _log(svc, "mx-a", "task-1", "2026-07-13T00:00:01+00:00")
    _log(svc, "mx-a", "task-1", "2026-07-13T00:00:03+00:00")  # same entry, 2nd
    _log(svc, "mx-b", "task-1", "2026-07-13T00:00:02+00:00")
    _log(svc, "mx-c", "task-2", "2026-07-13T00:00:04+00:00")  # other task

    rows = svc.concepts_recalled_by_task("task-1")
    assert {r["entry_id"] for r in rows} == {"mx-a", "mx-b"}
    a = next(r for r in rows if r["entry_id"] == "mx-a")
    assert a["recall_count"] == 2
    assert a["last_recalled"] == "2026-07-13T00:00:03+00:00"
    # Newest recall first.
    assert rows[0]["entry_id"] == "mx-a"


def test_concepts_recalled_by_task_empty_id_is_guarded(tmp_path):
    svc = _mem(tmp_path)
    assert svc.concepts_recalled_by_task("") == []
    assert svc.concepts_recalled_by_task("never-recalled-anything") == []


# ── MemoryService.tasks_that_recalled ──────────────────────────────────────

def test_tasks_that_recalled_groups_distinct_tasks_with_outcome(tmp_path):
    svc = _mem(tmp_path)
    _log(svc, "mx-a", "task-1", "2026-07-13T00:00:01+00:00")
    _log(svc, "mx-a", "task-1", "2026-07-13T00:00:02+00:00")  # same task, 2nd
    _log(svc, "mx-a", "task-2", "2026-07-13T00:00:03+00:00")
    svc.record_outcome("task-1", "positive")

    rows = svc.tasks_that_recalled("mx-a")
    assert {r["task_id"] for r in rows} == {"task-1", "task-2"}
    t1 = next(r for r in rows if r["task_id"] == "task-1")
    assert t1["recall_count"] == 2
    assert t1["outcome"] == "positive"
    # task-2 has no recorded outcome yet.
    assert next(r for r in rows if r["task_id"] == "task-2")["outcome"] == ""


def test_tasks_that_recalled_empty_id_is_guarded(tmp_path):
    svc = _mem(tmp_path)
    assert svc.tasks_that_recalled("") == []
    assert svc.tasks_that_recalled("mx-nobody") == []


# ── OkfHost join — only surviving concepts surface ─────────────────────────

def test_okf_task_concepts_resolves_recalls_to_live_concepts(tmp_path):
    from prism_service.services.okf_host import OkfHost
    svc = _mem(tmp_path)
    entry = svc.store(
        domain="conductor", name="Gate enforcement doctrine",
        description="A gate is decided by a distinct actor.",
        type="convention", classification="tactical",
    )
    when = "2026-07-13T00:00:05+00:00"
    _log(svc, entry.id, "task-1", when, domain="conductor")
    # A recall for an entry that no longer exists as a concept is dropped.
    _log(svc, "mx-retired", "task-1", when, domain="conductor")

    host = OkfHost(svc)
    concepts = host.task_concepts("task-1")
    assert [c["id"] for c in concepts] == [entry.id]
    got = concepts[0]
    assert got["title"] == "Gate enforcement doctrine"
    assert got["type"] == "convention"
    assert got["recall_count"] == 1

    # Reverse direction: the concept lists task-1 as a recaller.
    recallers = host.concept_recallers(entry.id)
    assert [r["task_id"] for r in recallers] == ["task-1"]

    # And it rides on the concept payload the DetailPanel reads.
    payload = host.get(got["path"])
    assert payload is not None
    assert [r["task_id"] for r in payload["recalled_by"]] == ["task-1"]


def test_okf_task_concepts_empty_when_no_recalls(tmp_path):
    from prism_service.services.okf_host import OkfHost
    svc = _mem(tmp_path)
    svc.store(
        domain="conductor", name="Lonely concept", description="x",
        type="note", classification="tactical",
    )
    host = OkfHost(svc)
    assert host.task_concepts("task-1") == []
    assert host.concept_recallers("") == []
