"""RED scaffold — implement.js agent-run telemetry emitter (task f4498190).

The conductor SDLC workflow (implement.js) must emit one agent-run
telemetry row to /api/agent-runs/ingest after EACH step's agent()
returns — in BOTH the serial drive loop and the parallel fanout wrapper.
This is the USER-FACING seam: the JS the harness runs is what actually
populates the agent_runs spine. A green backend with a workflow that
never POSTs telemetry is a false-green — agent_runs stays empty in real
drives (the exact gap the task calls out as 'currently transient').

Source-structure asserts (mirror the session-wiring asserts in
test_task_session_association.py): read the workflow file and assert the
telemetry-emit wiring substrings are present.

ALL FAIL today: implement.js has no /api/agent-runs/ingest POST, no
emitter helper, and no parallel fanout wrapper.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
_WORKFLOWS = _SERVICE_ROOT.parent.parent / ".claude" / "workflows"


def _implement_src() -> str:
    return (_WORKFLOWS / "implement.js").read_text(encoding="utf-8")


def test_implement_posts_telemetry_to_agent_runs_ingest():
    """implement.js must POST each agent's telemetry to the registered
    ingest route /api/agent-runs/ingest."""
    src = _implement_src()
    assert "/api/agent-runs/ingest" in src, (
        "implement.js never POSTs to /api/agent-runs/ingest — the agent_runs "
        "spine is never populated by a real drive (telemetry stays transient)"
    )


def test_implement_has_agent_run_emitter_helper():
    """A dedicated emitter (e.g. postAgentRun / emitAgentRun) builds the
    telemetry row once so the serial and parallel paths emit identically."""
    src = _implement_src()
    assert ("postAgentRun" in src or "emitAgentRun" in src
            or "agentRunRow" in src), (
        "implement.js has no agent-run emitter helper — serial and parallel "
        "paths cannot share one row shape without it"
    )


def test_emitter_carries_required_telemetry_fields():
    """The emitted row must carry run_id, agent_id, role, step, model,
    timing, ok, gate_state, and a validation/verdict summary — the columns
    the agent_runs table persists."""
    src = _implement_src()
    for field in ("run_id", "agent_id", "role", "step", "model",
                  "gate_state"):
        assert field in src, (
            f"implement.js telemetry emit does not carry {field!r}"
        )
    # Timing: duration_ms (or started/ended) must be threaded.
    assert ("duration_ms" in src or "started_at" in src), (
        "implement.js telemetry emit carries no timing field"
    )
    # The validation/verdict summary from the step result feeds the row.
    assert ("verdict_summary" in src or "validation" in src), (
        "implement.js telemetry emit carries no validation/verdict summary"
    )


def test_emitter_threads_plumbed_session_id_as_session_id():
    """The emitter reuses the already-plumbed SID as the telemetry
    session_id — same source as task_link_session / conductor_advance."""
    src = _implement_src()
    # The ingest payload must reference SID for the session_id field.
    assert "session_id" in src and "SID" in src, (
        "implement.js telemetry emit does not thread SID as session_id"
    )


def test_serial_loop_emits_after_each_step():
    """The serial drive loop must call the emitter after each step result
    is recorded — `trace.push(res)` is where the per-step telemetry POST
    belongs.

    RE-ANCHORED (task 682b7e48): this used to locate the loop by
    `src.index("for (let i = startIdx")`, a loop shape that no longer
    exists — the drive is now the server-driven conductor_work job loop
    (`for (let i = 0; i < MAX_JOBS; i++)` at implement.js:660), which
    pushes the result and emits at :681-682. The old index() raised
    ValueError: substring not found. The assertion now anchors to
    `trace.push(res)` itself, which is the actual seam the emit must
    follow, and is therefore loop-shape independent."""
    src = _implement_src()
    # SUPERSEDED ANCHOR (2026-08-01): the loop used to index a client-side
    # ORDER array ("for (let i = startIdx"). The conductor_work rewrite
    # deleted that array - the server hands back the next job - so the pull
    # loop is now bounded by MAX_JOBS. The INVARIANT is unchanged.
    anchor = "trace.push(res)"
    assert anchor in src, "serial drive loop shape changed"
    loop_start = src.index("for (let i = 0; i < MAX_JOBS")
    after = src[src.index(anchor, loop_start) + len(anchor):]
    # The emit must be the NEXT thing that happens to the result, not merely
    # present somewhere in the file (a whole-file check passes on the
    # helper's own definition). Bound the region by the next agent() call
    # rather than a fixed character window - a character window silently
    # drifts the moment a comment is added above the emit.
    nxt = after.find("await agent(")
    window = after if nxt == -1 else after[:nxt]
    assert ("postAgentRun" in window or "emitAgentRun" in window
            or "/api/agent-runs/ingest" in window), (
        "serial loop does not emit an agent-run telemetry row after each "
        f"step's agent() returns; window after trace.push(res): {window!r}"
    )


def test_parallel_fanout_wrapper_emits_same_shape():
    """A parallel fanout wrapper must emit the SAME telemetry row shape as
    the serial path — no telemetry gap between serial and parallel
    execution. The wrapper must route through the SAME emitter helper."""
    src = _implement_src()
    # A fanout/parallel wrapper exists...
    assert ("fanout" in src.lower() or "Promise.all" in src
            or "parallel" in src.lower()), (
        "implement.js has no parallel fanout wrapper — the task requires "
        "serial AND parallel execution to emit identically"
    )
    # ...and it shares the emitter (no second, divergent POST shape).
    emitter = ("postAgentRun" if "postAgentRun" in src
               else "emitAgentRun" if "emitAgentRun" in src else None)
    assert emitter is not None, "no shared emitter helper to reuse"
    assert src.count(emitter + "(") >= 2, (
        "the shared emitter is called fewer than twice — serial and "
        "parallel paths must BOTH route through it for identical shape"
    )
