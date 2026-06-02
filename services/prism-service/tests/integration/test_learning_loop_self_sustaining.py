"""RED scaffold — learning loop self-sustaining + bounded (task fc2d8aeb).

Pins the acceptance criteria for making the learning loop drain itself on
a bounded cadence and surface its workers, end-to-end against the REAL
seams (env defaults, the transcript enqueue path, the janitor rollup
write, the adaptive-policy worker registration, and a retention sweep).

These are integration-level: they assert the user-facing seam, not a
unit echo —
  * reflection worker DEFAULTS ON (is_enabled) and spawns without an
    explicit PRISM_REFLECTION_WORKER=on, but =off still disables it;
  * the noise filter is still honored at enqueue time;
  * a one-call backlog drain exists in api/consolidation;
  * a task-linked session yields a candidate carrying that task_id;
  * a completed reflection writes a task_quality_rollup row joined with
    Layer-A and learning_data.get_learning_rows returns it;
  * an env-gated AdaptivePolicyService worker is registered in main.py
    and surfaces in /api/consolidation/workers;
  * a bounded retention sweep prunes only completed/old candidates and
    caps stale session_outcomes.

Every test FAILS today. Parent task: fc2d8aeb.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _make_scores_db(tmp_path: Path) -> str:
    """Real scores.db with the full consolidation/learning schema."""
    from prism_service.engines.brain_engine import Brain

    scores = tmp_path / "scores.db"
    Brain(
        brain_db=str(tmp_path / "brain.db"),
        graph_db=str(tmp_path / "graph.db"),
        scores_db=str(scores),
    )
    return str(scores)


# =====================================================================
# FIX 1a/1b/1c — reflection worker self-drains on a bounded cadence,
# defaults ON, honors =off, and respects the v6.2.18 noise filter.
# =====================================================================


def test_reflection_worker_is_enabled_defaults_on(monkeypatch):
    """FIX 1a — mirror memory_summary_worker.is_enabled(): default ON.

    There must be an is_enabled() that returns True with the env var
    UNSET, so start_reflection_worker spawns the thread without an
    explicit PRISM_REFLECTION_WORKER=on."""
    from prism_service.services import reflection_worker as rw

    monkeypatch.delenv("PRISM_REFLECTION_WORKER", raising=False)
    assert hasattr(rw, "is_enabled"), (
        "reflection_worker must expose is_enabled() mirroring "
        "memory_summary_worker"
    )
    assert rw.is_enabled() is True, (
        "reflection worker must DEFAULT ON (self-sustaining loop)"
    )


def test_reflection_worker_off_switch_preserved(monkeypatch):
    """FIX 1b — PRISM_REFLECTION_WORKER=off fully disables the worker."""
    from prism_service.services import reflection_worker as rw

    monkeypatch.setenv("PRISM_REFLECTION_WORKER", "off")
    assert rw.is_enabled() is False
    assert rw.start_reflection_worker() is None, (
        "=off must keep the opt-out: no thread spawned"
    )


def test_start_reflection_worker_spawns_without_explicit_on(monkeypatch):
    """FIX 1a — with the env var UNSET, the worker still starts."""
    from prism_service.services import reflection_worker as rw

    monkeypatch.delenv("PRISM_REFLECTION_WORKER", raising=False)

    # Keep the daemon from looping forever: single-pass loop + no-op cycle.
    monkeypatch.setattr(rw, "run_once", lambda: {
        "projects": [], "ran": 0, "errors": 0, "skipped": 0,
    })
    monkeypatch.setattr(rw, "_loop", lambda: rw.run_once())

    t = rw.start_reflection_worker()
    assert t is not None, (
        "default-ON: thread must spawn with PRISM_REFLECTION_WORKER unset"
    )
    t.join(timeout=2)


def test_reflection_worker_skips_noise_candidate(tmp_path, monkeypatch):
    """FIX 1c — a no-signal candidate (task_id NULL, zero signals) must
    NOT be dispatched to reflection_runner; the loop drains real signal
    only."""
    from prism_service.services import reflection_worker as rw

    scores = _make_scores_db(tmp_path)
    conn = sqlite3.connect(scores)
    try:
        conn.execute(
            "INSERT INTO consolidation_candidates "
            "(id, task_id, session_id, trigger, scope_json, status, queued_at) "
            "VALUES ('noise-1', NULL, 'sess-noise', 'transcript_imported', "
            " '{\"signal_counts\": {\"pushbacks\": 0, \"bg_signals\": 0, "
            "   \"tool_failures\": 0, \"memory_writes\": 0}}', 'pending', "
            " '2026-05-25T10:00:00+00:00')",
        )
        conn.commit()
    finally:
        conn.close()

    class _Ctx:
        _data_dir = tmp_path

    monkeypatch.setattr(rw, "_projects_in_scope", lambda: ["p"])
    dispatched: list[str] = []

    def _fake_run_one(*, project, candidate_id):
        dispatched.append(candidate_id)
        raise AssertionError("noise candidate must not be reflected")

    from unittest.mock import patch
    with patch(
        "prism_service.project_context.get_project", return_value=_Ctx()
    ):
        monkeypatch.setattr(
            "prism_service.services.reflection_runner.run_one", _fake_run_one
        )
        summary = rw.run_once()

    assert dispatched == [], (
        "noise candidate must be skipped, not dispatched for reflection"
    )
    assert summary["ran"] == 0


# =====================================================================
# FIX 1d — a one-call backlog drain exists in api/consolidation.
# =====================================================================


def test_backlog_drain_endpoint_drains_pending(tmp_path, monkeypatch):
    """FIX 1d — POST /api/consolidation/drain (or equivalent) processes
    the existing pending candidates in one call and reports how many it
    drained. Wired through the real FastAPI app + route dispatcher."""
    from fastapi.testclient import TestClient

    scores = _make_scores_db(tmp_path)
    conn = sqlite3.connect(scores)
    try:
        for i in range(3):
            conn.execute(
                "INSERT INTO consolidation_candidates "
                "(id, task_id, session_id, trigger, scope_json, status, "
                " queued_at) VALUES (?, 'T-1', ?, 'task_done', "
                " '{\"signal_counts\": {\"pushbacks\": 1}}', 'pending', ?)",
                (f"cand-{i}", f"sess-{i}", f"2026-05-2{i}T10:00:00+00:00"),
            )
        conn.commit()
    finally:
        conn.close()

    class _Ctx:
        _data_dir = Path(scores).parent

    monkeypatch.setattr(
        "prism_service.api.consolidation.get_project", lambda p: _Ctx()
    )
    # Don't actually shell out to claude — count the dispatch instead.
    drained: list[str] = []
    monkeypatch.setattr(
        "prism_service.services.reflection_worker.run_once",
        lambda: {"ran": 0, "errors": 0, "skipped": 0, "projects": []},
        raising=False,
    )

    from prism_service.main import app
    client = TestClient(app)
    resp = client.post("/api/consolidation/drain?project=p")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "drained" in body or "ran" in body, (
        "drain endpoint must report how many candidates it processed"
    )


# =====================================================================
# FIX 2a — a task-linked session produces a candidate carrying task_id.
# =====================================================================


def test_task_linked_session_candidate_carries_task_id(tmp_path):
    """FIX 2a — when scores.db task_sessions links a session to a task,
    claude_transcripts._enqueue_with_signals must stamp that task_id onto
    the consolidation_candidate (no longer NULL)."""
    from prism_service.services.claude_transcripts import _enqueue_with_signals

    scores = _make_scores_db(tmp_path)
    # Link session -> task in task_sessions (the LL association table).
    conn = sqlite3.connect(scores)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS task_sessions ("
            "task_id TEXT NOT NULL, session_id TEXT NOT NULL, "
            "started_at TEXT, ended_at TEXT, "
            "PRIMARY KEY (task_id, session_id))"
        )
        conn.execute(
            "INSERT INTO task_sessions (task_id, session_id) VALUES (?, ?)",
            ("TASK-42", "sess-linked"),
        )
        conn.commit()
    finally:
        conn.close()

    metrics = {
        "session_id": "sess-linked",
        "files_modified": 1,
        "signals": {"pushbacks": ["p"]},
    }
    _enqueue_with_signals(scores, "sess-linked", metrics,
                          "2026-05-25T10:00:00+00:00")

    conn = sqlite3.connect(scores)
    try:
        row = conn.execute(
            "SELECT task_id FROM consolidation_candidates "
            "WHERE session_id=?", ("sess-linked",),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "a candidate must be enqueued for the session"
    assert row[0] == "TASK-42", (
        f"candidate must carry the linked task_id, got {row[0]!r} (still NULL "
        f"means FIX 2a not wired)"
    )


# =====================================================================
# FIX 2b — a completed reflection writes a task_quality_rollup (Layer-B)
# joined with Layer-A, and learning_data.get_learning_rows returns it.
# =====================================================================


def test_completed_reflection_writes_rollup_and_feeds_outcome(
    tmp_path, monkeypatch
):
    """FIX 2b — a completed reflection (via reflection_runner.run_one)
    must (1) write the Layer-B task_quality_rollup joined with Layer-A so
    learning_data.get_learning_rows returns it, AND (2) feed the
    recall->outcome signal via memory_service.record_outcome so the
    adaptive policy has something to tune on. The record_outcome call is
    the wire that is MISSING today."""
    import prism_service.services.reflection_runner as rr
    from prism_service.services.learning_data import get_learning_rows

    scores = _make_scores_db(tmp_path)
    conn = sqlite3.connect(scores)
    try:
        conn.execute(
            "INSERT INTO consolidation_candidates "
            "(id, task_id, session_id, trigger, scope_json, status, queued_at) "
            "VALUES ('cand-q', 'TASK-Q', 'sess-q', 'task_done', '{}', "
            " 'pending', '2026-05-25T10:00:00+00:00')",
        )
        # Layer-A metric already present for the same task.
        conn.execute(
            "INSERT INTO task_quality_rollup (task_id, quality_score) "
            "VALUES ('TASK-Q', 0.91)",
        )
        conn.commit()
    finally:
        conn.close()

    recorded: list[tuple] = []

    class _Mem:
        def record_outcome(self, task_id, outcome):
            recorded.append((task_id, outcome))
            return 1

        def store(self, **kw):
            class _E:
                id = "e1"
                name = kw.get("name")
                domain = kw.get("domain")
            return _E()

    class _Ctx:
        _data_dir = tmp_path
        memory_svc = _Mem()

    monkeypatch.setattr(rr, "get_project", lambda p: _Ctx(), raising=False)
    monkeypatch.setattr(
        "prism_service.project_context.get_project", lambda p: _Ctx()
    )

    # Stub the claude shell-out: return a clean qualitative verdict.
    class _Res:
        run_id = "run-q"
        exit_code = 0
        duration_s = 1.0

        def final_text(self):
            return (
                '{"qualitative_score": 0.77, "narrative": "clean", '
                '"new_memories": [], "invalidate_memory_ids": [], '
                '"confidence": 0.6}'
            )

    monkeypatch.setattr(
        "prism_service.inference.claude_cli.invoke",
        lambda **kw: _Res(),
    )
    monkeypatch.setattr(
        "prism_service.services.source_service.source_dir_for",
        lambda p: str(tmp_path), raising=False,
    )

    res = rr.run_one(project="p", candidate_id="cand-q")
    assert res.ok, res.to_dict()

    # (1) Layer-B rollup row joined with Layer-A, surfaced via learning_data.
    rows = get_learning_rows(scores)
    by_task = {r["task_id"]: r for r in rows}
    assert "TASK-Q" in by_task, (
        "get_learning_rows must surface the task-linked rollup row"
    )
    row = by_task["TASK-Q"]
    assert abs(row["qualitative_score"] - 0.77) < 1e-9, (
        "Layer-B qualitative_score must be written onto the rollup"
    )
    assert row["quality_score"] is not None and row["quality_score"] >= 0.9, (
        "Layer-B row must join the existing Layer-A quality_score"
    )

    # (2) The recall->outcome signal must be fed for the task.
    assert any(t == "TASK-Q" for (t, _o) in recorded), (
        "completed reflection must call memory_service.record_outcome for "
        f"the task; recorded={recorded!r}"
    )


# =====================================================================
# FIX 3a — AdaptivePolicyService is an env-gated worker registered in
# main.py lifespan and a tick persists >=1 policy_knobs row.
# =====================================================================


def test_adaptive_policy_worker_is_registered_in_lifespan():
    """FIX 3a + Phase 4 (epic 4fd1e6b4) — the adaptive-policy retune is no
    longer its OWN lifespan thread; it is folded into the single maintenance
    clock as the 'adaptive' pass (still env-gated by
    PRISM_ADAPTIVE_POLICY_WORKER). The lifespan now wires the consolidated
    clock instead of start_adaptive_policy_worker()."""
    main_src = (_SERVICE_ROOT / "prism_service" / "main.py").read_text(
        encoding="utf-8"
    )
    assert "start_adaptive_policy_worker()" not in main_src, (
        "start_adaptive_policy_worker() must no longer be spawned from the "
        "lifespan (folded into the maintenance clock)"
    )
    assert "start_maintenance_clock" in main_src, (
        "main.py lifespan must wire start_maintenance_clock (the consolidated "
        "home of the adaptive retune pass)"
    )
    # The env gate that governs the adaptive pass remains honored — now via
    # maintenance_clock.pass_enabled()/pass_cadences().
    from prism_service.services import maintenance_clock as mc
    assert "adaptive" in mc.PASS_ORDER
    assert "PRISM_ADAPTIVE_POLICY_WORKER_INTERVAL" in (
        (_SERVICE_ROOT / "prism_service" / "services" / "maintenance_clock.py")
        .read_text(encoding="utf-8")
    ), "the maintenance clock must honor PRISM_ADAPTIVE_POLICY_WORKER_INTERVAL"


def test_adaptive_policy_worker_persists_knob_row(tmp_path, monkeypatch):
    """FIX 3a — one worker tick nudges + persists at least one
    policy_knobs row from the recall->outcome signal."""
    from prism_service.services import adaptive_policy as ap

    scores = _make_scores_db(tmp_path)

    class _Ctx:
        _data_dir = tmp_path
        memory_svc = None

    monkeypatch.setattr(
        "prism_service.project_context.get_project", lambda p: _Ctx()
    )
    monkeypatch.setattr(
        "prism_service.project_context.get_all_projects", lambda: ["p"]
    )

    assert hasattr(ap, "run_once") or hasattr(ap, "tick"), (
        "adaptive_policy must expose a worker entrypoint (run_once/tick) "
        "the lifespan thread can call"
    )
    runner = getattr(ap, "run_once", None) or getattr(ap, "tick", None)
    runner()

    conn = sqlite3.connect(scores)
    try:
        n = conn.execute("SELECT COUNT(*) FROM policy_knobs").fetchone()[0]
    finally:
        conn.close()
    assert n >= 1, (
        "an adaptive-policy worker tick must persist >=1 policy_knobs row"
    )


# =====================================================================
# FIX 3b — the adaptive-policy worker appears in /api/consolidation/workers.
# =====================================================================


def test_adaptive_policy_worker_in_workers_endpoint(monkeypatch):
    """FIX 3b + Phase 4 — the adaptive-policy retune duty now surfaces on
    /api/consolidation/workers as part of the consolidated maintenance_clock
    entry (the separate adaptive_policy_worker row was folded). The clock entry
    must name the 'adaptive' pass so /settings/activity + /learning panels can
    still tell the operator it is running."""
    from prism_service.api.consolidation import workers

    monkeypatch.setenv("PRISM_ADAPTIVE_POLICY_WORKER", "on")
    payload = workers()
    by_id = {w["id"]: w for w in payload["workers"]}
    assert "maintenance_clock" in by_id, (
        f"the consolidated maintenance_clock must appear in "
        f"/api/consolidation/workers; got {sorted(by_id)}"
    )
    assert "adaptive_policy_worker" not in by_id, (
        "the separate adaptive_policy_worker row must be folded into "
        "maintenance_clock"
    )
    assert "adaptive" in by_id["maintenance_clock"]["description"].lower(), (
        "the maintenance_clock description must name the adaptive retune pass"
    )


# =====================================================================
# FIX 4a — bounded, idempotent retention sweep prunes only intended rows.
# =====================================================================


def test_retention_sweep_prunes_only_intended_rows(tmp_path):
    """FIX 4a — a retention sweep prunes completed + old candidates and
    caps/archives stale session_outcomes, deleting ONLY the intended rows
    (recent + pending survive) and reporting what it pruned. Idempotent:
    a second call deletes nothing more."""
    from prism_service.services import consolidation_data as cd

    scores = _make_scores_db(tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
    recent = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(scores)
    try:
        # Old completed -> should be pruned.
        conn.execute(
            "INSERT INTO consolidation_candidates "
            "(id, task_id, status, queued_at, completed_at) "
            "VALUES ('old-done', 'T', 'completed', ?, ?)", (old, old),
        )
        # Recent completed -> must SURVIVE.
        conn.execute(
            "INSERT INTO consolidation_candidates "
            "(id, task_id, status, queued_at, completed_at) "
            "VALUES ('new-done', 'T', 'completed', ?, ?)", (recent, recent),
        )
        # Pending -> must SURVIVE regardless of age.
        conn.execute(
            "INSERT INTO consolidation_candidates "
            "(id, task_id, status, queued_at) "
            "VALUES ('old-pending', 'T', 'pending', ?)", (old,),
        )
        conn.commit()
    finally:
        conn.close()

    assert hasattr(cd, "retention_sweep"), (
        "consolidation_data must expose a bounded retention_sweep()"
    )
    report = cd.retention_sweep(scores)
    assert isinstance(report, dict), "retention_sweep must report what it pruned"

    conn = sqlite3.connect(scores)
    try:
        survivors = {
            r[0] for r in conn.execute(
                "SELECT id FROM consolidation_candidates"
            ).fetchall()
        }
    finally:
        conn.close()
    assert "old-done" not in survivors, "old completed candidate must be pruned"
    assert "new-done" in survivors, "recent completed candidate must survive"
    assert "old-pending" in survivors, "pending candidate must never be pruned"

    # Idempotent: nothing left to prune on the second pass.
    report2 = cd.retention_sweep(scores)
    pruned2 = sum(
        v for v in report2.values() if isinstance(v, int)
    )
    assert pruned2 == 0, (
        f"retention_sweep must be idempotent; second pass pruned {pruned2}"
    )
