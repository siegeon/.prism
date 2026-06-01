"""Red scaffold (Tier 2) — the `forget` memory-operation runner.

Pins the acceptance criteria for `ForgetOperation` — principled archival
under constraints. These tests assert REAL seams, not mocks:

  * ForgetOperation IS a MemoryOperation (typed contract, op_type='forget');
  * select() returns ONLY low-activation fading entries (Tier-1 prior),
    and NEVER a pinned / safety entry (evidence.pinned) or one that helped
    a task outcome (effectiveness > 0);
  * apply_verdict archives via the existing temporal-validity path
    (status='archived' + invalid_at) and NEVER deletes the JSONL row —
    the row stays recoverable in supersede history;
  * a pinned entry is refused by apply_verdict even if the verdict says
    'archive' (guard rail: never blind-delete a load-bearing record);
  * run_one writes a consolidation_runs row with op_type='forget'
    (read back through a SEPARATE sqlite connection — durability, not echo);
  * the op is registered behind the PRISM_FORGET_WORKER env gate.

Every test FAILS today: `memory_ops/forget.py` does not exist and the
worker's registered_ops() is empty.
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
    from prism_service.engines.brain_engine import Brain

    scores = tmp_path / "scores.db"
    Brain(
        brain_db=str(tmp_path / "brain.db"),
        graph_db=str(tmp_path / "graph.db"),
        scores_db=str(scores),
    )
    return str(scores)


def _memory_svc(tmp_path: Path):
    from prism_service.services.memory_service import MemoryService

    return MemoryService(str(tmp_path / "mulch"))


class _FakeCliResult:
    def __init__(self, text: str, run_id: str = "run-1"):
        self._text = text
        self.exit_code = 0
        self.run_id = run_id
        self.duration_s = 0.1

    def final_text(self) -> str:
        return self._text


def _patch_project(monkeypatch, scores_db, mem_svc, tmp_path):
    """Point get_project + source_dir at temp resources so the runner +
    forget op run fully offline against a REAL MemoryService."""

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


def _seed_fader(mem_svc, name="cold fact", importance=1):
    """A low-importance, never-recalled entry => low activation => a fader."""
    return mem_svc.store(
        domain="project", name=name, description=f"{name} body",
        type="pattern", classification="tactical", importance=importance,
    )


_VALID_ARCHIVE_VERDICT = (
    '{"qualitative_score": 0.2, "narrative": "stale, no recall signal", '
    '"new_memories": [], "invalidate_memory_ids": [], "confidence": 0.6, '
    '"decision": "archive", "reason": "low activation, never helped a task"}'
)


# ---------------------------------------------------------------------------
# (a) ForgetOperation is a typed MemoryOperation with op_type='forget'
# ---------------------------------------------------------------------------

def test_forget_is_a_memory_operation():
    from prism_service.services.memory_ops.base import MemoryOperation
    from prism_service.services.memory_ops.forget import ForgetOperation

    op = ForgetOperation()
    assert isinstance(op, MemoryOperation)
    assert op.op_type == "forget"


# ---------------------------------------------------------------------------
# (b) select() returns ONLY low-activation fading entries (Tier-1 prior)
# ---------------------------------------------------------------------------

def test_select_returns_only_fading_entries(monkeypatch, tmp_path):
    from prism_service.services.memory_ops.forget import ForgetOperation

    scores_db = _make_scores_db(tmp_path)
    mem = _memory_svc(tmp_path)
    _patch_project(monkeypatch, scores_db, mem, tmp_path)

    fader = _seed_fader(mem, name="cold fact", importance=1)
    # A high-importance (warm) entry must NOT be selected for forgetting.
    warm = mem.store(
        domain="project", name="hot fact", description="hot body",
        type="pattern", classification="foundational", importance=10,
    )

    op = ForgetOperation()
    candidate_ids = op.select("p")
    assert candidate_ids, "select() found no faders despite a cold entry"

    # Each returned item must be a real consolidation_candidates id whose
    # scope_json names a faded entry — and never the warm entry.
    conn = sqlite3.connect(scores_db)
    try:
        named = set()
        for cid in candidate_ids:
            row = conn.execute(
                "SELECT scope_json, trigger FROM consolidation_candidates "
                "WHERE id = ?", (cid,),
            ).fetchone()
            assert row is not None, f"{cid} is not a real candidate row"
            import json as _j
            named.add(_j.loads(row[0] or "{}").get("entry_id"))
    finally:
        conn.close()
    assert fader.id in named, "the cold fader was not selected"
    assert warm.id not in named, "a warm/high-activation entry was selected"


# ---------------------------------------------------------------------------
# (c) a pinned / safety-critical entry is NEVER selected for forgetting
# ---------------------------------------------------------------------------

def test_select_skips_pinned_entry(monkeypatch, tmp_path):
    from prism_service.services.memory_ops.forget import ForgetOperation

    scores_db = _make_scores_db(tmp_path)
    mem = _memory_svc(tmp_path)
    _patch_project(monkeypatch, scores_db, mem, tmp_path)

    # A cold entry that is PINNED (safety-critical) — low activation but
    # must survive: pinned lives in the evidence dict.
    pinned = mem.store(
        domain="project", name="pinned safety rule", description="do not delete",
        type="convention", classification="foundational", importance=1,
        evidence={"pinned": True},
    )

    op = ForgetOperation()
    candidate_ids = op.select("p")

    import json as _j
    conn = sqlite3.connect(scores_db)
    try:
        named = set()
        for cid in candidate_ids:
            row = conn.execute(
                "SELECT scope_json FROM consolidation_candidates WHERE id=?",
                (cid,),
            ).fetchone()
            named.add(_j.loads(row[0] or "{}").get("entry_id"))
    finally:
        conn.close()
    assert pinned.id not in named, "a pinned entry must never be a forget candidate"


# ---------------------------------------------------------------------------
# (d) an entry that HELPED a task outcome (effectiveness > 0) is excluded
# ---------------------------------------------------------------------------

def test_select_skips_entry_that_helped_a_task(monkeypatch, tmp_path):
    from prism_service.services.memory_ops.forget import ForgetOperation

    scores_db = _make_scores_db(tmp_path)
    mem = _memory_svc(tmp_path)
    _patch_project(monkeypatch, scores_db, mem, tmp_path)

    helped = _seed_fader(mem, name="helped fact", importance=1)
    # Mark it as having helped a task outcome.
    mem.update_entry(helped.id, effectiveness=0.9)

    op = ForgetOperation()
    candidate_ids = op.select("p")

    import json as _j
    conn = sqlite3.connect(scores_db)
    try:
        named = set()
        for cid in candidate_ids:
            row = conn.execute(
                "SELECT scope_json FROM consolidation_candidates WHERE id=?",
                (cid,),
            ).fetchone()
            named.add(_j.loads(row[0] or "{}").get("entry_id"))
    finally:
        conn.close()
    assert helped.id not in named, "an entry that helped a task must not be forgotten"


# ---------------------------------------------------------------------------
# (e) apply_verdict archives via invalid_at — and NEVER deletes the row
# ---------------------------------------------------------------------------

def test_apply_verdict_archives_without_deleting(monkeypatch, tmp_path):
    from prism_service.services.memory_ops.forget import ForgetOperation

    scores_db = _make_scores_db(tmp_path)
    mem = _memory_svc(tmp_path)
    _patch_project(monkeypatch, scores_db, mem, tmp_path)

    fader = _seed_fader(mem, name="archive me", importance=1)
    op = ForgetOperation()
    candidate_ids = op.select("p")
    assert candidate_ids
    item = candidate_ids[0]

    import json as _j
    verdict = _j.loads(_VALID_ARCHIVE_VERDICT)
    op.apply_verdict(item, verdict, "p")

    # Row still exists (recoverable in supersede history), but archived.
    after = mem.get_entry(fader.id)
    assert after is not None, "forget DELETED the row — must only archive"
    assert after.status == "archived", f"status not archived: {after.status!r}"
    assert after.invalid_at, "invalid_at not stamped — temporal-validity path unused"


# ---------------------------------------------------------------------------
# (f) GUARD RAIL: a pinned entry is refused even if the verdict says archive
# ---------------------------------------------------------------------------

def test_apply_verdict_refuses_to_archive_pinned(monkeypatch, tmp_path):
    from prism_service.services.memory_ops.forget import ForgetOperation

    scores_db = _make_scores_db(tmp_path)
    mem = _memory_svc(tmp_path)
    _patch_project(monkeypatch, scores_db, mem, tmp_path)

    pinned = mem.store(
        domain="project", name="pinned rule", description="load-bearing",
        type="convention", classification="foundational", importance=1,
        evidence={"pinned": True},
    )
    # Build a candidate row pointing at the pinned entry directly (bypassing
    # select's own pinned filter) to prove apply_verdict is a SECOND guard.
    cid = "forget-pinned-1"
    conn = sqlite3.connect(scores_db)
    try:
        conn.execute(
            "INSERT INTO consolidation_candidates "
            "(id, task_id, session_id, trigger, status, queued_at, scope_json) "
            "VALUES (?, NULL, ?, 'forget', 'pending', ?, ?)",
            (cid, "s", "2026-05-25T10:00:00+00:00",
             '{"entry_id": "%s"}' % pinned.id),
        )
        conn.commit()
    finally:
        conn.close()

    import json as _j
    op = ForgetOperation()
    with pytest.raises(Exception):
        op.apply_verdict(cid, _j.loads(_VALID_ARCHIVE_VERDICT), "p")
    after = mem.get_entry(pinned.id)
    assert after.status == "active" and not after.invalid_at, (
        "a pinned entry was archived despite the guard rail"
    )


# ---------------------------------------------------------------------------
# (g) GUARD RAIL: a verdict with no reason is refused (require the reason)
# ---------------------------------------------------------------------------

def test_apply_verdict_requires_reason(monkeypatch, tmp_path):
    from prism_service.services.memory_ops.forget import ForgetOperation

    scores_db = _make_scores_db(tmp_path)
    mem = _memory_svc(tmp_path)
    _patch_project(monkeypatch, scores_db, mem, tmp_path)

    fader = _seed_fader(mem, name="needs reason", importance=1)
    op = ForgetOperation()
    item = op.select("p")[0]

    no_reason = {
        "qualitative_score": 0.2, "narrative": "n", "new_memories": [],
        "invalidate_memory_ids": [], "confidence": 0.6,
        "decision": "archive", "reason": "",
    }
    with pytest.raises(Exception):
        op.apply_verdict(item, no_reason, "p")
    after = mem.get_entry(fader.id)
    assert after.status == "active", "archived without a reason — guard rail breached"


# ---------------------------------------------------------------------------
# (h) run_one drives forget end-to-end: archives + writes op_type='forget'
# ---------------------------------------------------------------------------

def test_run_one_forget_writes_op_type_and_archives(monkeypatch, tmp_path):
    from prism_service.services.memory_ops import runner
    from prism_service.services.memory_ops.forget import ForgetOperation

    scores_db = _make_scores_db(tmp_path)
    mem = _memory_svc(tmp_path)
    _patch_project(monkeypatch, scores_db, mem, tmp_path)

    monkeypatch.setattr(
        "prism_service.inference.claude_cli.invoke",
        lambda **kw: _FakeCliResult(_VALID_ARCHIVE_VERDICT),
    )

    fader = _seed_fader(mem, name="e2e forget", importance=1)
    op = ForgetOperation()
    item = op.select("p")[0]
    result = runner.run_one(op, item, "p")
    assert result.ok, getattr(result, "error", result)

    conn = sqlite3.connect(scores_db)
    try:
        row = conn.execute(
            "SELECT op_type FROM consolidation_runs WHERE candidate_id=?",
            (item,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None and row[0] == "forget", f"op_type not 'forget': {row}"
    after = mem.get_entry(fader.id)
    assert after.status == "archived" and after.invalid_at, "entry not archived by run_one"


# ---------------------------------------------------------------------------
# (i) the forget op is registered behind the PRISM_FORGET_WORKER env gate
# ---------------------------------------------------------------------------

def test_forget_registered_in_worker():
    from prism_service.services import memory_ops_worker as mow

    types = {getattr(o, "op_type", "") for o in mow.registered_ops()}
    assert "forget" in types, (
        f"ForgetOperation not in registered_ops(): {types}"
    )
