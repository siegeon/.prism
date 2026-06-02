"""Phase 2 (epic 4fd1e6b4) — red scaffold for the three learning-bus
emit seams. Task 8d90b12e.

Phase 1 built the substrate (services/event_pool.py: EventBus, get_bus()
singleton, the three pinned event-type constants, wrap/no-op handlers,
the budget-governed ConsumerPool, the lifespan-wired daemon). Phase 2
wires the THREE real emitters onto that bus while the timers keep
running in parallel (dual-run):

  1. memory.written        — emitted from the memory_store dispatch arm
                             AFTER memory_svc.store(...) succeeds.
  2. memory.recalled+outcome — emitted from the task_update dispatch arm
                             when memory_svc.record_outcome updated >0
                             recall_log rows (an outcome was attached).
  3. session.imported      — emitted from claude_transcripts.import_unseen
                             after conn.commit(), ALONGSIDE (not instead
                             of) _enqueue_with_signals.

These pin the USER-FACING seam, not a unit contract: each event is
asserted to land on the PROCESS-SINGLETON bus (event_pool.get_bus())
after driving the REAL MCP dispatcher (handle_tool) / the REAL disk
importer — so a method that exists but is never wired would still fail.

They FAIL today: no emit call exists at any of the three seams.

Hazard note (memory mx-c83f9a): if the red_gate verifier only runs a
skipped ruff lint and never runs pytest, it is STRUCTURALLY BLIND and
must be overridden WITH this red trace, not treated as a genuine pass.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


# ----------------------------------------------------------------------
# Isolation: per-project data dir + a FRESH process-singleton bus so the
# emit assertions can't be contaminated by another test's events.
# ----------------------------------------------------------------------
def _isolated_project(tmp_path, pid="test-emit-seams"):
    from prism_service import config as cfg

    original = cfg.PROJECTS_DIR
    cfg.PROJECTS_DIR = tmp_path / "projects"
    cfg.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    from prism_service import project_context as pc

    pc._contexts.clear()
    yield pid
    cfg.PROJECTS_DIR = original
    pc._contexts.clear()


@pytest.fixture
def project(tmp_path):
    yield from _isolated_project(tmp_path)


@pytest.fixture
def fresh_bus():
    """Reset the event_pool process-singleton so each test inspects a
    bus that only carries the events its own seam emitted."""
    from prism_service.services import event_pool as ep

    saved = ep._BUS
    ep._BUS = None
    bus = ep.get_bus()  # re-registers the wrap/no-op default handlers
    yield bus
    ep._BUS = saved


def _drain_types(bus) -> list[str]:
    """Claim every queued event off the bus and return its type list.
    The dual-run safety means handlers are no-ops, so events sit on the
    queue until claimed."""
    seen: list[str] = []
    while True:
        ev = bus.claim_one()
        if ev is None:
            break
        seen.append(ev.type)
    return seen


def _call(tool_name, arguments=None, project_id="test-emit-seams"):
    from prism_service.mcp.tools import handle_tool

    return asyncio.run(
        handle_tool(tool_name, arguments or {}, project_id=project_id)
    )


def _text(result):
    assert len(result) == 1
    return result[0].text


# ----------------------------------------------------------------------
# SEAM 1 — memory.written emitted from the memory_store dispatch arm
# ----------------------------------------------------------------------
def test_memory_store_emits_memory_written(project, fresh_bus):
    """Driving memory_store through the REAL MCP dispatcher must land a
    MEMORY_WRITTEN event on the process-singleton learning bus, AFTER the
    durable memory_svc.store write succeeded."""
    from prism_service.services import event_pool as ep

    res = json.loads(_text(_call("memory_store", {
        "domain": "project",
        "name": "phase2-emit-probe",
        "description": "probe that the write seam emits memory.written",
        "type": "decision",
        "classification": "tactical",
        "importance": 5,
    })))
    # The durable write itself still returns its entry (behavior unchanged).
    assert res, "memory_store returned an empty result"

    types = _drain_types(fresh_bus)
    assert ep.MEMORY_WRITTEN in types, (
        "memory_store did not emit MEMORY_WRITTEN onto the learning bus — "
        f"saw {types!r}. The write seam is not wired to event_pool.emit()."
    )


# ----------------------------------------------------------------------
# SEAM 2 — memory.recalled+outcome emitted from task_update when an
# outcome is actually attached (record_outcome updated >0 rows).
# ----------------------------------------------------------------------
def test_task_update_done_emits_recalled_outcome_when_attached(project, fresh_bus):
    """A done/blocked task_update that ATTACHES an outcome to a prior
    recall (record_outcome updates >0 recall_log rows) must emit
    MEMORY_RECALLED_OUTCOME on the bus."""
    from prism_service.project_context import get_project
    from prism_service.services import event_pool as ep

    # Create a task, then make a real recall logged against that task_id so
    # record_outcome has a row to update on done.
    t = json.loads(_text(_call("task_create", {"title": "outcome task"})))
    tid = t["id"]

    # Seed a memory + a recall_log row tied to this task so record_outcome
    # updates >0 rows.
    _call("memory_store", {
        "domain": "project", "name": "recall-seed",
        "description": "seed entry so a recall can be logged + scored",
        "type": "convention", "classification": "tactical",
    })
    ctx = get_project(project)
    # _log_recall stamps a recall_log row with this task_id.
    entries = ctx.memory_svc.recall(query="recall seed", limit=5)
    # Force a recall_log row tied to tid even if recall() doesn't carry a
    # task_id by default: write one directly through the service DB.
    assert hasattr(ctx.memory_svc, "_recall_db")
    if entries:
        ctx.memory_svc._recall_db.execute(
            "INSERT INTO recall_log (entry_id, entry_domain, query, "
            "recalled_at, task_id, outcome) VALUES (?, ?, ?, ?, ?, '')",
            (entries[0].id, entries[0].domain, "recall seed",
             time.strftime("%Y-%m-%d %H:%M:%S"), tid),
        )
        ctx.memory_svc._recall_db.commit()

    # Drain whatever the seeding emitted (e.g. the memory.written above) so
    # we measure only the task_update seam.
    _drain_types(fresh_bus)

    _call("task_update", {"id": tid, "status": "done"})

    types = _drain_types(fresh_bus)
    assert ep.MEMORY_RECALLED_OUTCOME in types, (
        "task_update->done attached an outcome to a recall (record_outcome "
        f"updated >0 rows) but did not emit MEMORY_RECALLED_OUTCOME — saw "
        f"{types!r}."
    )


def test_task_update_done_no_emit_when_nothing_attached(project, fresh_bus):
    """When no recall is tied to the task (record_outcome updates 0 rows),
    the outcome seam must NOT emit — the event fires only on a real
    attach."""
    from prism_service.services import event_pool as ep

    t = json.loads(_text(_call("task_create", {"title": "no-recall task"})))
    tid = t["id"]
    _drain_types(fresh_bus)

    _call("task_update", {"id": tid, "status": "done"})

    types = _drain_types(fresh_bus)
    assert ep.MEMORY_RECALLED_OUTCOME not in types, (
        "task_update emitted MEMORY_RECALLED_OUTCOME even though no recall "
        "was attached (record_outcome updated 0 rows) — the emit must be "
        "gated on updated>0."
    )


# ----------------------------------------------------------------------
# SEAM 3 — session.imported emitted from import_unseen after conn.commit,
# alongside _enqueue_with_signals (dual-run; enqueue still fires).
# ----------------------------------------------------------------------
def _write_transcript(dir_path: Path, session_id: str) -> Path:
    """Minimal valid transcript: a Write tool_use so it carries a
    files_modified signal and survives the v6.2.18 noise filter."""
    dir_path.mkdir(parents=True, exist_ok=True)
    line = {
        "sessionId": session_id,
        "timestamp": "2026-06-02T00:00:00.000Z",
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "name": "Write",
                 "input": {"file_path": "/tmp/x.py"}},
            ],
        },
    }
    p = dir_path / f"{session_id}.jsonl"
    p.write_text(json.dumps(line) + "\n", encoding="utf-8")
    return p


def test_import_unseen_emits_session_imported(project, fresh_bus, tmp_path):
    """import_unseen (the disk importer) must emit SESSION_IMPORTED on the
    bus after conn.commit() for a newly imported session."""
    from prism_service.project_context import get_project
    from prism_service.services import claude_transcripts as ct
    from prism_service.services import event_pool as ep

    ctx = get_project(project)
    scores_db = str(ctx._data_dir / "scores.db")
    # Ensure the scores.db schema exists by touching a dispatcher path that
    # writes a session_outcomes row, then clearing the bus.
    _call("record_session_outcome", {
        "session_id": "warmup", "duration_s": 1, "tokens_used": 1,
        "files_read": 0, "files_modified": 0, "skills_invoked": 0,
    })
    _drain_types(fresh_bus)

    tdir = tmp_path / "claude_override"
    _write_transcript(tdir, "S-imported-1")

    n = ct.import_unseen(
        scores_db,
        project_source_path=str(tmp_path / "src"),
        override_dir=str(tdir),
    )
    assert n == 1, f"importer should have imported exactly 1 session, got {n}"

    types = _drain_types(fresh_bus)
    assert ep.SESSION_IMPORTED in types, (
        "import_unseen committed an imported session but did not emit "
        f"SESSION_IMPORTED onto the learning bus — saw {types!r}."
    )


def test_import_unseen_dual_runs_enqueue_and_emit(project, fresh_bus, tmp_path):
    """Dual-run guarantee: the existing _enqueue_with_signals path STILL
    runs alongside the new emit (a consolidation_candidate is produced),
    so end-to-end behavior is unchanged this phase."""
    from prism_service.project_context import get_project
    from prism_service.services import claude_transcripts as ct
    from prism_service.services import event_pool as ep

    ctx = get_project(project)
    scores_db = str(ctx._data_dir / "scores.db")
    _call("record_session_outcome", {
        "session_id": "warmup2", "duration_s": 1, "tokens_used": 1,
        "files_read": 0, "files_modified": 0, "skills_invoked": 0,
    })
    _drain_types(fresh_bus)

    tdir = tmp_path / "claude_override2"
    _write_transcript(tdir, "S-imported-2")
    ct.import_unseen(
        scores_db,
        project_source_path=str(tmp_path / "src"),
        override_dir=str(tdir),
    )

    # Emit landed...
    assert ep.SESSION_IMPORTED in _drain_types(fresh_bus)

    # ...AND the legacy enqueue still produced a consolidation candidate
    # for that session (dual-run, nothing retired this phase).
    conn = sqlite3.connect(scores_db)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM consolidation_candidates "
            "WHERE session_id = ?",
            ("S-imported-2",),
        ).fetchone()
    finally:
        conn.close()
    assert row[0] >= 1, (
        "session.imported emit must run ALONGSIDE _enqueue_with_signals — "
        "the legacy enqueue produced no consolidation_candidate, so the "
        "emit replaced the enqueue instead of dual-running."
    )
