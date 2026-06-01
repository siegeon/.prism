"""Red scaffold (Tier 2) — the MERGE memory-operation runner.

Pins the real seams of ``memory_ops.merge.MergeOperation``, the first
concrete op on the Tier-2 chassis:

  * it IS a ``MemoryOperation`` (typed contract, op_type='merge');
  * ``select(project)`` uses the Tier-1 activation prior
    (``MemoryService.fading_entries``) + Brain similarity to surface a
    cluster of N>=2 near-duplicate expertise memories, enqueued as a
    ``consolidation_candidate`` whose scope carries the member ids;
  * ``run_one(merge_op, candidate, project)`` drives the SHARED inference
    + audit path and writes a ``consolidation_runs`` row whose
    ``op_type`` column reads back 'merge' (separate connection => durable);
  * ``apply_verdict`` mints the synthesized canonical memory at
    generation = max(member generation) + 1 and supersedes (archives)
    every member via the existing memory_service path, preserving history;
  * the op registers behind the ``PRISM_MERGE_WORKER`` env gate.

Every test FAILS today: ``memory_ops.merge`` does not exist.
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
    """Real scores.db with the consolidation schema (Brain() init builds it)."""
    from prism_service.engines.brain_engine import Brain

    scores = tmp_path / "scores.db"
    Brain(
        brain_db=str(tmp_path / "brain.db"),
        graph_db=str(tmp_path / "graph.db"),
        scores_db=str(scores),
    )
    return str(scores)


def _seed_cluster(svc) -> list:
    """Seed a real MemoryService with a 2-member near-duplicate cluster
    plus one unrelated entry. Returns the member ExpertiseEntry rows."""
    a = svc.store(
        domain="expertise", name="dev means source-run",
        description="dev is the editable source-run on this windows box, "
        "ports 8887 and 8888; never docker or pipx or a tauri build.",
        type="convention", classification="environment", importance=8,
    )
    b = svc.store(
        domain="expertise", name="dev is source-run not a build",
        description="development means the editable pip source-run, never "
        "a docker image or pipx wheel or tauri compile; ports 8887/8888.",
        type="convention", classification="environment", importance=7,
    )
    svc.store(
        domain="expertise", name="unrelated porkbun dns",
        description="point growwise.studio at the droplet via an A record.",
        type="fact", classification="ops", importance=3,
    )
    return [a, b]


class _FakeCliResult:
    def __init__(self, text: str, run_id: str = "run-merge"):
        self._text = text
        self.exit_code = 0
        self.run_id = run_id
        self.duration_s = 0.1

    def final_text(self) -> str:
        return self._text


def _patch_project(monkeypatch, scores_db: str, tmp_path: Path, svc):
    """Point get_project at a context exposing the temp data dir AND the
    real seeded MemoryService, so select/apply_verdict run fully offline."""
    data_dir = Path(scores_db).parent

    class _Ctx:
        _data_dir = data_dir
        memory_svc = svc

    monkeypatch.setattr(
        "prism_service.project_context.get_project", lambda p: _Ctx()
    )
    monkeypatch.setattr(
        "prism_service.services.source_service.source_dir_for",
        lambda p: tmp_path, raising=False,
    )
    return _Ctx


def _real_memory_svc(tmp_path: Path):
    from prism_service.services.memory_service import MemoryService

    return MemoryService(str(tmp_path / "mulch"))


def _merge_verdict(synthesized_name: str, member_ids: list) -> str:
    """A merge verdict: JanitorService-required fields PLUS the merge-specific
    synthesized memory + the member ids it supersedes."""
    import json as _j

    return _j.dumps({
        "qualitative_score": 0.9,
        "narrative": "two memories state the same fact; merged into one.",
        "confidence": 0.9,
        "same_fact": True,
        "invalidate_memory_ids": list(member_ids),
        "new_memories": [{
            "name": synthesized_name,
            "description": "dev is the editable source-run (ports 8887/8888); "
            "never docker, pipx, or a tauri build.",
            "domain": "expertise", "type": "convention",
            "classification": "environment", "importance": 8,
        }],
    })


# ---------------------------------------------------------------------------
# (a) MergeOperation is a MemoryOperation with op_type='merge'
# ---------------------------------------------------------------------------

def test_merge_is_a_memory_operation():
    from prism_service.services.memory_ops.base import MemoryOperation
    from prism_service.services.memory_ops.merge import MergeOperation

    op = MergeOperation()
    assert isinstance(op, MemoryOperation)
    assert op.op_type == "merge"
    # apply_verdict / build_prompt / select are all implemented (not abstract)
    assert callable(op.select) and callable(op.build_prompt)
    assert callable(op.apply_verdict)


# ---------------------------------------------------------------------------
# (b) select() surfaces a cluster of N>=2 near-duplicate memories
# ---------------------------------------------------------------------------

def test_select_surfaces_a_cluster(monkeypatch, tmp_path):
    from prism_service.services.janitor_service import JanitorService
    from prism_service.services.memory_ops.merge import MergeOperation

    scores_db = _make_scores_db(tmp_path)
    svc = _real_memory_svc(tmp_path)
    members = _seed_cluster(svc)
    # Activation prior must see the cluster: fading_entries default threshold
    # is 0.0, so force the activation prior to surface them regardless of
    # their freshly-stored (positive) activation by lowering the bar.
    _patch_project(monkeypatch, scores_db, tmp_path, svc)

    op = MergeOperation()
    items = op.select("p")
    assert items, "select() returned no cluster candidates"

    # Each item is a consolidation_candidate id whose scope carries the
    # member ids of the cluster (N >= 2). Read the scope back from the DB.
    js = JanitorService(scores_db)
    cand = items[0]
    scope = js.scope_for(cand) if hasattr(js, "scope_for") else None
    if scope is None:
        conn = sqlite3.connect(scores_db)
        try:
            import json as _j
            raw = conn.execute(
                "SELECT scope_json FROM consolidation_candidates WHERE id=?",
                (cand,),
            ).fetchone()
        finally:
            conn.close()
        scope = _j.loads(raw[0]) if raw and raw[0] else {}
    member_ids = scope.get("member_ids") or []
    assert len(member_ids) >= 2, f"cluster must have N>=2 members: {scope}"
    seeded = {m.id for m in members}
    assert set(member_ids) & seeded, "cluster ids must be real seeded members"


# ---------------------------------------------------------------------------
# (c) run_one(merge_op, ...) writes a consolidation_runs row op_type='merge'
# ---------------------------------------------------------------------------

def test_run_one_writes_merge_run_row(monkeypatch, tmp_path):
    from prism_service.services.memory_ops import runner
    from prism_service.services.memory_ops.merge import MergeOperation

    scores_db = _make_scores_db(tmp_path)
    svc = _real_memory_svc(tmp_path)
    members = _seed_cluster(svc)
    _patch_project(monkeypatch, scores_db, tmp_path, svc)

    op = MergeOperation()
    items = op.select("p")
    assert items, "select() produced no candidate to run"
    cand = items[0]
    member_ids = [m.id for m in members]

    monkeypatch.setattr(
        "prism_service.inference.claude_cli.invoke",
        lambda **kw: _FakeCliResult(_merge_verdict("dev=source-run", member_ids)),
    )

    result = runner.run_one(op, cand, "p")
    assert result.ok, getattr(result, "error", result)

    conn = sqlite3.connect(scores_db)
    try:
        row = conn.execute(
            "SELECT op_type, output_json FROM consolidation_runs "
            "WHERE candidate_id=?",
            (cand,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "no consolidation_runs row was written"
    assert row[0] == "merge", f"op_type column not 'merge': {row[0]!r}"
    assert '"same_fact"' in (row[1] or ""), "merge verdict JSON not persisted"


# ---------------------------------------------------------------------------
# (d) apply_verdict supersedes members + mints a gen+1 canonical memory
# ---------------------------------------------------------------------------

def test_apply_verdict_supersedes_members_and_mints_gen_plus_one(
    monkeypatch, tmp_path
):
    from prism_service.services.memory_ops import runner
    from prism_service.services.memory_ops.merge import MergeOperation

    scores_db = _make_scores_db(tmp_path)
    svc = _real_memory_svc(tmp_path)
    members = _seed_cluster(svc)
    _patch_project(monkeypatch, scores_db, tmp_path, svc)

    op = MergeOperation()
    items = op.select("p")
    cand = items[0]
    member_ids = [m.id for m in members]
    max_gen = max(m.generation for m in members)

    monkeypatch.setattr(
        "prism_service.inference.claude_cli.invoke",
        lambda **kw: _FakeCliResult(_merge_verdict("canonical-dev", member_ids)),
    )

    result = runner.run_one(op, cand, "p")
    assert result.ok, getattr(result, "error", result)

    # Members are archived (superseded), preserving history (not deleted).
    for mid in member_ids:
        e = svc.get_entry(mid)
        assert e is not None, "member must be preserved, not deleted"
        assert e.status == "archived" and e.invalid_at, (
            f"member {mid} not superseded: status={e.status!r} "
            f"invalid_at={e.invalid_at!r}"
        )

    # The synthesized canonical memory is active and at generation max+1.
    active = [
        e for e in svc._read_entries("expertise")
        if e.status == "active" and not e.invalid_at
        and e.id not in set(member_ids)
    ]
    minted = [e for e in active if e.generation == max_gen + 1]
    assert minted, (
        f"no active gen={max_gen + 1} canonical memory minted; "
        f"active gens={[(e.name, e.generation) for e in active]}"
    )


# ---------------------------------------------------------------------------
# (e) the op registers behind the PRISM_MERGE_WORKER env gate
# ---------------------------------------------------------------------------

def test_merge_registered_in_worker(monkeypatch):
    from prism_service.services import memory_ops_worker as mow
    from prism_service.services.memory_ops.merge import MergeOperation

    ops = mow.registered_ops()
    assert any(isinstance(o, MergeOperation) for o in ops), (
        "MergeOperation must be in memory_ops_worker.registered_ops()"
    )

    # Env-gated: PRISM_MERGE_WORKER=on starts a thread for it; off => none.
    for k in list(__import__("os").environ):
        if k.startswith("PRISM_") and k.endswith("_WORKER"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(mow, "_loop_for", lambda op: None, raising=False)
    assert mow.start_memory_ops_workers() in ([], None), (
        "merge worker must be OFF by default"
    )
    monkeypatch.setenv("PRISM_MERGE_WORKER", "on")
    started = mow.start_memory_ops_workers()
    assert started, "PRISM_MERGE_WORKER=on must start the merge worker"
