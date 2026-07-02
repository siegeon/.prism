"""Phase 3 (epic 4fd1e6b4) — RED scaffold for migrating the real learning
handlers onto the event bus and retiring the memory timers. Task 386f4a26.

Phase 1 built the substrate (services/event_pool.py). Phase 2 wired the
three emitters live (mcp/tools.py + claude_transcripts.py) while handlers
stayed wrap/no-op and the timer daemons kept running (dual-run). Phase 3
makes the handlers REAL and retires the timers one-at-a-time so the
9->1 collapse actually lands.

These tests pin the USER-FACING migration, not a unit contract: the
swap point is event_pool.default_registry() / _register_default_handlers
(the single place get_bus() registers handlers), so a handler that exists
in some module but is never wired into the process-singleton bus still
fails here. Every behavioral assertion drives the handler the way the
ConsumerPool drives it (bus dispatch / drain_once), and the inference
assertions monkeypatch the SINGLE claude -p chokepoint and count calls.

They FAIL today because default_registry still returns [_noop_handler]
for all three event types and the timers are still wired in main.py
lifespan.
"""

from __future__ import annotations

import inspect
import re
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


# ----------------------------------------------------------------------
# Isolation: per-project data dir + a FRESH process-singleton bus.
# ----------------------------------------------------------------------
@pytest.fixture
def project(tmp_path):
    from prism_service import config as cfg
    from prism_service import project_context as pc

    original = cfg.PROJECTS_DIR
    cfg.PROJECTS_DIR = tmp_path / "projects"
    cfg.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    # get_project no longer creates on miss (d37193da):
    # seed the sandboxed project dir explicitly.
    cfg.project_data_dir("test-phase3")
    pc._contexts.clear()
    yield "test-phase3"
    cfg.PROJECTS_DIR = original
    pc._contexts.clear()


@pytest.fixture
def fresh_bus():
    """Reset the event_pool process-singleton so get_bus() re-registers
    the DEFAULT handler surface — that's the swap point under test."""
    from prism_service.services import event_pool as ep

    saved = ep._BUS
    ep._BUS = None
    bus = ep.get_bus()
    yield bus
    ep._BUS = saved


def _drain_synchronously(bus):
    """Run the real ConsumerPool one-shot drain so every queued event is
    dispatched to its registered handler (the way the daemon does it)."""
    from prism_service.services import event_pool as ep

    pool = ep.ConsumerPool(bus, max_concurrency=2, per_interval_call_budget=999)
    return pool.drain_once()


# ======================================================================
# Criterion 1 — default_registry / _register_default_handlers no longer
# return _noop_handler for the three types; each returns a REAL handler.
# ======================================================================
def test_default_registry_has_no_noop_handlers():
    from prism_service.services import event_pool as ep

    registry = ep.default_registry()
    for etype in (ep.SESSION_IMPORTED, ep.MEMORY_WRITTEN,
                  ep.MEMORY_RECALLED_OUTCOME):
        handlers = registry.get(etype) or []
        assert handlers, f"no handler registered for {etype}"
        for h in handlers:
            assert h is not ep._noop_handler, (
                f"{etype} still maps to the Phase-1 _noop_handler — the real "
                "migrated handler was never wired into default_registry()"
            )
            # The real handler is not the trivial no-op function object.
            name = getattr(h, "__name__", "")
            assert name != "_noop_handler", f"{etype} handler is still a no-op"


def test_get_bus_registers_real_handlers():
    """The PROCESS-SINGLETON bus (what emitters actually hit) carries a
    non-noop handler for each event type after get_bus() bootstraps it."""
    from prism_service.services import event_pool as ep

    saved = ep._BUS
    ep._BUS = None
    try:
        bus = ep.get_bus()
        for etype in (ep.SESSION_IMPORTED, ep.MEMORY_WRITTEN,
                      ep.MEMORY_RECALLED_OUTCOME):
            hs = bus.handlers_for(etype)
            assert hs, f"singleton bus has no handler for {etype}"
            assert all(h is not ep._noop_handler for h in hs), (
                f"singleton bus still dispatches {etype} to _noop_handler"
            )
    finally:
        ep._BUS = saved


# ======================================================================
# Criterion 2 — session.imported and memory.written register with
# inference=True so the BudgetGovernor charges them; recalled+outcome
# must NOT be an inference type (zero LLM).
# ======================================================================
def test_inference_flags_on_singleton_bus():
    from prism_service.services import event_pool as ep

    saved = ep._BUS
    ep._BUS = None
    try:
        bus = ep.get_bus()
        assert bus.is_inference(ep.SESSION_IMPORTED), (
            "session.imported must register inference=True so the governor "
            "charges + circuit-breaks it"
        )
        assert bus.is_inference(ep.MEMORY_WRITTEN), (
            "memory.written must register inference=True"
        )
        assert not bus.is_inference(ep.MEMORY_RECALLED_OUTCOME), (
            "memory.recalled+outcome makes ZERO LLM calls — it must NOT be "
            "marked an inference type"
        )
    finally:
        ep._BUS = saved


# ======================================================================
# Criterion 10 — the migrated timers are RETIRED from main.py lifespan.
# After Phase 3 the work runs via the bus handlers, so the lifespan must
# no longer spin these daemons. Source assertion so it fails loudly if a
# timer is left wired (the 9->1 collapse didn't land).
# ======================================================================
def _lifespan_source() -> str:
    import prism_service.main as main_mod
    return inspect.getsource(main_mod)


@pytest.mark.parametrize("spawn_call", [
    "start_reflection_worker(",
    "start_memory_summary_worker(",
])
def test_retired_timers_not_spawned_in_lifespan(spawn_call):
    src = _lifespan_source()
    # The IMPORT may remain (used by the migrated handler), but the lifespan
    # must not SPAWN the daemon any more.
    assert spawn_call not in src, (
        f"main.py lifespan still spawns {spawn_call} — the timer was not "
        "retired; the work must now run via the event-bus handler"
    )


def test_transcript_to_candidate_timer_retired():
    """The transcript->candidate REFLECTION path is retired from lifespan;
    session.imported handles candidate reflection on the bus now. The
    standalone transcript IMPORT poller may stay (it produces the candidate
    + emits SESSION_IMPORTED), but the always-on reflection drain it fed is
    gone. Pin both: the replacement (event pool) is wired AND an explicit
    marker documents the transcript->candidate timer retirement, so this
    fails today (no handler/marker) and can't vacuously pass."""
    src = _lifespan_source()
    assert "start_event_pool" in src, (
        "event pool must remain wired — it is the replacement for the "
        "retired timers"
    )
    assert "TRANSCRIPT_CANDIDATE_RETIRED_TO_BUS" in src, (
        "expected a TRANSCRIPT_CANDIDATE_RETIRED_TO_BUS marker in lifespan "
        "documenting that the transcript->candidate reflection timer was "
        "retired onto the session.imported handler"
    )


def test_memory_ops_merge_timer_retired():
    """The Memory Ops Merge op no longer runs on its own interval daemon —
    memory.written dedup/supersede DETECTION runs on the bus. Pin that the
    merge op is not started as a standalone always-on worker in lifespan."""
    src = _lifespan_source()
    # start_memory_ops_workers may remain for OTHER ops, but the merge op
    # must be excluded from the bus-migrated set. Assert an explicit marker
    # the implementer sets when merge is removed from the always-on fleet.
    assert "MERGE_RETIRED_TO_BUS" in src, (
        "expected a MERGE_RETIRED_TO_BUS marker in lifespan documenting that "
        "the merge op was retired onto the memory.written handler"
    )


# ----------------------------------------------------------------------
# Inference chokepoint spy: count + capture every claude -p invocation so
# the deterministic-vs-inference criteria are measurable.
# ----------------------------------------------------------------------
@pytest.fixture
def claude_spy(monkeypatch):
    calls: list[dict] = []

    class _Result:
        exit_code = 0

        def final_text(self):
            return "a concise one sentence summary"

    def _fake_invoke(prompt, work_dir, plugin_dir, **kwargs):
        calls.append({"prompt": prompt, "model": kwargs.get("model", ""),
                      "purpose": kwargs.get("purpose", "")})
        return _Result()

    from prism_service.inference import claude_cli
    monkeypatch.setattr(claude_cli, "invoke", _fake_invoke)
    return calls


def _seed_memory(ctx, name, description, domain="project"):
    return ctx.memory_svc.store(
        domain=domain, name=name, description=description,
        type="convention", classification="tactical", importance=5,
    )


# ======================================================================
# Criterion 7 — memory.written summarize uses --model haiku.
# ======================================================================
def test_memory_written_summarize_uses_haiku(project, fresh_bus, claude_spy):
    from prism_service.project_context import get_project
    from prism_service.services import event_pool as ep

    ctx = get_project(project)
    entry = _seed_memory(ctx, "needs-summary",
                         "a brand new unique memory with no near duplicate "
                         "anywhere about quantum widget calibration protocol")
    eid = entry.get("id") if isinstance(entry, dict) else entry.id

    fresh_bus.emit(ep.Event(ep.MEMORY_WRITTEN, {"memory_id": eid}))
    _drain_synchronously(fresh_bus)

    summarize_calls = [c for c in claude_spy if c["purpose"] == "memory_summary"
                       or "summar" in c["prompt"].lower()]
    assert summarize_calls, (
        "memory.written handler never invoked claude -p to summarize a "
        "novel memory — summarize is not wired onto the bus"
    )
    assert any(c["model"] == "haiku" for c in summarize_calls), (
        "memory.written summarize must invoke claude with --model haiku; "
        f"saw models {[c['model'] for c in summarize_calls]!r}"
    )


# ======================================================================
# Criterion 6 — dedup/supersede DETECTION is deterministic: a near-dup
# write is detected with ZERO claude -p.
# ======================================================================
def test_memory_written_near_dup_detected_with_zero_claude(
        project, fresh_bus, claude_spy):
    from prism_service.project_context import get_project
    from prism_service.services import event_pool as ep

    ctx = get_project(project)
    # An existing memory + a near-identical new write.
    orig = _seed_memory(
        ctx, "deploy-rule",
        "Always run the database migration before deploying the api")
    oid = orig.get("id") if isinstance(orig, dict) else orig.id
    dup = _seed_memory(ctx, "deploy-rule-2",
                       "Always run the DB migration before deploying the API")
    did = dup.get("id") if isinstance(dup, dict) else dup.id

    before_signal = ctx.memory_svc.get_entry(oid).recall_count
    claude_spy.clear()
    fresh_bus.emit(ep.Event(ep.MEMORY_WRITTEN, {"memory_id": did}))
    _drain_synchronously(fresh_bus)

    # Detection of the near-duplicate is deterministic (embedding/string-sim
    # + entity-predicate match) — it must NOT shell claude just to NOTICE the
    # duplicate. (Summarize of a true novelty is the only allowed call, and
    # this write is a dup so even that is suppressed.)
    assert claude_spy == [], (
        "near-duplicate detection shelled claude -p; detection must be "
        f"deterministic (zero inference). Calls: {claude_spy!r}"
    )
    # ...AND the handler actually RAN and DETECTED the dup: the near-dup is
    # reconciled against the original (the original's signal/recall is bumped).
    # This distinguishes real deterministic detection from "no handler ran".
    after_signal = ctx.memory_svc.get_entry(oid).recall_count
    assert after_signal > before_signal, (
        "memory.written did not deterministically detect/reconcile the "
        f"near-duplicate against the original (signal {before_signal} -> "
        f"{after_signal}) — a no-op handler would leave both rows untouched"
    )


# ======================================================================
# Criterion 8 — input-hash skip: re-emitting the SAME write does not
# re-run inference.
# ======================================================================
def test_memory_written_input_hash_skip(project, fresh_bus, claude_spy):
    from prism_service.project_context import get_project
    from prism_service.services import event_pool as ep

    ctx = get_project(project)
    entry = _seed_memory(ctx, "hash-skip",
                         "a unique memory about the gyroscopic flux capacitor "
                         "alignment sequence used nowhere else")
    eid = entry.get("id") if isinstance(entry, dict) else entry.id

    fresh_bus.emit(ep.Event(ep.MEMORY_WRITTEN, {"memory_id": eid}))
    _drain_synchronously(fresh_bus)
    first = len(claude_spy)
    assert first >= 1, "first write of a novel memory should run inference once"

    # Re-emit the identical write — same input hash, must be a no-op.
    fresh_bus.emit(ep.Event(ep.MEMORY_WRITTEN, {"memory_id": eid}))
    _drain_synchronously(fresh_bus)
    assert len(claude_spy) == first, (
        "re-emitting the same memory.written re-ran inference — the "
        f"input-hash skip is not wired (calls {first} -> {len(claude_spy)})"
    )


# ======================================================================
# Criterion 9 — memory.recalled+outcome makes ZERO LLM calls and bumps
# utility/signal ONLY on the specific memories in THIS recall's
# recall_log (per entry_id/task_id). Un-recalled memories + bad-outcome
# recalls get no bump.
# ======================================================================
def _utility(ctx, entry_id):
    e = ctx.memory_svc.get_entry(entry_id)
    # effectiveness is the per-memory utility signal driven off recall->outcome
    return None if e is None else e.effectiveness


def test_recalled_outcome_zero_llm_and_per_entry_bump(
        project, fresh_bus, claude_spy):
    import time as _t

    from prism_service.project_context import get_project
    from prism_service.services import event_pool as ep

    ctx = get_project(project)
    recalled = _seed_memory(ctx, "recalled-mem", "this one gets recalled+rewarded")
    other = _seed_memory(ctx, "un-recalled-mem", "this one is never recalled")
    rid = recalled.get("id") if isinstance(recalled, dict) else recalled.id
    oid = other.get("id") if isinstance(other, dict) else other.id

    tid = "task-credit-1"
    # Log a recall of ONLY `recalled` against this task, with a positive
    # outcome already attached (record_outcome ran upstream of the emit).
    now = _t.strftime("%Y-%m-%d %H:%M:%S")
    db = ctx.memory_svc._recall_db
    db.execute(
        "INSERT INTO recall_log (entry_id, entry_domain, query, recalled_at, "
        "task_id, outcome) VALUES (?, ?, 'q', ?, ?, 'positive')",
        (rid, "project", now, tid),
    )
    db.commit()

    util_recalled_before = _utility(ctx, rid)
    util_other_before = _utility(ctx, oid)

    claude_spy.clear()
    fresh_bus.emit(ep.Event(ep.MEMORY_RECALLED_OUTCOME, {
        "task_id": tid, "outcome": "positive", "updated": 1,
    }))
    _drain_synchronously(fresh_bus)

    # ZERO LLM calls — pure arithmetic on recall_log.
    assert claude_spy == [], (
        f"recalled+outcome handler shelled claude -p (must be zero): {claude_spy!r}"
    )

    util_recalled_after = _utility(ctx, rid)
    util_other_after = _utility(ctx, oid)

    # The recalled+rewarded memory's utility measurably INCREASES...
    assert util_recalled_after > util_recalled_before, (
        f"recalled+rewarded memory utility did not increase "
        f"({util_recalled_before} -> {util_recalled_after})"
    )
    # ...while the un-recalled memory gets NO bump.
    assert util_other_after == util_other_before, (
        f"un-recalled memory was bumped ({util_other_before} -> "
        f"{util_other_after}) — the bump must target only this recall's rows"
    )


def test_recalled_outcome_bad_outcome_no_positive_bump(
        project, fresh_bus, claude_spy):
    import time as _t

    from prism_service.project_context import get_project
    from prism_service.services import event_pool as ep

    ctx = get_project(project)
    mem = _seed_memory(ctx, "blamed-mem", "recalled then the task FAILED")
    mid = mem.get("id") if isinstance(mem, dict) else mem.id

    tid = "task-credit-bad"
    now = _t.strftime("%Y-%m-%d %H:%M:%S")
    db = ctx.memory_svc._recall_db
    db.execute(
        "INSERT INTO recall_log (entry_id, entry_domain, query, recalled_at, "
        "task_id, outcome) VALUES (?, ?, 'q', ?, ?, 'negative')",
        (mid, "project", now, tid),
    )
    db.commit()

    before = _utility(ctx, mid)
    fresh_bus.emit(ep.Event(ep.MEMORY_RECALLED_OUTCOME, {
        "task_id": tid, "outcome": "negative", "updated": 1,
    }))
    _drain_synchronously(fresh_bus)
    after = _utility(ctx, mid)

    # A bad-outcome recall must NOT raise utility — and the handler DID run
    # (it pushed utility DOWN for the blamed memory). Strict < distinguishes
    # real bad-outcome processing from a no-op handler that leaves it at 0.0.
    assert after < before, (
        f"a bad-outcome recall must lower the blamed memory's utility "
        f"(handler ran), got {before} -> {after} (no decrease => no handler)"
    )


# ----------------------------------------------------------------------
# session.imported helpers — seed a REAL consolidation_candidate so the
# handler resolves and reflects on it the way the importer feeds it.
# ----------------------------------------------------------------------
def _scores_db(ctx):
    return str(ctx._data_dir / "scores.db")


def _seed_candidate(ctx, session_id, *, with_signal=True):
    """Create a real pending consolidation_candidate and return its id.
    with_signal=False produces a no-task, all-zero-signal NOISE candidate
    (matches reflection_worker._is_noise_candidate). The candidate is
    created via record_session_outcome (the real dispatcher path, which
    also builds the scores.db schema + auto-enqueues), then its scope_json
    is set to the desired signal shape."""
    import asyncio
    import json as _json
    import sqlite3

    from prism_service.mcp.tools import handle_tool

    scores_db = _scores_db(ctx)
    asyncio.run(handle_tool("record_session_outcome", {
        "session_id": session_id, "duration_s": 1, "tokens_used": 1,
        "files_read": 0, "files_modified": 3 if with_signal else 0,
        "skills_invoked": 0,
    }, project_id="test-phase3"))
    if with_signal:
        scope = {"files_modified": 3, "signal_counts": {
            "pushbacks": 1, "bg_signals": 0, "tool_failures": 0,
            "memory_writes": 0}}
    else:
        scope = {"files_modified": 0, "signal_counts": {
            "pushbacks": 0, "bg_signals": 0, "tool_failures": 0,
            "memory_writes": 0}}
    conn = sqlite3.connect(scores_db)
    try:
        # Pin task_id NULL so the noise filter (task_id NULL + zero signals)
        # actually classifies a with_signal=False candidate as noise.
        conn.execute(
            "UPDATE consolidation_candidates SET scope_json=?, task_id=NULL "
            "WHERE session_id=?",
            (_json.dumps(scope), session_id),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id FROM consolidation_candidates WHERE session_id=? "
            "ORDER BY queued_at DESC LIMIT 1",
            (session_id,),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def _reflection_spy(monkeypatch, new_memories):
    """Spy claude_cli.invoke returning a reflection verdict that mints
    `new_memories`. Returns the call list."""
    calls: list[dict] = []

    class _Result:
        exit_code = 0
        run_id = "run-x"

        def final_text(self):
            import json
            return json.dumps({
                "qualitative_score": 0.9,
                "new_memories": new_memories,
            })

    def _fake_invoke(prompt, work_dir, plugin_dir, **kwargs):
        calls.append({"prompt": prompt, "model": kwargs.get("model", ""),
                      "purpose": kwargs.get("purpose", "")})
        return _Result()

    from prism_service.inference import claude_cli
    monkeypatch.setattr(claude_cli, "invoke", _fake_invoke)
    return calls


# ======================================================================
# Criterion 5 — session.imported deterministically SKIPS noise candidates
# (no claude -p for a no-signal candidate), reusing _is_noise_candidate.
# ======================================================================
def test_session_imported_skips_noise_candidate(project, fresh_bus, monkeypatch):
    from prism_service.project_context import get_project
    from prism_service.services import event_pool as ep

    import sqlite3

    ctx = get_project(project)
    cid = _seed_candidate(ctx, "S-noise", with_signal=False)
    calls = _reflection_spy(monkeypatch, new_memories=[])

    fresh_bus.emit(ep.Event(ep.SESSION_IMPORTED, {
        "session_id": "S-noise", "project": project,
    }))
    _drain_synchronously(fresh_bus)

    assert calls == [], (
        "session.imported reflected on a NOISE candidate (no task, zero "
        f"signals) — it must reuse _is_noise_candidate and skip. Calls: {calls!r}"
    )
    # ...AND the handler RAN: it deterministically dispositioned the noise
    # candidate OUT of 'pending' (so it isn't re-claimed forever). A no-op /
    # absent handler would leave it pending — that's the false-green guard.
    conn = sqlite3.connect(_scores_db(ctx))
    try:
        row = conn.execute(
            "SELECT status FROM consolidation_candidates WHERE id=?", (cid,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "noise candidate row vanished"
    assert row[0] != "pending", (
        "noise candidate left 'pending' — the handler must deterministically "
        f"skip+disposition it (status was {row[0]!r}); a no-op handler would "
        "leave it pending"
    )


# ======================================================================
# Criterion 4 — a BURST of N imports coalesces into ONE claude -p
# reflection pass (call count == 1 for the burst).
# ======================================================================
def test_session_imported_burst_coalesces_to_one_pass(
        project, fresh_bus, monkeypatch):
    from prism_service.project_context import get_project
    from prism_service.services import event_pool as ep

    ctx = get_project(project)
    for i in range(5):
        _seed_candidate(ctx, f"S-burst-{i}", with_signal=True)
    calls = _reflection_spy(monkeypatch, new_memories=[])

    for i in range(5):
        fresh_bus.emit(ep.Event(ep.SESSION_IMPORTED, {
            "session_id": f"S-burst-{i}", "project": project,
        }))
    _drain_synchronously(fresh_bus)

    assert len(calls) == 1, (
        f"a burst of 5 imports must coalesce into ONE reflection pass, "
        f"ran {len(calls)} claude -p calls"
    )


# ======================================================================
# Criterion 3 — recall/brain_search BEFORE any write; a matching existing
# memory is REINFORCED (no new duplicate row; matched memory's signal /
# recall updated).
# ======================================================================
def test_session_imported_reinforces_instead_of_duplicating(
        project, fresh_bus, monkeypatch):
    from prism_service.project_context import get_project
    from prism_service.services import event_pool as ep

    ctx = get_project(project)
    # An existing memory the reflection will "re-discover".
    existing = _seed_memory(
        ctx, "ci-runs-on-push",
        "CI runs the full pytest suite on every push to a feature branch")
    eid = existing.get("id") if isinstance(existing, dict) else existing.id

    def _count_rows():
        n = 0
        for dom in ctx.memory_svc.list_domains():
            n += len(ctx.memory_svc.list_entries(domain=dom,
                                                 status_filter="active"))
        return n

    before_rows = _count_rows()
    before_recall = ctx.memory_svc.get_entry(eid).recall_count

    _seed_candidate(ctx, "S-reinforce", with_signal=True)
    # The reflection proposes a NEAR-DUPLICATE of the existing memory.
    _reflection_spy(monkeypatch, new_memories=[{
        "domain": "project", "name": "ci-on-push",
        "description": "CI runs the full pytest suite on every push to a "
                       "feature branch",
        "type": "convention", "classification": "tactical",
    }])

    fresh_bus.emit(ep.Event(ep.SESSION_IMPORTED, {
        "session_id": "S-reinforce", "project": project,
    }))
    _drain_synchronously(fresh_bus)

    after_rows = _count_rows()
    after = ctx.memory_svc.get_entry(eid)

    # NO new duplicate row was minted...
    assert after_rows == before_rows, (
        f"session.imported minted a DUPLICATE memory row ({before_rows} -> "
        f"{after_rows}) instead of reinforcing the existing match — it must "
        "recall/brain_search before writing"
    )
    # ...and the matched memory's recall/signal was updated (reinforced).
    assert after.recall_count > before_recall, (
        f"matched memory was not reinforced (recall_count {before_recall} -> "
        f"{after.recall_count})"
    )
