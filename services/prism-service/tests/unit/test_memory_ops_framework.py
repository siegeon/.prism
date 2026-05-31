"""Red scaffold (Tier 2) — memory-operation runner framework.

Pins the acceptance criteria for the new
`prism_service.services.memory_ops` chassis that generalises the
single-purpose reflection runner into a typed, pluggable family of
scheduled memory-operation runners.

These tests assert the REAL seams, not just unit contracts:

  * the `MemoryOperation` ABC refuses direct instantiation (a typed
    contract, not a duck-typed dict);
  * `run_one(op, item, project)` drives the SHARED inference + audit +
    idempotency path and writes a `consolidation_runs` row whose
    `op_type` column is populated — read back through a SEPARATE
    sqlite connection so we prove durability, not an in-memory echo;
  * `op.model` is threaded through to `claude_cli.invoke(model=...)`
    (model routing, no hard-coding in the runner);
  * a second `run_one` on the same item is a no-op (idempotency);
  * the generalised worker (`memory_ops_worker.start_memory_ops_workers`)
    is env-gated AND actually wired into `main.py` lifespan startup —
    a defined-but-uncalled worker is dead code, so we assert the call
    site exists, not just the function.

Every test here FAILS today: the `memory_ops` package does not exist,
`consolidation_runs` has no `op_type` column, and `JanitorService.submit`
takes no `op_type` kwarg.
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
    """Build a real scores.db with the consolidation schema (same path the
    reflection_worker test uses — Brain() init creates the tables)."""
    from prism_service.engines.brain_engine import Brain

    scores = tmp_path / "scores.db"
    Brain(
        brain_db=str(tmp_path / "brain.db"),
        graph_db=str(tmp_path / "graph.db"),
        scores_db=str(scores),
    )
    return str(scores)


def _insert_candidate(scores_db: str, cid: str) -> None:
    """Insert a pending consolidation_candidate so JanitorService.submit
    (which the shared runner calls) can resolve the candidate row."""
    c = sqlite3.connect(scores_db)
    try:
        c.execute(
            "INSERT INTO consolidation_candidates "
            "(id, task_id, session_id, trigger, status, queued_at, scope_json) "
            "VALUES (?, NULL, ?, ?, 'pending', ?, '{}')",
            (cid, "sess-1", "test", "2026-05-25T10:00:00+00:00"),
        )
        c.commit()
    finally:
        c.close()


class _FakeCliResult:
    """Mimics inference.claude_cli.ClaudeCliResult enough for the runner."""

    def __init__(self, text: str, run_id: str = "run-1"):
        self._text = text
        self.exit_code = 0
        self.run_id = run_id
        self.duration_s = 0.1

    def final_text(self) -> str:
        return self._text


def _patch_project(monkeypatch, scores_db: str, tmp_path: Path):
    """Point get_project + source_dir at the temp scores.db / dir so the
    shared runner runs fully offline."""
    class _Ctx:
        _data_dir = Path(scores_db).parent

        class memory_svc:  # noqa: N801 — attribute namespace, not a class use
            @staticmethod
            def store(**kw):
                class _E:
                    id = "mem-1"
                    name = kw.get("name", "")
                    domain = kw.get("domain", "")
                return _E()

    monkeypatch.setattr(
        "prism_service.project_context.get_project", lambda p: _Ctx()
    )
    monkeypatch.setattr(
        "prism_service.services.source_service.source_dir_for",
        lambda p: tmp_path, raising=False,
    )
    return _Ctx


def _concrete_op_cls():
    """A minimal concrete MemoryOperation subclass for runner tests. Imported
    lazily so the module import error (package missing) surfaces inside each
    test as a clean failure rather than at collection time."""
    from prism_service.services.memory_ops.base import MemoryOperation

    class _StubOp(MemoryOperation):
        op_type = "forget"
        schema = {"confidence": "float", "decision": "str"}
        model = "claude-haiku-test"
        provenance = {"source": "stub"}

        def __init__(self, items=None):
            self._items = items if items is not None else ["item-1"]
            self.applied = []

        def select(self, project):
            return list(self._items)

        def build_prompt(self, item, project):
            return f"forget? item={item} project={project}"

        def apply_verdict(self, item, verdict, project):
            self.applied.append((item, verdict))

    return _StubOp


_VALID_VERDICT_JSON = (
    '{"qualitative_score": 0.5, "narrative": "n", "new_memories": [], '
    '"invalidate_memory_ids": [], "confidence": 0.7, "decision": "forget"}'
)


# ---------------------------------------------------------------------------
# (a) ABC cannot be instantiated directly
# ---------------------------------------------------------------------------

def test_memory_operation_abc_is_not_instantiable():
    from prism_service.services.memory_ops.base import MemoryOperation

    with pytest.raises(TypeError):
        MemoryOperation()  # type: ignore[abstract]


def test_memory_operation_requires_abstract_methods():
    """A subclass missing an abstract method is still abstract."""
    from prism_service.services.memory_ops.base import MemoryOperation

    class _Incomplete(MemoryOperation):
        op_type = "x"
        schema = {}
        model = ""
        provenance = {}
        # deliberately omit select/build_prompt/apply_verdict

    with pytest.raises(TypeError):
        _Incomplete()  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# (b) run_one writes a consolidation_runs row with the correct op_type
# ---------------------------------------------------------------------------

def test_run_one_writes_consolidation_run_with_op_type(monkeypatch, tmp_path):
    from prism_service.services.memory_ops import runner

    scores_db = _make_scores_db(tmp_path)
    _insert_candidate(scores_db, "item-1")
    _patch_project(monkeypatch, scores_db, tmp_path)

    monkeypatch.setattr(
        "prism_service.inference.claude_cli.invoke",
        lambda **kw: _FakeCliResult(_VALID_VERDICT_JSON),
    )

    op = _concrete_op_cls()(items=["item-1"])
    result = runner.run_one(op, "item-1", "p")
    assert result.ok, getattr(result, "error", result)

    # Read back through a SEPARATE connection — prove durability, not echo.
    conn = sqlite3.connect(scores_db)
    try:
        row = conn.execute(
            "SELECT op_type, output_json, confidence FROM consolidation_runs "
            "WHERE candidate_id = ?",
            ("item-1",),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "no consolidation_runs row was written"
    assert row[0] == "forget", f"op_type column not populated: {row[0]!r}"
    assert '"decision"' in (row[1] or ""), "raw verdict JSON not persisted"
    assert abs((row[2] or 0.0) - 0.7) < 1e-6, "confidence not taken from verdict"


# ---------------------------------------------------------------------------
# (c) run_one skips when select() returns empty
# ---------------------------------------------------------------------------

def test_run_one_skips_when_select_empty(monkeypatch, tmp_path):
    from prism_service.services.memory_ops import runner

    scores_db = _make_scores_db(tmp_path)
    _patch_project(monkeypatch, scores_db, tmp_path)

    invoked = {"n": 0}

    def _no_invoke(**kw):
        invoked["n"] += 1
        return _FakeCliResult(_VALID_VERDICT_JSON)

    monkeypatch.setattr("prism_service.inference.claude_cli.invoke", _no_invoke)

    op = _concrete_op_cls()(items=[])  # select() returns []
    result = runner.run_one(op, None, "p")

    assert result.ok is False or getattr(result, "skipped", False), (
        "empty select() must short-circuit to a skip, not run inference"
    )
    assert invoked["n"] == 0, "claude_cli.invoke must NOT run when nothing selected"

    conn = sqlite3.connect(scores_db)
    try:
        n = conn.execute("SELECT COUNT(*) FROM consolidation_runs").fetchone()[0]
    finally:
        conn.close()
    assert n == 0, "a skipped op must not write a consolidation_runs row"


# ---------------------------------------------------------------------------
# (d) model routing: op.model is threaded through to claude_cli.invoke
# ---------------------------------------------------------------------------

def test_run_one_routes_op_model_to_claude_cli(monkeypatch, tmp_path):
    from prism_service.services.memory_ops import runner

    scores_db = _make_scores_db(tmp_path)
    _insert_candidate(scores_db, "item-1")
    _patch_project(monkeypatch, scores_db, tmp_path)

    seen = {}

    def _capture_invoke(**kw):
        seen.update(kw)
        return _FakeCliResult(_VALID_VERDICT_JSON)

    monkeypatch.setattr(
        "prism_service.inference.claude_cli.invoke", _capture_invoke
    )

    op = _concrete_op_cls()(items=["item-1"])  # op.model == "claude-haiku-test"
    runner.run_one(op, "item-1", "p")

    assert seen.get("model") == "claude-haiku-test", (
        f"op.model not routed to claude_cli.invoke(model=...): {seen.get('model')!r}"
    )


# ---------------------------------------------------------------------------
# (e) idempotency: a second run_one on the same item is a no-op
# ---------------------------------------------------------------------------

def test_run_one_second_call_same_item_is_noop(monkeypatch, tmp_path):
    from prism_service.services.memory_ops import runner

    scores_db = _make_scores_db(tmp_path)
    _insert_candidate(scores_db, "item-1")
    _patch_project(monkeypatch, scores_db, tmp_path)

    calls = {"n": 0}

    def _count_invoke(**kw):
        calls["n"] += 1
        return _FakeCliResult(_VALID_VERDICT_JSON)

    monkeypatch.setattr("prism_service.inference.claude_cli.invoke", _count_invoke)

    op = _concrete_op_cls()(items=["item-1"])
    first = runner.run_one(op, "item-1", "p")
    assert first.ok, getattr(first, "error", first)
    assert calls["n"] == 1

    # Second tick on the SAME item: the candidate is already 'completed',
    # so the runner must skip — no second inference, no duplicate audit row.
    second = runner.run_one(op, "item-1", "p")
    assert calls["n"] == 1, "second run_one re-invoked inference (not idempotent)"
    assert second.ok is False or getattr(second, "skipped", False)

    conn = sqlite3.connect(scores_db)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM consolidation_runs WHERE candidate_id = ?",
            ("item-1",),
        ).fetchone()[0]
    finally:
        conn.close()
    assert n == 1, f"idempotency broken: {n} audit rows for one item"


# ---------------------------------------------------------------------------
# JanitorService.submit accepts op_type and persists it on the row
# ---------------------------------------------------------------------------

def test_janitor_submit_persists_op_type(tmp_path):
    from prism_service.services.janitor_service import JanitorService

    scores_db = _make_scores_db(tmp_path)
    _insert_candidate(scores_db, "cand-op")

    js = JanitorService(scores_db)
    verdict = {
        "qualitative_score": 0.5, "narrative": "n", "new_memories": [],
        "invalidate_memory_ids": [], "confidence": 0.4,
    }
    res = js.submit("cand-op", output_json=verdict, op_type="prune")
    assert res.get("accepted"), res

    conn = sqlite3.connect(scores_db)
    try:
        row = conn.execute(
            "SELECT op_type FROM consolidation_runs WHERE candidate_id = ?",
            ("cand-op",),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None and row[0] == "prune", (
        f"op_type kwarg not written to consolidation_runs: {row}"
    )


def test_consolidation_runs_has_op_type_column(tmp_path):
    """Schema-level: the migration must add op_type to consolidation_runs."""
    scores_db = _make_scores_db(tmp_path)
    conn = sqlite3.connect(scores_db)
    try:
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(consolidation_runs)"
        ).fetchall()}
    finally:
        conn.close()
    assert "op_type" in cols, f"consolidation_runs missing op_type column: {cols}"


# ---------------------------------------------------------------------------
# Worker: env-gated registration AND wired into main.py lifespan (no dead code)
# ---------------------------------------------------------------------------

def test_memory_ops_workers_disabled_by_default(monkeypatch):
    from prism_service.services import memory_ops_worker as mow

    # No PRISM_<OP>_WORKER env vars set -> nothing started.
    for k in list(__import__("os").environ):
        if k.startswith("PRISM_") and k.endswith("_WORKER"):
            monkeypatch.delenv(k, raising=False)
    started = mow.start_memory_ops_workers()
    assert started == [] or started is None, (
        f"ops workers must be off by default, got {started!r}"
    )


def test_memory_ops_worker_env_gate_starts_thread(monkeypatch):
    """PRISM_<OP_TYPE>_WORKER=on starts a daemon thread for that op."""
    from prism_service.services import memory_ops_worker as mow

    monkeypatch.setenv("PRISM_FORGET_WORKER", "on")
    # keep the loop from actually sleeping/running forever
    monkeypatch.setattr(mow, "_loop_for", lambda op: None, raising=False)
    started = mow.start_memory_ops_workers()
    assert started, "PRISM_FORGET_WORKER=on must start at least one worker"


def test_memory_ops_worker_registered_in_main_lifespan():
    """A worker defined but never called from main.py is DEAD. Assert the
    lifespan startup actually wires start_memory_ops_workers()."""
    main_py = (
        _SERVICE_ROOT / "prism_service" / "main.py"
    ).read_text(encoding="utf-8")
    assert "start_memory_ops_workers" in main_py, (
        "main.py lifespan must call start_memory_ops_workers() "
        "(alongside start_reflection_worker / start_memory_summary_worker)"
    )
