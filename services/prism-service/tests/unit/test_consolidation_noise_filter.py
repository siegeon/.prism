"""Red-scaffold tests for consolidation noise filtering + memory-summary token bound.

Pins task ed7292bd against the REAL code surface (verified from source, not
invented contracts):

  (1) claude_transcripts._enqueue_with_signals(scores_db, session_id, metrics,
      ts_iso) must NOT enqueue a candidate when the session carries no usable
      signal: task_id would be NULL (always, on this path) AND every signal
      bucket is empty AND files_modified == 0.
  (2) consolidation_data.prune_noise_candidates(scores_db) one-shot DELETEs the
      existing zero-signal / task_id-NULL pending pile, returns a count, and is
      idempotent.
  (3) POST /api/consolidation/prune-noise exposes (2) end-to-end through the
      real FastAPI router (reachable, returns the deleted count).
  (4) memory_summary_worker default interval raised 60 -> 300.

The endpoint test drives the router with a TestClient over a real sqlite file
and counts rows from a SEPARATE connection — proving durability, not an
in-memory illusion. Column shape mirrors the production DDL in
engines/brain_engine.py:1003 (id, task_id, session_id, trigger, scope_json,
status, queued_at, ...). enqueue_for_session signature is
(scores_db, session_id, scope=None, trigger=...) — consolidation_data.py:232.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from prism_service.services import claude_transcripts, consolidation_data


# --------------------------------------------------------------------------
# schema + helpers
# --------------------------------------------------------------------------
def _make_scores_db(path: Path) -> str:
    """Create a scores.db with the consolidation_candidates table the bridge
    functions write to. The data-access functions only guard
    Path(scores_db).exists() — they do NOT create the table — so the test owns
    the DDL (mirrors brain_engine.py:1003)."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS consolidation_candidates ("
            "  id TEXT PRIMARY KEY,"
            "  task_id TEXT,"
            "  session_id TEXT,"
            "  trigger TEXT,"
            "  scope_json TEXT,"
            "  status TEXT DEFAULT 'pending',"
            "  queued_at TEXT,"
            "  staled_at TEXT,"
            "  dispensed_at TEXT,"
            "  completed_at TEXT,"
            "  retry_count INTEGER DEFAULT 0,"
            "  last_nudged_at TEXT"
            ")"
        )
        conn.commit()
    finally:
        conn.close()
    return str(path)


def _metrics(files_modified=0, pushbacks=0, bg_signals=0,
             tool_failures=0, memory_writes=0):
    """Build a metrics dict shaped like parse_session_metrics output
    (claude_transcripts.py:241). signal buckets are lists of DICTS — the
    exact shape format_transcript_excerpt reads (.get('ts'), .get('text'),
    .get('kind'), .get('error'), .get('domain')); using bare strings would
    make format_transcript_excerpt raise and the bridge's bare except would
    swallow the enqueue, producing a WRONG-REASON red. _enqueue_with_signals
    len()s each bucket into signal_counts."""
    return {
        "files_read": 0,
        "files_modified": files_modified,
        "skills_invoked": 0,
        "duration_s": 0,
        "tokens_used": 0,
        "signals": {
            "pushbacks": [{"ts": "", "text": "no"}] * pushbacks,
            "bg_signals": [{"kind": "result", "text": "ok"}] * bg_signals,
            "tool_failures": [{"ts": "", "error": "boom"}] * tool_failures,
            "memory_writes": [{"domain": "d", "name": "n", "ts": "",
                               "context": []}] * memory_writes,
        },
        "skill_invocations": [],
    }


def _pending_count(scores_db: str) -> int:
    # SEPARATE connection from whatever wrote — proves durability on disk.
    conn = sqlite3.connect(scores_db)
    try:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM consolidation_candidates "
                "WHERE status='pending'"
            ).fetchone()[0]
        )
    finally:
        conn.close()


def _seed(scores_db, session_id, *, signal_counts, task_id=None,
          files_modified=0, status="pending"):
    """Directly insert a pending candidate with a controlled scope_json."""
    import uuid
    from datetime import datetime, timezone

    scope = {
        "session_id": session_id,
        "signal_counts": signal_counts,
        "files_modified": files_modified,
    }
    conn = sqlite3.connect(scores_db)
    try:
        conn.execute(
            "INSERT INTO consolidation_candidates "
            "(id, task_id, session_id, trigger, scope_json, status, queued_at) "
            "VALUES (?, ?, ?, 'transcript_imported', ?, ?, ?)",
            (
                str(uuid.uuid4()), task_id, session_id,
                json.dumps(scope), status,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


_ZEROS = {"pushbacks": 0, "bg_signals": 0, "tool_failures": 0, "memory_writes": 0}


# --------------------------------------------------------------------------
# (a) _enqueue_with_signals skips a zero-signal / no-files session
# --------------------------------------------------------------------------
def test_enqueue_with_signals_skips_noise_session(tmp_path):
    scores_db = _make_scores_db(tmp_path / "scores.db")

    claude_transcripts._enqueue_with_signals(
        scores_db, "noise-1", _metrics(), "2026-05-31 00:00:00"
    )

    assert _pending_count(scores_db) == 0, (
        "zero-signal, task_id-null, files_modified==0 session must NOT enqueue"
    )


# --------------------------------------------------------------------------
# (b) _enqueue_with_signals still enqueues a real candidate
# --------------------------------------------------------------------------
def test_enqueue_with_signals_enqueues_on_any_signal(tmp_path):
    scores_db = _make_scores_db(tmp_path / "scores.db")

    claude_transcripts._enqueue_with_signals(
        scores_db, "real-1", _metrics(pushbacks=2), "2026-05-31 00:00:00"
    )

    assert _pending_count(scores_db) == 1, (
        "a session with any non-zero signal bucket must still enqueue"
    )


def test_enqueue_with_signals_enqueues_on_files_modified(tmp_path):
    scores_db = _make_scores_db(tmp_path / "scores.db")

    claude_transcripts._enqueue_with_signals(
        scores_db, "real-2", _metrics(files_modified=3), "2026-05-31 00:00:00"
    )

    assert _pending_count(scores_db) == 1, (
        "a session that modified files must still enqueue even with 0 signals"
    )


# --------------------------------------------------------------------------
# (c) prune_noise_candidates deletes only zero-signal task_id-null pending rows
# --------------------------------------------------------------------------
def test_prune_noise_candidates_deletes_only_noise(tmp_path):
    scores_db = _make_scores_db(tmp_path / "scores.db")
    _seed(scores_db, "noise-a", signal_counts=_ZEROS)
    _seed(scores_db, "noise-b", signal_counts=_ZEROS)
    _seed(scores_db, "real-a",
          signal_counts={**_ZEROS, "pushbacks": 1})          # carries signal
    _seed(scores_db, "linked-a", signal_counts=_ZEROS,
          task_id="task-123")                                 # task-linked

    deleted = consolidation_data.prune_noise_candidates(scores_db)

    assert deleted == 2, "only the two zero-signal task_id-null rows prune"
    assert _pending_count(scores_db) == 2, "signal/task rows survive"


# --------------------------------------------------------------------------
# (d) idempotency — second prune returns 0
# --------------------------------------------------------------------------
def test_prune_noise_candidates_is_idempotent(tmp_path):
    scores_db = _make_scores_db(tmp_path / "scores.db")
    _seed(scores_db, "noise-a", signal_counts=_ZEROS)

    first = consolidation_data.prune_noise_candidates(scores_db)
    second = consolidation_data.prune_noise_candidates(scores_db)

    assert first == 1
    assert second == 0, "pruning twice returns 0 on the second call"


# --------------------------------------------------------------------------
# end-to-end: POST /api/consolidation/prune-noise reachable through the router
# --------------------------------------------------------------------------
def test_prune_noise_endpoint_drains_pile(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from prism_service.api import consolidation as consolidation_api
    from prism_service import project_context

    scores_db = _make_scores_db(tmp_path / "scores.db")
    _seed(scores_db, "noise-a", signal_counts=_ZEROS)
    _seed(scores_db, "noise-b", signal_counts=_ZEROS)
    _seed(scores_db, "real-a", signal_counts={**_ZEROS, "pushbacks": 3})

    # Point get_project at our tmp scores.db via a stub ctx with _data_dir.
    class _Ctx:
        _data_dir = tmp_path

    monkeypatch.setattr(project_context, "get_project", lambda p: _Ctx())
    monkeypatch.setattr(consolidation_api, "get_project", lambda p: _Ctx())

    app = FastAPI()
    app.include_router(consolidation_api.router, prefix="/api/consolidation")
    client = TestClient(app)

    resp = client.post("/api/consolidation/prune-noise", params={"project": "prism"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("deleted") == 2, body
    assert _pending_count(scores_db) == 1, "real candidate survives the drain"


# --------------------------------------------------------------------------
# memory-summary worker default interval raised to 300s
# --------------------------------------------------------------------------
def test_memory_summary_worker_default_interval_is_300(monkeypatch):
    from prism_service.services import memory_summary_worker

    monkeypatch.delenv("PRISM_MEMORY_SUMMARY_WORKER_INTERVAL", raising=False)
    assert memory_summary_worker._interval_s() == 300
