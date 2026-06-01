"""Red scaffold (Tier 2) — distill-procedural memory-op runner.

Pins the REAL seams for the first concrete MemoryOperation to land on the
memory_ops chassis: it distills recurring tool/skill sequences from session
history into ``memory_type='procedural'`` entries indexed into Brain.

Acceptance criteria pinned here (integration-level, real sqlite):

  * ``DistillProceduralOperation`` IS-A ``MemoryOperation`` (typed contract,
    not a duck-typed dict) and carries ``op_type='distill_procedural'``;
  * ``select(project)`` CLUSTERS sessions that share skills/tasks by reading
    the real ``scores.db`` ``skill_usage`` + ``task_sessions`` tables, and
    returns ``consolidation_candidates`` row ids (the id the shared runner
    looks up) — proven by reading the enqueued candidate back through a
    SEPARATE connection;
  * ``run_one(op, item, project)`` drives the SHARED path and writes a
    ``consolidation_runs`` row whose ``op_type`` column == 'distill_procedural'
    (read back through a separate connection — durability, not echo);
  * ``apply_verdict`` MINTS a ``memory_type='procedural'`` ExpertiseEntry via
    ``memory_svc.store`` (so it indexes into Brain like any memory);
  * the worker registers the op behind ``PRISM_DISTILL_PROCEDURAL_WORKER``.

Every test FAILS today: ``memory_ops/distill_procedural.py`` does not exist
and ``registered_ops()`` returns [].
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _make_scores_db(tmp_path: Path) -> str:
    """Build a real scores.db with the full consolidation + session schema
    (Brain() init creates skill_usage / task_sessions / session_outcomes /
    consolidation_candidates / consolidation_runs)."""
    from prism_service.engines.brain_engine import Brain

    scores = tmp_path / "scores.db"
    Brain(
        brain_db=str(tmp_path / "brain.db"),
        graph_db=str(tmp_path / "graph.db"),
        scores_db=str(scores),
    )
    return str(scores)


def _seed_clustered_sessions(scores_db: str) -> None:
    """Two sessions that SHARE the 'implement' skill (a recurring sequence
    worth distilling) plus a task linking them — the clustering signal."""
    c = sqlite3.connect(scores_db)
    try:
        for sid in ("sess-a", "sess-b"):
            c.execute(
                "INSERT INTO session_outcomes "
                "(session_id, files_modified, skills_invoked, timestamp) "
                "VALUES (?, 1, 2, '2026-05-25 10:00:00')",
                (sid,),
            )
            for skill in ("implement", "verify"):
                c.execute(
                    "INSERT INTO skill_usage (session_id, skill_name, timestamp) "
                    "VALUES (?, ?, '2026-05-25 10:00:00')",
                    (sid, skill),
                )
        c.execute(
            "INSERT INTO task_sessions (task_id, session_id) VALUES (?, ?)",
            ("task-1", "sess-a"),
        )
        c.execute(
            "INSERT INTO task_sessions (task_id, session_id) VALUES (?, ?)",
            ("task-1", "sess-b"),
        )
        c.commit()
    finally:
        c.close()


class _FakeCliResult:
    """Mimics inference.claude_cli.ClaudeCliResult enough for the runner."""

    def __init__(self, text: str, run_id: str = "run-dp"):
        self._text = text
        self.exit_code = 0
        self.run_id = run_id
        self.duration_s = 0.1

    def final_text(self) -> str:
        return self._text


class _RecordingMemSvc:
    """Captures store(**kw) calls so a test can assert memory_type='procedural'
    actually reaches the memory service."""

    def __init__(self):
        self.stored: list[dict] = []

    def store(self, **kw):
        self.stored.append(kw)

        class _E:
            id = "mx-proc1"
            name = kw.get("name", "")
            domain = kw.get("domain", "")

        return _E()


def _patch_project(monkeypatch, scores_db: str, tmp_path: Path, mem_svc):
    """Point get_project + source_dir at the temp scores.db / dir so the
    shared runner + op run fully offline."""
    class _Ctx:
        _data_dir = Path(scores_db).parent
        memory_svc = mem_svc

    monkeypatch.setattr(
        "prism_service.project_context.get_project", lambda p: _Ctx()
    )
    monkeypatch.setattr(
        "prism_service.services.source_service.source_dir_for",
        lambda p: tmp_path, raising=False,
    )
    return _Ctx


# A verdict that satisfies JanitorService._RESPONSE_SCHEMA AND proposes one
# procedural memory the op should mint.
_PROCEDURAL_VERDICT = (
    '{"qualitative_score": 0.6, "narrative": "recurring implement->verify",'
    ' "new_memories": [{"domain": "procedures", "name": "implement-then-verify",'
    ' "description": "When touching sessions.py, run the transcript-import test.",'
    ' "type": "pattern", "classification": "tactical"}],'
    ' "invalidate_memory_ids": [], "confidence": 0.5}'
)


# ---------------------------------------------------------------------------
# (a) the op IS-A MemoryOperation with the right op_type
# ---------------------------------------------------------------------------

def test_distill_procedural_is_a_memory_operation():
    from prism_service.services.memory_ops.base import MemoryOperation
    from prism_service.services.memory_ops.distill_procedural import (
        DistillProceduralOperation,
    )

    op = DistillProceduralOperation()
    assert isinstance(op, MemoryOperation)
    assert op.op_type == "distill_procedural"


# ---------------------------------------------------------------------------
# (b) select() clusters sessions from skill_usage + task_sessions and
#     returns a consolidation_candidates row id
# ---------------------------------------------------------------------------

def test_select_clusters_sessions_into_candidate(monkeypatch, tmp_path):
    from prism_service.services.memory_ops.distill_procedural import (
        DistillProceduralOperation,
    )

    scores_db = _make_scores_db(tmp_path)
    _seed_clustered_sessions(scores_db)
    _patch_project(monkeypatch, scores_db, tmp_path, _RecordingMemSvc())

    op = DistillProceduralOperation()
    items = op.select("p")
    assert items, "select() must cluster the two shared-skill sessions"

    cid = items[0]
    # Read the enqueued candidate back through a SEPARATE connection.
    conn = sqlite3.connect(scores_db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT id, status, scope_json FROM consolidation_candidates "
            "WHERE id = ?",
            (cid,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "select() must enqueue a real consolidation_candidate"
    assert row["status"] == "pending"
    # The cluster scope must reference the member sessions it grouped.
    assert "sess-a" in (row["scope_json"] or "")
    assert "sess-b" in (row["scope_json"] or "")


def test_select_empty_when_no_shared_skills(monkeypatch, tmp_path):
    from prism_service.services.memory_ops.distill_procedural import (
        DistillProceduralOperation,
    )

    scores_db = _make_scores_db(tmp_path)  # no skill_usage rows at all
    _patch_project(monkeypatch, scores_db, tmp_path, _RecordingMemSvc())

    op = DistillProceduralOperation()
    assert op.select("p") == [], "no clusters => empty select (no inference)"


# ---------------------------------------------------------------------------
# (c) run_one drives the SHARED path: writes consolidation_runs op_type AND
#     mints a memory_type='procedural' entry
# ---------------------------------------------------------------------------

def test_run_one_writes_op_type_and_mints_procedural(monkeypatch, tmp_path):
    from prism_service.services.memory_ops import runner
    from prism_service.services.memory_ops.distill_procedural import (
        DistillProceduralOperation,
    )

    scores_db = _make_scores_db(tmp_path)
    _seed_clustered_sessions(scores_db)
    mem = _RecordingMemSvc()
    _patch_project(monkeypatch, scores_db, tmp_path, mem)

    monkeypatch.setattr(
        "prism_service.inference.claude_cli.invoke",
        lambda **kw: _FakeCliResult(_PROCEDURAL_VERDICT),
    )

    op = DistillProceduralOperation()
    item = op.select("p")[0]
    result = runner.run_one(op, item, "p")
    assert result.ok, getattr(result, "error", result)

    # consolidation_runs row carries the op family — separate connection.
    conn = sqlite3.connect(scores_db)
    try:
        row = conn.execute(
            "SELECT op_type FROM consolidation_runs WHERE candidate_id = ?",
            (item,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None and row[0] == "distill_procedural", (
        f"op_type column not populated: {row}"
    )

    # apply_verdict minted a procedural memory via memory_svc.store.
    assert mem.stored, "apply_verdict must mint at least one memory"
    minted = mem.stored[0]
    assert minted.get("memory_type") == "procedural", (
        f"minted memory must be procedural, got {minted.get('memory_type')!r}"
    )
    assert minted.get("name") == "implement-then-verify"


# ---------------------------------------------------------------------------
# (d) build_prompt asks the distillation question over the cluster signals
# ---------------------------------------------------------------------------

def test_build_prompt_asks_for_reusable_procedure(monkeypatch, tmp_path):
    from prism_service.services.memory_ops.distill_procedural import (
        DistillProceduralOperation,
    )

    scores_db = _make_scores_db(tmp_path)
    _seed_clustered_sessions(scores_db)
    _patch_project(monkeypatch, scores_db, tmp_path, _RecordingMemSvc())

    op = DistillProceduralOperation()
    item = op.select("p")[0]
    prompt = op.build_prompt(item, "p")
    low = prompt.lower()
    assert "procedure" in low, "prompt must ask about a reusable procedure"
    # the recurring skills from the cluster should surface in the prompt
    assert "implement" in low


# ---------------------------------------------------------------------------
# (e) the worker registers the op behind PRISM_DISTILL_PROCEDURAL_WORKER
# ---------------------------------------------------------------------------

def test_op_registered_in_worker():
    from prism_service.services import memory_ops_worker as mow
    from prism_service.services.memory_ops.distill_procedural import (
        DistillProceduralOperation,
    )

    ops = mow.registered_ops()
    assert any(isinstance(o, DistillProceduralOperation) for o in ops), (
        "registered_ops() must include DistillProceduralOperation so the "
        "PRISM_DISTILL_PROCEDURAL_WORKER gate has a real op to schedule"
    )


def test_distill_procedural_worker_env_gate_starts_thread(monkeypatch):
    from prism_service.services import memory_ops_worker as mow

    monkeypatch.setenv("PRISM_DISTILL_PROCEDURAL_WORKER", "on")
    monkeypatch.setattr(mow, "_loop_for", lambda op: None, raising=False)
    started = mow.start_memory_ops_workers()
    assert started, (
        "PRISM_DISTILL_PROCEDURAL_WORKER=on must start the distill worker"
    )
