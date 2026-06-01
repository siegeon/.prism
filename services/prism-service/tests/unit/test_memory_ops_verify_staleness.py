"""Red scaffold (Tier 2) — verify-staleness memory-operation runner.

Pins the acceptance criteria for ``VerifyStalenessOperation`` — the runner
that solves the named #1 production gap (mem0 2026): a high-relevance memory
that is confidently WRONG after the code it describes was refactored.

These tests assert the REAL seams, not just unit contracts:

  * the op is a genuine ``MemoryOperation`` subclass with
    ``op_type == 'verify_staleness'`` (typed contract, ABC-enforced);
  * ``select(project)`` flags ONLY a memory whose evidence cites a source
    file whose brain.db ``docs`` row was re-indexed (content_hash drifted)
    AFTER the memory's ``valid_at`` — a fresh memory over unchanged code is
    NOT flagged;
  * ``select`` enqueues a real ``consolidation_candidates`` row (so the
    shared ``runner.run_one`` can resolve + audit it) carrying the
    memory_id in scope_json;
  * ``run_one`` drives the shared inference+audit path and writes a
    ``consolidation_runs`` row whose ``op_type`` column == 'verify_staleness'
    (read back through a SEPARATE connection — durability, not echo);
  * ``apply_verdict`` honours the verdict: decision='needs_review' flips the
    memory status to 'needs_review'; decision='supersede' mints a corrected
    memory (new generation) and archives the stale one;
  * the worker registers the op behind ``PRISM_VERIFY_STALENESS_WORKER``.

Every test here FAILS today: ``verify_staleness.py`` does not exist and the
worker's ``registered_ops()`` is empty.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


# ---------------------------------------------------------------------------
# Fixtures / helpers — a real brain.db + scores.db + mulch dir, offline.
# ---------------------------------------------------------------------------

def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _build_dbs(tmp_path: Path):
    """Build a real brain.db (with a docs row) + scores.db via Brain init."""
    from prism_service.engines.brain_engine import Brain

    brain_db = str(tmp_path / "brain.db")
    Brain(
        brain_db=brain_db,
        graph_db=str(tmp_path / "graph.db"),
        scores_db=str(tmp_path / "scores.db"),
    )
    return brain_db


def _insert_doc(brain_db: str, source_file: str, indexed_at: str) -> None:
    """Insert a docs row whose indexed_at marks the last content_hash write."""
    from prism_service.engines.brain_engine import _expand_identifiers

    c = sqlite3.connect(brain_db)
    c.create_function("expand_identifiers", 1, _expand_identifiers)
    try:
        c.execute(
            "INSERT OR REPLACE INTO docs "
            "(id, source_file, content, domain, content_hash, indexed_at) "
            "VALUES (?, ?, ?, 'py', ?, ?)",
            (source_file, source_file, "def f(): return 2  # was 1",
             "hash-new", indexed_at),
        )
        c.commit()
    finally:
        c.close()


class _MemSvcStub:
    """Captures apply_verdict side-effects without a full MemoryService."""

    def __init__(self, entries):
        self._entries = {e["id"]: dict(e) for e in entries}
        self.updated = {}
        self.stored = []

    def list_domains(self):
        return sorted({e["domain"] for e in self._entries.values()})

    def _read_entries(self, domain):
        from prism_service.models.memory import ExpertiseEntry
        return [
            ExpertiseEntry(**{k: v for k, v in e.items()})
            for e in self._entries.values() if e["domain"] == domain
        ]

    def get_entry(self, entry_id):
        from prism_service.models.memory import ExpertiseEntry
        e = self._entries.get(entry_id)
        return ExpertiseEntry(**e) if e else None

    def update_entry(self, entry_id, **kw):
        self.updated[entry_id] = kw
        self._entries[entry_id].update(kw)
        return self.get_entry(entry_id)

    def store(self, **kw):
        self.stored.append(kw)
        from prism_service.models.memory import ExpertiseEntry
        return ExpertiseEntry(id="mem-new", **{
            k: kw[k] for k in (
                "domain", "name", "description", "type", "classification")
            if k in kw})


def _patch_ctx(monkeypatch, tmp_path, brain_db, mem_svc):
    """Point get_project at the temp data dir + stub memory_svc."""
    class _Ctx:
        _data_dir = tmp_path
        memory_svc = mem_svc

    monkeypatch.setattr(
        "prism_service.project_context.get_project", lambda p: _Ctx(),
    )
    monkeypatch.setattr(
        "prism_service.services.source_service.source_dir_for",
        lambda p: tmp_path, raising=False,
    )
    return _Ctx


def _mk_op():
    from prism_service.services.memory_ops.verify_staleness import (
        VerifyStalenessOperation,
    )
    return VerifyStalenessOperation()


# ---------------------------------------------------------------------------
# (a) the op is a typed MemoryOperation with the right op_type
# ---------------------------------------------------------------------------

def test_verify_staleness_is_memory_operation():
    from prism_service.services.memory_ops.base import MemoryOperation

    op = _mk_op()
    assert isinstance(op, MemoryOperation)
    assert op.op_type == "verify_staleness"


# ---------------------------------------------------------------------------
# (b) select flags ONLY a memory whose evidence file drifted past valid_at
# ---------------------------------------------------------------------------

def test_select_flags_only_drifted_memory(monkeypatch, tmp_path):
    brain_db = _build_dbs(tmp_path)
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=10)

    # Stale memory: valid_at OLD, evidence cites a file re-indexed RECENTLY.
    # Fresh memory: valid_at NOW, evidence cites a file re-indexed long ago.
    _insert_doc(brain_db, "svc/changed.py", _iso(now))           # drifted
    _insert_doc(brain_db, "svc/stable.py", _iso(old - timedelta(days=5)))

    mem = _MemSvcStub([
        {"id": "mem-stale", "domain": "py", "name": "n1",
         "description": "changed.py returns 1", "valid_at": _iso(old),
         "status": "active", "evidence": {"files": ["svc/changed.py"]}},
        {"id": "mem-fresh", "domain": "py", "name": "n2",
         "description": "stable.py is fine", "valid_at": _iso(now),
         "status": "active", "evidence": {"files": ["svc/stable.py"]}},
    ])
    _patch_ctx(monkeypatch, tmp_path, brain_db, mem)

    op = _mk_op()
    selected = op.select("p")
    assert selected, "drifted memory must be flagged"

    # Every selected item is a real pending consolidation_candidate carrying
    # the stale memory id in scope_json — and ONLY the stale one.
    scores_db = str(tmp_path / "scores.db")
    c = sqlite3.connect(scores_db)
    c.row_factory = sqlite3.Row
    try:
        flagged_mem_ids = set()
        for cid in selected:
            row = c.execute(
                "SELECT status, scope_json FROM consolidation_candidates "
                "WHERE id=?", (cid,),
            ).fetchone()
            assert row is not None, f"select did not enqueue candidate {cid}"
            assert row["status"] == "pending"
            import json as _j
            flagged_mem_ids.add(_j.loads(row["scope_json"])["memory_id"])
    finally:
        c.close()
    assert "mem-stale" in flagged_mem_ids
    assert "mem-fresh" not in flagged_mem_ids, (
        "a fresh memory over unchanged code must NOT be flagged"
    )


# ---------------------------------------------------------------------------
# (c) run_one writes a consolidation_runs row with op_type=verify_staleness
# ---------------------------------------------------------------------------

def test_run_one_writes_verify_staleness_op_type(monkeypatch, tmp_path):
    from prism_service.services.memory_ops import runner

    brain_db = _build_dbs(tmp_path)
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=10)
    _insert_doc(brain_db, "svc/changed.py", _iso(now))

    mem = _MemSvcStub([
        {"id": "mem-stale", "domain": "py", "name": "n1",
         "description": "changed.py returns 1", "valid_at": _iso(old),
         "status": "active", "evidence": {"files": ["svc/changed.py"]}},
    ])
    _patch_ctx(monkeypatch, tmp_path, brain_db, mem)

    # The verdict carries BOTH the staleness fields and the shared audit
    # envelope JanitorService.submit requires (so run_one can persist the row).
    verdict = (
        '{"decision": "needs_review", "confidence": 0.4, '
        '"correction": "now returns 2", "qualitative_score": 0.4, '
        '"narrative": "code drifted", "new_memories": [], '
        '"invalidate_memory_ids": []}'
    )

    class _Res:
        exit_code = 0
        run_id = "run-1"
        duration_s = 0.1

        def final_text(self):
            return verdict

    monkeypatch.setattr(
        "prism_service.inference.claude_cli.invoke", lambda **kw: _Res(),
    )

    op = _mk_op()
    selected = op.select("p")
    assert selected
    result = runner.run_one(op, selected[0], "p")
    assert result.ok, getattr(result, "error", result)

    conn = sqlite3.connect(str(tmp_path / "scores.db"))
    try:
        row = conn.execute(
            "SELECT op_type FROM consolidation_runs WHERE candidate_id=?",
            (selected[0],),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None and row[0] == "verify_staleness", (
        f"op_type not persisted as verify_staleness: {row}"
    )


# ---------------------------------------------------------------------------
# (d) apply_verdict: needs_review flips memory status
# ---------------------------------------------------------------------------

def test_apply_verdict_needs_review_sets_status(monkeypatch, tmp_path):
    brain_db = _build_dbs(tmp_path)
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=10)
    _insert_doc(brain_db, "svc/changed.py", _iso(now))

    mem = _MemSvcStub([
        {"id": "mem-stale", "domain": "py", "name": "n1",
         "description": "old fact", "valid_at": _iso(old),
         "status": "active", "evidence": {"files": ["svc/changed.py"]}},
    ])
    _patch_ctx(monkeypatch, tmp_path, brain_db, mem)

    op = _mk_op()
    item = op.select("p")[0]
    op.apply_verdict(item, {"decision": "needs_review", "confidence": 0.3}, "p")

    assert mem.updated.get("mem-stale", {}).get("status") == "needs_review", (
        f"needs_review verdict must flip status: {mem.updated}"
    )


# ---------------------------------------------------------------------------
# (e) apply_verdict: supersede mints a corrected memory
# ---------------------------------------------------------------------------

def test_apply_verdict_supersede_stores_correction(monkeypatch, tmp_path):
    brain_db = _build_dbs(tmp_path)
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=10)
    _insert_doc(brain_db, "svc/changed.py", _iso(now))

    mem = _MemSvcStub([
        {"id": "mem-stale", "domain": "py", "name": "n1",
         "description": "changed.py returns 1", "valid_at": _iso(old),
         "status": "active", "evidence": {"files": ["svc/changed.py"]}},
    ])
    _patch_ctx(monkeypatch, tmp_path, brain_db, mem)

    op = _mk_op()
    item = op.select("p")[0]
    op.apply_verdict(
        item,
        {"decision": "supersede", "confidence": 0.9,
         "correction": "changed.py now returns 2"},
        "p",
    )
    assert mem.stored, "supersede verdict must store a corrected memory"
    assert any(
        "returns 2" in (s.get("description") or "") for s in mem.stored
    ), f"corrected memory should carry the correction text: {mem.stored}"


# ---------------------------------------------------------------------------
# (f) worker registers the op behind PRISM_VERIFY_STALENESS_WORKER
# ---------------------------------------------------------------------------

def test_worker_registers_verify_staleness():
    from prism_service.services import memory_ops_worker as mow

    op_types = {getattr(o, "op_type", "") for o in mow.registered_ops()}
    assert "verify_staleness" in op_types, (
        f"memory_ops_worker.registered_ops() must include the verify_staleness "
        f"op: {op_types}"
    )
