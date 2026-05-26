"""v6.0.18 — /learning's `recent_reflections` panel data-access.

Covers two regressions:

1. The /learning page was empty after a reflection ran. Cause:
   task_quality_rollup is only populated when a candidate carries a
   task_id, but transcript-derived candidates only carry session_id.
   Fix: surface consolidation_runs directly via get_recent_reflections.

2. The runs join consolidation_candidates so the UI can render the
   subject context (task_id vs session_id vs trigger) without a
   second round-trip.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _seed_schema(scores_db):
    from prism_service.engines.brain_engine import Brain
    Brain(
        brain_db=str(Path(scores_db).parent / "brain.db"),
        graph_db=str(Path(scores_db).parent / "graph.db"),
        scores_db=scores_db,
    )


def _insert_candidate(scores_db, *, cid, task_id=None, session_id=None,
                      trigger="task_done", status="completed"):
    c = sqlite3.connect(scores_db)
    try:
        c.execute(
            "INSERT INTO consolidation_candidates "
            "(id, task_id, session_id, trigger, status, queued_at) "
            "VALUES (?, ?, ?, ?, ?, datetime('now'))",
            (cid, task_id, session_id, trigger, status),
        )
        c.commit()
    finally:
        c.close()


def _insert_run(scores_db, *, run_id, candidate_id, run_at, payload,
                subagent_type="prism-reflect", confidence=0.4):
    c = sqlite3.connect(scores_db)
    try:
        c.execute(
            "INSERT INTO consolidation_runs "
            "(id, candidate_id, run_at, output_json, subagent_type, confidence) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, candidate_id, run_at, json.dumps(payload),
             subagent_type, confidence),
        )
        c.commit()
    finally:
        c.close()


def test_get_recent_reflections_returns_empty_when_no_db(tmp_path):
    from prism_service.services.learning_data import get_recent_reflections
    assert get_recent_reflections(str(tmp_path / "nope.db")) == []


def test_get_recent_reflections_flattens_payload_and_joins_candidate(tmp_path):
    """Each row carries qualitative_score + narrative_excerpt extracted
    from output_json, plus the candidate's task_id / session_id /
    trigger via LEFT JOIN."""
    from prism_service.services.learning_data import get_recent_reflections
    scores = str(tmp_path / "scores.db")
    _seed_schema(scores)

    # transcript-derived candidate (no task_id, only session_id)
    _insert_candidate(
        scores, cid="cand-sess", session_id="sess-abc",
        trigger="transcript_imported",
    )
    _insert_run(
        scores, run_id="run-1", candidate_id="cand-sess",
        run_at="2026-05-25T12:00:00+00:00",
        payload={
            "qualitative_score": 0.55,
            "narrative": "Walks JSONL with parse_session_metrics " * 6,
            "new_memories": [{"name": "m1"}, {"name": "m2"}],
        },
    )
    # task-derived candidate (has task_id)
    _insert_candidate(
        scores, cid="cand-task", task_id="T-42", trigger="task_done",
    )
    _insert_run(
        scores, run_id="run-2", candidate_id="cand-task",
        run_at="2026-05-25T13:00:00+00:00",
        payload={"qualitative_score": 0.82, "narrative": "Clean fix."},
    )

    rows = get_recent_reflections(scores)
    assert len(rows) == 2
    # Newest first by run_at
    assert rows[0]["id"] == "run-2"
    assert rows[0]["candidate_task_id"] == "T-42"
    assert rows[0]["qualitative_score"] == 0.82
    assert rows[0]["memories_minted"] == 0

    assert rows[1]["id"] == "run-1"
    assert rows[1]["candidate_session_id"] == "sess-abc"
    assert rows[1]["candidate_task_id"] is None
    assert rows[1]["memories_minted"] == 2
    # narrative_excerpt is capped at 280 chars
    assert len(rows[1]["narrative_excerpt"]) <= 280


def test_get_recent_reflections_honors_limit(tmp_path):
    from prism_service.services.learning_data import get_recent_reflections
    scores = str(tmp_path / "scores.db")
    _seed_schema(scores)
    _insert_candidate(scores, cid="c", session_id="s")
    for i in range(5):
        _insert_run(
            scores, run_id=f"r{i}", candidate_id="c",
            run_at=f"2026-05-25T1{i}:00:00+00:00",
            payload={"qualitative_score": 0.5},
        )
    assert len(get_recent_reflections(scores, limit=2)) == 2
