"""v6.0.18 — PRISM_REFLECTION_WORKER env-gated daemon."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def test_start_reflection_worker_starts_by_default(monkeypatch):
    """FIX 1a (task fc2d8aeb) — DEFAULT ON. The self-sustaining loop must
    drain pending candidates without an explicit PRISM_REFLECTION_WORKER=on,
    mirroring memory_summary_worker.is_enabled(default=True)."""
    from prism_service.services import reflection_worker as rw
    monkeypatch.delenv("PRISM_REFLECTION_WORKER", raising=False)
    # Keep the daemon from looping forever.
    monkeypatch.setattr(rw, "run_once", lambda: {
        "projects": [], "ran": 0, "errors": 0, "skipped": 0,
    })
    monkeypatch.setattr(rw, "_loop", lambda: rw.run_once())
    t = rw.start_reflection_worker()
    assert t is not None, "worker must spawn by default (env unset)"
    t.join(timeout=2)


def test_start_reflection_worker_returns_none_when_off(monkeypatch):
    """FIX 1b — PRISM_REFLECTION_WORKER=off preserves the opt-out."""
    from prism_service.services.reflection_worker import start_reflection_worker
    monkeypatch.setenv("PRISM_REFLECTION_WORKER", "off")
    assert start_reflection_worker() is None


def test_start_reflection_worker_starts_thread_when_on(monkeypatch):
    """`PRISM_REFLECTION_WORKER=on` starts a daemon thread. The thread
    sleeps inside `time.sleep` after one cycle, so we monkeypatch
    sleep to a no-op-then-stop to keep the test fast."""
    from prism_service.services import reflection_worker as rw

    monkeypatch.setenv("PRISM_REFLECTION_WORKER", "on")
    # Make _loop fall through after the first run_once
    seen = {"calls": 0}

    def _fake_run_once():
        seen["calls"] += 1
        return {"projects": [], "ran": 0, "errors": 0, "skipped": 0}

    monkeypatch.setattr(rw, "run_once", _fake_run_once)
    # Replace _loop with a single-pass variant so the daemon exits cleanly
    # after one cycle (avoids leaking a thread or raising in tearDown).
    def _single_pass_loop():
        rw.run_once()
    monkeypatch.setattr(rw, "_loop", _single_pass_loop)

    t = rw.start_reflection_worker()
    assert t is not None
    t.join(timeout=2)
    assert not t.is_alive(), "worker thread should have exited after one cycle"
    assert seen["calls"] == 1


def test_run_once_skips_projects_without_scores_db(monkeypatch, tmp_path):
    """When a project's scores.db doesn't exist, the project is skipped
    without calling run_one or treating it as an error."""
    from prism_service.services import reflection_worker as rw

    monkeypatch.setenv("PRISM_REFLECTION_WORKER_PROJECT", "ghost-project")

    class _Ctx:
        _data_dir = tmp_path

    monkeypatch.setattr(rw, "_projects_in_scope", lambda: ["ghost-project"])
    with patch(
        "prism_service.project_context.get_project", return_value=_Ctx()
    ):
        summary = rw.run_once()
    assert summary["ran"] == 0
    assert summary["skipped"] == 1


def test_run_once_dispatches_run_one_for_pending(monkeypatch, tmp_path):
    """run_once: claim oldest pending candidate, call reflection_runner."""
    from prism_service.engines.brain_engine import Brain
    from prism_service.services import reflection_worker as rw
    import sqlite3

    scores = tmp_path / "scores.db"
    Brain(
        brain_db=str(tmp_path / "brain.db"),
        graph_db=str(tmp_path / "graph.db"),
        scores_db=str(scores),
    )
    c = sqlite3.connect(str(scores))
    try:
        c.execute(
            "INSERT INTO consolidation_candidates "
            "(id, task_id, session_id, trigger, status, queued_at) "
            "VALUES (?, NULL, ?, ?, 'pending', ?)",
            ("cand-1", "sess-1", "transcript_imported",
             "2026-05-25T10:00:00+00:00"),
        )
        c.commit()
    finally:
        c.close()

    class _Ctx:
        _data_dir = tmp_path

    monkeypatch.setattr(rw, "_projects_in_scope", lambda: ["p"])
    captured: dict = {}

    class _Result:
        ok = True
        error = ""
        verdict = {"qualitative_score": 0.6}
        memories_stored: list = []
        duration_s = 1.0

    def _fake_run_one(*, project, candidate_id):
        captured["project"] = project
        captured["candidate_id"] = candidate_id
        return _Result()

    with patch(
        "prism_service.project_context.get_project", return_value=_Ctx()
    ):
        monkeypatch.setattr(
            "prism_service.services.reflection_runner.run_one", _fake_run_one
        )
        summary = rw.run_once()

    assert captured["candidate_id"] == "cand-1"
    assert captured["project"] == "p"
    assert summary["ran"] == 1
    assert summary["errors"] == 0


def test_workers_endpoint_reflects_env_state(monkeypatch):
    """/api/consolidation/workers shows the reflection worker as
    running/cadence. FIX 1a — DEFAULT ON: with the env var UNSET the
    worker reports running=True; only =off flips it to running=False."""
    from prism_service.api.consolidation import workers

    monkeypatch.setenv("PRISM_REFLECTION_WORKER", "on")
    monkeypatch.setenv("PRISM_REFLECTION_WORKER_INTERVAL", "300")
    payload = workers()
    rw_entry = [w for w in payload["workers"] if w["id"] == "reflection_worker"][0]
    assert rw_entry["running"] is True
    assert rw_entry["cadence_s"] == 300

    # Env UNSET -> still running (default ON).
    monkeypatch.delenv("PRISM_REFLECTION_WORKER")
    payload = workers()
    rw_entry = [w for w in payload["workers"] if w["id"] == "reflection_worker"][0]
    assert rw_entry["running"] is True, (
        "reflection worker must default ON when the env var is unset"
    )

    # Explicit off -> not running.
    monkeypatch.setenv("PRISM_REFLECTION_WORKER", "off")
    payload = workers()
    rw_entry = [w for w in payload["workers"] if w["id"] == "reflection_worker"][0]
    assert rw_entry["running"] is False
