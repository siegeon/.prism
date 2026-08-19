"""Task-detail Trace tab endpoint (UI redesign, workstream 5).

Drives the REAL tasks router with a FastAPI TestClient over a REAL
scores.db on disk, seeding agent_runs rows the same way production's
telemetry channel does. Pins the user-facing seam end to end:

  * GET /api/tasks/{id}/trace groups the task's agent_runs by session,
    then by SDLC step, tokens on every row;
  * per-session tokens_total and the {tokens, steps, sessions} totals
    sum the seeded rows exactly;
  * steps within a session stay time-ordered (started_at ASC);
  * a task with NO runs returns empty arrays (honest empty state), not
    a 404 — the tab renders an empty trace.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _seed_schema(scores_db: str) -> None:
    """Create scores.db (incl. agent_runs) via Brain's CREATE TABLE block —
    the same path production takes."""
    from prism_service.engines.brain_engine import Brain
    Brain(
        brain_db=str(Path(scores_db).parent / "brain.db"),
        graph_db=str(Path(scores_db).parent / "graph.db"),
        scores_db=scores_db,
    )


def _run(**kw) -> dict:
    base = dict(
        run_id="run-1", workflow_name="implement", task_id="T-trace",
        session_id="S-1", agent_id="agent-a", parent_agent_id=None,
        role="dev", step="implement", model="claude-opus-4-8",
        started_at="2026-05-31T12:00:00+00:00",
        ended_at="2026-05-31T12:01:00+00:00", duration_ms=60000,
        tokens=1000, tool_uses=3, ok=True, gate_state="none",
        verdict_summary="", evidence_ref="",
    )
    base.update(kw)
    return base


def _client(tmp_path, monkeypatch):
    """Mount the REAL tasks router over a REAL scores.db in tmp. get_project
    is patched so the trace route resolves scores_db via ctx._data_dir."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from prism_service.api import tasks as tasks_api

    scores_db = str(tmp_path / "scores.db")
    _seed_schema(scores_db)

    # The trace route guards task existence via _svc(project).get(task_id)
    # (its only use of task_svc; grouping reads scores.db). These tests all
    # query tasks that DO exist (they seed that task's agent_runs), so a stub
    # that reports the task present lets the real grouping be exercised.
    class _Svc:
        def get(self, _task_id):
            return object()

    class _Ctx:
        _data_dir = tmp_path
        task_svc = _Svc()

    monkeypatch.setattr(tasks_api, "get_project", lambda p: _Ctx())

    app = FastAPI()
    app.include_router(tasks_api.router, prefix="/api/tasks")
    return TestClient(app), scores_db


def _seed(scores_db: str, rows: list[dict]) -> None:
    from prism_service.services.agent_runs_data import upsert_agent_run
    for r in rows:
        upsert_agent_run(scores_db, r)


def test_trace_groups_by_session_then_step(tmp_path, monkeypatch):
    client, scores_db = _client(tmp_path, monkeypatch)
    # Two sessions on one task: S-1 has two steps, S-2 one.
    _seed(scores_db, [
        _run(session_id="S-1", agent_id="a1", step="write_failing_tests",
             role="qa", tokens=1400, started_at="2026-05-31T12:00:00+00:00"),
        _run(session_id="S-1", agent_id="a2", step="implement",
             role="dev", tokens=6300, started_at="2026-05-31T12:05:00+00:00"),
        _run(session_id="S-2", agent_id="a3", step="verify_green",
             role="qa", tokens=900, gate_state="passed",
             started_at="2026-05-31T13:00:00+00:00"),
    ])
    got = client.get("/api/tasks/T-trace/trace", params={"project": "prism"})
    assert got.status_code == 200, got.text
    body = got.json()

    # Totals sum every row across both sessions.
    assert body["totals"] == {"tokens": 1400 + 6300 + 900, "cost_usd": 0.0,
                              "steps": 3, "sessions": 2}, body["totals"]

    sessions = {s["session_id"]: s for s in body["sessions"]}
    assert set(sessions) == {"S-1", "S-2"}
    # Per-session tokens_total.
    assert sessions["S-1"]["tokens_total"] == 1400 + 6300
    assert sessions["S-2"]["tokens_total"] == 900
    # Steps time-ordered within S-1.
    s1_steps = [st["step"] for st in sessions["S-1"]["steps"]]
    assert s1_steps == ["write_failing_tests", "implement"], s1_steps
    # Each step row carries role/model/tokens/gate_state/ts.
    first = sessions["S-1"]["steps"][0]
    for f in ("step", "role", "model", "tokens", "cost_usd", "gate_state",
              "ts"):
        assert f in first, f"step row missing {f!r}: {first}"
    assert first["role"] == "qa" and first["tokens"] == 1400
    assert sessions["S-2"]["steps"][0]["gate_state"] == "passed"


def test_trace_empty_for_task_with_no_runs(tmp_path, monkeypatch):
    """A task with no agent_runs returns empty arrays + zeroed totals, not a
    404 — the tab renders an honest empty state."""
    client, _ = _client(tmp_path, monkeypatch)
    got = client.get("/api/tasks/nobody/trace", params={"project": "prism"})
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["sessions"] == []
    assert body["totals"] == {"tokens": 0, "cost_usd": 0.0, "steps": 0,
                              "sessions": 0}


def test_trace_scoped_to_one_task(tmp_path, monkeypatch):
    """Rows from a different task never leak into this task's trace."""
    client, scores_db = _client(tmp_path, monkeypatch)
    _seed(scores_db, [
        _run(task_id="T-trace", session_id="S-1", agent_id="a1",
             step="implement", tokens=500),
        _run(task_id="OTHER", session_id="S-9", agent_id="z9",
             step="implement", tokens=99999),
    ])
    body = client.get("/api/tasks/T-trace/trace",
                      params={"project": "prism"}).json()
    assert body["totals"]["tokens"] == 500, body
    assert {s["session_id"] for s in body["sessions"]} == {"S-1"}


# ── Backfill: zero-token UUID sessions re-attribute from the transcript ─────
# (task 61261201 — _record_agent_run stamped tokens=0 when the transcript
# wasn't readable at write time; the read path repairs it honestly.)

_SID = "8750fa11-6890-4c5e-aa94-cae329e268dc"


def _write_transcript(dirpath: Path, sid: str, events: list[tuple[float, int]]) -> None:
    """Minimal transcript JSONL the production reader parses: one assistant
    turn per (epoch, tokens) with a usage dict routed through sum_usage."""
    import json
    from datetime import datetime, timezone
    lines = [
        json.dumps({
            "timestamp": datetime.fromtimestamp(ep, tz=timezone.utc).isoformat(),
            "message": {"usage": {"output_tokens": tok}},
        })
        for ep, tok in events
    ]
    # Trailing newline matters: the incremental reader only folds COMPLETE
    # lines, holding back a partial tail — a real transcript always ends \n.
    (dirpath / f"{sid}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_trace_backfills_zero_token_uuid_session(tmp_path, monkeypatch):
    """AC-1: a UUID session whose DB rows are all tokens=0 but whose
    transcript has real usage gets non-zero per-step tokens (bucketed into
    each row's time window), the session total equals their sum, the trace
    totals still equal the sum across sessions, and the repair PERSISTS —
    a second read without the transcript still shows the numbers."""
    from prism_service.services.agent_runs_data import build_task_trace
    scores_db = str(tmp_path / "scores.db")
    _seed_schema(scores_db)
    base = 1_783_400_000.0
    _seed(scores_db, [
        _run(session_id=_SID, agent_id="a1", step="implement_tasks", tokens=0,
             started_at=str(base + 0), ended_at=str(base + 100)),
        _run(session_id=_SID, agent_id="a2", step="verify_green_state", tokens=0,
             started_at=str(base + 100), ended_at=str(base + 200)),
        _run(session_id=_SID, agent_id="a3", step="green_gate", tokens=0,
             started_at=str(base + 200), ended_at=str(base + 300)),
    ])
    _write_transcript(tmp_path, _SID, [
        (base + 50, 111), (base + 150, 222), (base + 250, 333),
    ])
    body = build_task_trace(scores_db, "T-trace", override_dir=str(tmp_path))
    sess = {s["session_id"]: s for s in body["sessions"]}[_SID]
    assert [st["tokens"] for st in sess["steps"]] == [111, 222, 333], sess
    assert sess["tokens_total"] == 666
    assert body["totals"]["tokens"] == sum(
        s["tokens_total"] for s in body["sessions"])
    # Durable repair: read again with NO transcript source at all.
    (tmp_path / f"{_SID}.jsonl").unlink()
    again = build_task_trace(scores_db, "T-trace")
    sess2 = {s["session_id"]: s for s in again["sessions"]}[_SID]
    assert sess2["tokens_total"] == 666, "backfill must persist to agent_runs"


def test_trace_backfill_aligns_tz_skewed_stamps(tmp_path, monkeypatch):
    """agent_runs rows have shipped with whole-hour tz skew vs the
    transcript's UTC timestamps. The backfill finds the hour-aligned shift
    whose windows claim the events — and still only counts in-window spend
    (a 4-day transcript must never dump its lifetime total on one drive)."""
    from prism_service.services.agent_runs_data import build_task_trace
    scores_db = str(tmp_path / "scores.db")
    _seed_schema(scores_db)
    base = 1_783_400_000.0
    skew = 5 * 3600  # stamps written 5h behind the transcript's UTC epochs
    _seed(scores_db, [
        _run(session_id=_SID, agent_id="a1", step="implement_tasks", tokens=0,
             started_at=str(base + 0), ended_at=str(base + 100)),
        _run(session_id=_SID, agent_id="a2", step="green_gate", tokens=0,
             started_at=str(base + 100), ended_at=str(base + 200)),
    ])
    _write_transcript(tmp_path, _SID, [
        (base + skew + 50, 111),        # implement window (shifted)
        (base + skew + 150, 222),       # green window (shifted)
        (base + skew + 90_000, 9_999_999),  # a day later — other work, NEVER counted
    ])
    body = build_task_trace(scores_db, "T-trace", override_dir=str(tmp_path))
    sess = {s["session_id"]: s for s in body["sessions"]}[_SID]
    assert [st["tokens"] for st in sess["steps"]] == [111, 222], sess
    assert sess["tokens_total"] == 333
    assert body["totals"]["tokens"] == 333


def test_trace_never_fabricates_for_synthetic_or_absent(tmp_path, monkeypatch):
    """AC-2: synthetic/empty session ids and UUID sessions with NO transcript
    stay at 0 — nothing is invented — and totals still sum exactly."""
    from prism_service.services.agent_runs_data import build_task_trace
    scores_db = str(tmp_path / "scores.db")
    _seed_schema(scores_db)
    _seed(scores_db, [
        _run(session_id="", agent_id="m1", step="story_gate", tokens=0),
        _run(session_id="drive-rev-abc-3", agent_id="d1", step="plan_gate", tokens=0),
        _run(session_id=_SID, agent_id="a1", step="implement_tasks", tokens=0,
             started_at="1783400000.0", ended_at="1783400100.0"),
        _run(session_id="S-real", agent_id="r1", step="draft_story", tokens=400),
    ])
    body = build_task_trace(scores_db, "T-trace", override_dir=str(tmp_path))
    sess = {s["session_id"]: s for s in body["sessions"]}
    assert sess[""]["tokens_total"] == 0
    assert sess["drive-rev-abc-3"]["tokens_total"] == 0
    assert sess[_SID]["tokens_total"] == 0  # no transcript on disk → honest 0
    assert sess["S-real"]["tokens_total"] == 400
    assert body["totals"]["tokens"] == 400


def test_trace_returns_per_step_and_total_cost(tmp_path, monkeypatch):
    """Per-run dollar cost reaches the endpoint (task 9a51e670): each step row
    carries its own cost_usd and totals.cost_usd sums them, so the owner can
    read cost per step per bot off the Trace tab's own payload."""
    client, scores_db = _client(tmp_path, monkeypatch)
    _seed(scores_db, [
        _run(session_id="prism-task-runner", agent_id="a1", step="implement",
             tokens=8915, cost_usd=1.670074,
             started_at="2026-08-18T12:00:00+00:00"),
        _run(session_id="prism-task-runner", agent_id="a2", step="verify",
             tokens=4000, cost_usd=0.75,
             started_at="2026-08-18T12:05:00+00:00"),
    ])
    body = client.get("/api/tasks/T-trace/trace",
                      params={"project": "prism"}).json()

    steps = body["sessions"][0]["steps"]
    assert [s["cost_usd"] for s in steps] == [1.670074, 0.75], steps
    assert body["totals"]["cost_usd"] == pytest.approx(1.670074 + 0.75)
    assert body["totals"]["tokens"] == 8915 + 4000
