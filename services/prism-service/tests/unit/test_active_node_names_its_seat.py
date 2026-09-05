"""The active node names the seat working it (task 1c6d59e9).

The run view at /workflows?task=<id> highlights the active node but never
says WHICH SEAT drives it, WHAT that seat last did, or WHEN it last moved.
The owner reported this on 2026-09-04: "i dont see whats doing the work at
the moment, what is actively working".

The data is on file and is discarded on two floors:
  - drive_heartbeat stores ``driver`` (drive_heartbeat.py:46,57) and
    resume_actuator writes it on every dispatch beat
    (services/resume_actuator.py, ``"driver": SEAT``), but the served
    activity heartbeat carries only step/last_tool/elapsed_s/age_s.
  - a beat older than HEARTBEAT_WINDOW_S is emitted as None, so a finished
    driver's payload is byte-identical to a task that never had one.

conductor_service.activity_for is a control_plane.POLICY_FILES entry, so
this slice DOES NOT EDIT IT. The enrichment lands in the seam PRISM already
built for this case: a sibling of api/conductor.py::_with_report_signal
(:72), applied at the same two call sites (api/conductor.py:164 and
api/tasks.py:964-965). Every assertion below therefore reads the SERVED
payload -- activity_for composed with the enrichment -- which is the payload
the AC's own oracle inspects and the payload the node consumes.

Covers AC-1..AC-4 and AC-8 (served payload) and AC-6..AC-8 (the run node).
The UI criteria are pinned by reading the TSX source, because the PRISM SPA
has NO JS test runner (see test_conductor_page_animated_cleanup_ui.py:4-6).
Those assertions strip comments first and resolve the ENCLOSING ternary
branch -- never a fixed character window, and never a comment.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

ADJUDICATOR_SEAT = "conductor-adjudicator"  # gate_adjudicator.py:261

_WEB = Path(__file__).resolve().parents[2] / "prism_service" / "web" / "src"
SDLC = _WEB / "components" / "conductor" / "SdlcProgress.tsx"
WORKFLOWS = _WEB / "pages" / "WorkflowsPage.tsx"


def _src(path: Path) -> str:
    """The TSX with EVERY comment removed. A source-reading assertion that
    can be satisfied by a comment is not an assertion (lesson: the ClaudeAuthCard
    guards passed three times on an explanatory comment)."""
    text = path.read_text(encoding="utf-8")
    out, i, n = [], 0, len(text)
    tick = quote = None
    while i < n:
        c = text[i]
        if quote:
            out.append(c)
            if c == "\\":
                if i + 1 < n:
                    out.append(text[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if tick:
            out.append(c)
            if c == "\\":
                if i + 1 < n:
                    out.append(text[i + 1])
                i += 2
                continue
            if c == "`":
                tick = None
            i += 1
            continue
        if c == "`":
            tick = c
            out.append(c)
            i += 1
            continue
        if c in "\"'":
            quote = c
            out.append(c)
            i += 1
            continue
        if text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i)
            i = n if j < 0 else j + 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _expression(src: str, decl: str) -> str:
    """The FULL right-hand side of ``const <decl> =``, ended at the first
    top-level ``;`` -- string and template literals are tracked, so a ``;``
    inside a rendered label never truncates it."""
    at = src.index(f"const {decl} =") + len(f"const {decl} =")
    i, n = at, len(src)
    tick = quote = None
    while i < n:
        c = src[i]
        if quote:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = None
        elif tick:
            if c == "\\":
                i += 2
                continue
            if c == "`":
                tick = None
        elif c == "`":
            tick = c
        elif c in "\"'":
            quote = c
        elif c == ";":
            return src[at:i]
        i += 1
    raise AssertionError(f"unterminated expression for const {decl}")


def _ternary(decl: str, src: str) -> list:
    """The ternary chain for ``const <decl> =`` split into its ALTERNATIVES.
    Each alternative of this chain begins a line with ``: `` (the file's own
    formatting), so this resolves real branches, never a character window."""
    import re

    expr = _expression(src, decl)
    parts = re.split(r"(?m)^\s*:\s+", expr)
    assert len(parts) > 2, expr
    return [p.strip() for p in parts]


def _branch(branches: list, marker: str) -> str:
    """The ONE alternative whose text contains ``marker``."""
    hits = [b for b in branches if marker in b]
    assert len(hits) == 1, f"{marker!r} matched {len(hits)} branches: {branches}"
    return hits[0]


def _object_literal(src: str, after: str) -> str:
    """The ``return { ... }`` object literal that follows ``after``, resolved
    by BRACE MATCHING from the return, never a fixed window."""
    start = src.index("return {", src.index(after)) + len("return ")
    depth, i = 0, start
    while i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
        i += 1
    raise AssertionError(f"unbalanced object literal after {after!r}")


# --------------------------------------------------------------------------
# AC-6, AC-7 -- the run node
# --------------------------------------------------------------------------

def test_the_run_node_receives_the_live_heartbeat(tmp_path):
    """AC-6: conductorLiveActivity (WorkflowsPage.tsx:691-697) builds its
    object from ONE input, t.gateState, and returns ``{ state }`` and
    nothing else -- so the node is fed no beat at all and cannot name a
    seat however good the payload gets."""
    src = _src(WORKFLOWS)
    obj = _object_literal(src, "const conductorLiveActivity")
    for key in ("heartbeat", "beat", "seat", "dispatch_count"):
        assert key in obj, (key, obj)


def test_the_node_renders_seat_tool_and_beat_age(tmp_path):
    """AC-7 (live half): the active node prints the seat, the last tool and
    the age of the last beat. Today the driving branch prints only
    ``driving · ${hb.last_tool}`` plus time-in-step -- no seat, no beat age."""
    src = _src(SDLC)
    activity_type = _object_literal(
        src.replace("export type Activity = {", "return {"), "return {")
    for key in ("beat", "seat", "dispatch_count"):
        assert key in activity_type, (key, activity_type)

    live = _branch(_ternary("stateLabel", src), 'state === "driving"')
    assert "hb.driver" in live, live
    assert "hb.last_tool" in live, live
    assert "age_s" in live, live


def test_an_old_beat_reads_differently_from_a_live_one(tmp_path):
    """AC-7 (stale half): the node shows the beat GOING STALE instead of
    reading the same as a live one. The old-beat branch must render the
    beat's own facts and must not claim ``driving`` -- a stopped driver that
    still reads as working is this ticket's named misfire."""
    src = _src(SDLC)
    branches = _ternary("stateLabel", src)
    live = _branch(branches, 'state === "driving"')
    stale = [b for b in branches
             if "beat" in b.lower() and b != live
             and 'state === "driving"' not in b]
    assert len(stale) == 1, f"expected ONE old-beat branch, got: {stale}"
    old = stale[0]
    assert "last_tool" in old, old
    assert "age_s" in old, old
    assert "driving" not in old, old
    assert old != live


def _cond(tmp_path):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService

    scores_db = str(tmp_path / "scores.db")
    task_svc = TaskService(str(tmp_path / "tasks.db"), scores_db=scores_db)
    cond = ConductorService(scores_db, enable_engine=False, task_svc=task_svc)
    return cond, task_svc, scores_db


def _old_transition(task_svc, task_id, seconds_ago):
    """A stale advance_task row so task_motion_s reads past the 120s window
    (mirrors test_drive_heartbeat_activity.py's own helper)."""
    ts = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()
    task_svc._db.execute(
        "INSERT INTO task_history (task_id, actor, action, details, timestamp) "
        "VALUES (?, ?, ?, ?, ?)",
        (task_id, "", "advance_task", "to=verify_green_state", ts),
    )
    task_svc._db.commit()


def _age_heartbeat(scores_db, task_id, seconds_ago):
    import sqlite3
    ts = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()
    conn = sqlite3.connect(scores_db)
    conn.execute(
        "UPDATE drive_heartbeats SET last_progress_at = ? WHERE task_id = ?",
        (ts, task_id),
    )
    conn.commit()
    conn.close()


def _task(task_svc, step="verify_green_state"):
    t = task_svc.create(title="long step underway")
    task_svc.update(t.id, status="in_progress", workflow_step=step)
    _old_transition(task_svc, t.id, seconds_ago=600)
    return t


def _served(cond, task_svc, scores_db, task_id):
    """The activity payload AS SERVED by GET /api/tasks/{id}: the real
    activity_for output run through the api/conductor.py enrichment, exactly
    as api/tasks.py:964-965 already does for _with_report_signal."""
    from prism_service.api.conductor import _with_drive_seat

    act = cond.activity_for(task_svc.get(task_id), {"session_quiet_s": 300})
    rows = _with_drive_seat([{"id": task_id, "activity": act}], scores_db, task_svc)
    return rows[0]["activity"]


def _beat(scores_db, task_id, driver, last_tool="pytest", work_units=1,
          step="verify_green_state"):
    from prism_service.services import drive_heartbeat

    out = drive_heartbeat.record_heartbeat(scores_db, {
        "task_id": task_id, "step": step, "elapsed_s": 241,
        "last_tool": last_tool, "work_units": work_units, "driver": driver,
    })
    assert out.get("ok") is True, out
    return out


# --------------------------------------------------------------------------
# AC-1 .. AC-4, AC-8 -- the served payload
# --------------------------------------------------------------------------

def test_activity_heartbeat_names_the_seat(tmp_path):
    """AC-1: the served activity names WHO drives the step. The seat is
    already in the store (drive_heartbeat.latest selects ``driver`` back
    out); today it is read and thrown away."""
    cond, task_svc, scores_db = _cond(tmp_path)
    t = _task(task_svc)
    _beat(scores_db, t.id, driver="prism-task-runner", last_tool="Bash")

    act = _served(cond, task_svc, scores_db, t.id)
    assert act["heartbeat"] is not None, act
    assert act["heartbeat"]["driver"] == "prism-task-runner", act["heartbeat"]
    assert act["seat"] == "prism-task-runner", act


def test_an_old_beat_is_marked_not_dropped(tmp_path):
    """AC-2 (first half): a beat past HEARTBEAT_WINDOW_S survives on the
    payload under ``beat``, MARKED stale, so the node can show the beat
    going stale. ``heartbeat`` stays None for it -- that fresh-only contract
    lives in the policy file and is not moved by this slice."""
    from prism_service.services import drive_heartbeat

    cond, task_svc, scores_db = _cond(tmp_path)
    t = _task(task_svc)
    _beat(scores_db, t.id, driver="prism-resume-actuator",
          last_tool="resume_actuator_dispatch")
    _age_heartbeat(scores_db, t.id, seconds_ago=400)
    assert 400 > drive_heartbeat.HEARTBEAT_WINDOW_S

    act = _served(cond, task_svc, scores_db, t.id)
    assert act["heartbeat"] is None, act
    beat = act["beat"]
    assert beat is not None, act
    assert beat["driver"] == "prism-resume-actuator", beat
    assert beat["last_tool"] == "resume_actuator_dispatch", beat
    assert beat["age_s"] >= 399, beat
    assert beat["stale"] is True, beat


def test_a_task_with_no_beat_returns_null_heartbeat(tmp_path):
    """AC-2 (second half): a task that never had a driver still reads
    ``heartbeat: null`` AND ``beat: null`` -- and its payload is NOT equal
    to the payload of a task whose driver stopped 400s ago. Today those two
    are byte-identical, which is the defect."""
    cond, task_svc, scores_db = _cond(tmp_path)
    stopped = _task(task_svc)
    never = _task(task_svc)
    _beat(scores_db, stopped.id, driver="prism-resume-actuator")
    _age_heartbeat(scores_db, stopped.id, seconds_ago=400)

    act_never = _served(cond, task_svc, scores_db, never.id)
    act_stopped = _served(cond, task_svc, scores_db, stopped.id)

    assert act_never["heartbeat"] is None, act_never
    assert act_never["beat"] is None, act_never
    assert act_never != act_stopped, (act_never, act_stopped)


def test_the_state_word_still_needs_a_fresh_beat(tmp_path):
    """AC-3 (ship-hygiene guard): the ``driving`` boundary is NOT moved by
    this slice. conductor_service.activity_for is a POLICY_FILES entry and
    is absent from allowed_files, so the enrichment may ADD keys and must
    never rewrite ``state``."""
    cond, task_svc, scores_db = _cond(tmp_path)
    fresh = _task(task_svc)
    old = _task(task_svc)
    _beat(scores_db, fresh.id, driver="prism-task-runner")
    _beat(scores_db, old.id, driver="prism-task-runner")
    _age_heartbeat(scores_db, old.id, seconds_ago=400)

    assert _served(cond, task_svc, scores_db, fresh.id)["state"] == "driving"
    assert _served(cond, task_svc, scores_db, old.id)["state"] != "driving"


def test_activity_counts_dispatches_for_the_current_step(tmp_path):
    """AC-4: how many times a seat dispatched THE CURRENT STEP. The row
    already records the step (resume_actuator writes
    ``details="seat=<seat>; step=<step_id>"`` on every DISPATCH_ACTION), so
    this slice reads it and does not change the writer.

    resume_actuator._dispatch_count() is TASK-WIDE and adds 1 for the
    pending dispatch -- against this fixture it answers 5, not 3. It feeds
    drive_heartbeat's monotonic work_units guard and is read for reference,
    never reused here."""
    from prism_service.services import resume_actuator

    cond, task_svc, scores_db = _cond(tmp_path)
    t = _task(task_svc, step="verify_green_state")
    for _ in range(3):
        task_svc.record_history(
            t.id, action=resume_actuator.DISPATCH_ACTION,
            details=f"seat={resume_actuator.SEAT}; step=verify_green_state",
            actor=resume_actuator.SEAT)
    task_svc.record_history(
        t.id, action=resume_actuator.DISPATCH_ACTION,
        details=f"seat={resume_actuator.SEAT}; step=implement_tasks",
        actor=resume_actuator.SEAT)

    act = _served(cond, task_svc, scores_db, t.id)
    assert act["dispatch_count"] == 3, act
    assert resume_actuator._dispatch_count(task_svc, t.id) == 5


def test_a_gate_node_names_its_seat(tmp_path):
    """AC-8 (payload half): a task parked at a gate has no beat, so the seat
    comes from WHO OWNS THE GATE -- the machine seat when it is eligible.
    This is what lets a gate node name a seat with nothing beating."""
    cond, task_svc, scores_db = _cond(tmp_path)
    t = _task(task_svc, step="green_gate")

    act = _served(cond, task_svc, scores_db, t.id)
    assert act["heartbeat"] is None, act
    assert act["beat"] is None, act
    assert act["seat"] == ADJUDICATOR_SEAT, act

    # AC-8 (UI half): the gate branch reads the same fields as the live one.
    expr = _ternary("stateLabel", _src(SDLC))
    gate = _branch(expr, "awaiting_gate")
    assert "seat" in gate, gate
