"""Task 8fbd5cf0 — a task moves on the canvas in real time (walking skeleton).

RED BY CONSTRUCTION. At the base commit 0c896ff9 none of the following
exists: `prism_service/services/flow_run_recorder.py`, the `flow_node_runs`
table, the `flow.node` bus/SSE event type, or a
`GET /api/workflows/{workflow_id}/runs` route. `conductorLivePhase`
(prism_service/web/src/pages/WorkflowsPage.tsx:579-590) computes
`Math.min(0.97, inStepS / typical)` from `Date.now()`, which the task's
stop_if forbids outright.

WHAT THIS FILE PINS (the contract the build must satisfy):

  flow_run_recorder.record_node_execution(scores_db, row, project="")
      -> dict     writes ONE flow_node_runs row and publishes ONE
                  "flow.node" bus event. `flow_version` is always a real
                  int, never None.
  flow_run_recorder.runs_for_task(scores_db, task_id, workflow_id="conductor")
      -> list     reads the STORED rows. Never re-runs a check.
  flow_run_recorder.progress_source(scores_db, task_id, step, project="")
      -> dict     {"basis": "teeth"|"work_units", "done": int,
                  "total": int|None}. Counted units only — no clock.
  flow_run_recorder.gate_teeth(project, task_id, step)
      -> list     the SAME teeth /node-status already reports.
  flow_run_recorder.CONDUCTOR_NODES
      -> tuple    the canvas node ids, terminating in "land" (bot.json's own
                  declared terminal behaviorId) AFTER green_gate, WITHOUT
                  touching models.workflow.WORKFLOW_STEPS.

Both real decision sites construct the recorder: api/conductor_flow.py's
flow_report and services/gate_adjudicator.py's sweep_once. Neither is in
control_plane.POLICY_FILES.
"""

from __future__ import annotations

import ast
import json
import re
import sqlite3
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
_SRC = _SERVICE_ROOT / "prism_service"
_WORKFLOWS_TSX = _SRC / "web" / "src" / "pages" / "WorkflowsPage.tsx"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _fn(path: Path, name: str) -> ast.FunctionDef:
    """The named function's AST node — so an assertion reads the CODE and a
    comment mentioning the call can never satisfy it."""
    tree = ast.parse(_read(path))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == name:
            return node
    raise AssertionError(f"{path.name}: no function named {name}")


def _calls(node: ast.AST) -> set[str]:
    out: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            f = sub.func
            if isinstance(f, ast.Name):
                out.add(f.id)
            elif isinstance(f, ast.Attribute):
                out.add(f.attr)
    return out


@pytest.fixture()
def recorder():
    """HARD import — never importorskip. A missing module must FAIL this
    suite (rc=1), which is exactly the red the red_gate anchors on."""
    from prism_service.services import flow_run_recorder
    return flow_run_recorder


@pytest.fixture()
def scores_db(tmp_path):
    return str(tmp_path / "scores.db")


def _row(**over) -> dict:
    row = {"task_id": "8fbd5cf0", "workflow_id": "conductor",
           "node_id": "draft_story", "actor": "conductor-adjudicator",
           "outcome": "pass", "reason": "story rubric scored 7/7",
           "started_at": "2026-08-30T10:00:00Z",
           "ended_at": "2026-08-30T10:00:41Z"}
    row.update(over)
    return row


# ---------------------------------------------------------------------------
# AC-1 — an advance writes ONE row, and flow_version is a real int.
# RED today: `grep -rc flow_node_runs .` = 0 across the worktree.
# ---------------------------------------------------------------------------

def test_ac1_a_recorded_node_execution_carries_an_integer_flow_version(
        recorder, scores_db):
    stored = recorder.record_node_execution(scores_db, _row())

    assert isinstance(stored["flow_version"], int), stored
    assert stored["flow_version"] >= 1

    conn = sqlite3.connect(scores_db)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM flow_node_runs WHERE task_id = ?", ("8fbd5cf0",))]
    conn.close()
    assert len(rows) == 1
    got = rows[0]
    assert got["node_id"] == "draft_story"
    assert got["actor"] == "conductor-adjudicator"
    assert got["outcome"] == "pass"
    assert got["reason"] == "story rubric scored 7/7"
    assert got["started_at"] == "2026-08-30T10:00:00Z"
    assert got["ended_at"] == "2026-08-30T10:00:41Z"
    assert isinstance(got["flow_version"], int)
    assert got["run_id"]


# ---------------------------------------------------------------------------
# AC-2 — BOTH real decision sites construct the recorder. The machine sweep
# never passes through flow_report, so one call site is not the set.
# stop_if: "No production code path constructs the recorder".
# RED today: `grep -rc record_node_execution .` = 0.
# ---------------------------------------------------------------------------

def test_ac2_flow_report_and_the_machine_sweep_both_record(recorder):
    flow = _SRC / "api" / "conductor_flow.py"
    adj = _SRC / "services" / "gate_adjudicator.py"

    assert "record_node_execution" in _calls(_fn(flow, "flow_report")), (
        "api/conductor_flow.py:flow_report decides agent advances AND "
        "session-reported gates; it must record each concluded node")
    assert "record_node_execution" in _calls(_fn(adj, "sweep_once")), (
        "services/gate_adjudicator.py:sweep_once is the machine gate seat "
        "and never passes through flow_report — an unrecorded sweep is the "
        "whole reason the canvas reverse-maps history rows today")

    from prism_service.services import control_plane
    policy = {Path(p).name for p in control_plane.POLICY_FILES}
    assert "flow_run_recorder.py" not in policy
    assert "conductor_flow.py" not in policy
    assert "gate_adjudicator.py" not in policy


def test_ac2_two_seats_recording_the_same_task_append_two_rows(
        recorder, scores_db):
    recorder.record_node_execution(scores_db, _row(node_id="story_gate"))
    recorder.record_node_execution(
        scores_db, _row(node_id="green_gate", actor="owner"))
    runs = recorder.runs_for_task(scores_db, "8fbd5cf0")
    assert [r["node_id"] for r in runs] == ["story_gate", "green_gate"]
    assert [r["actor"] for r in runs] == ["conductor-adjudicator", "owner"]
    assert all(isinstance(r["flow_version"], int) for r in runs)


# ---------------------------------------------------------------------------
# AC-3 — GET /api/workflows/{workflow_id}/runs?task_id= returns a run whose
# flow_version is a real integer and whose last node is the shipped one.
# RED today: only /{workflow_id}/runs/history exists and it 404s for
# anything but workflow_id=validation (api/workflows.py:1356-1361).
# ---------------------------------------------------------------------------

def test_ac3_the_runs_route_exists_for_the_conductor_and_is_not_validation_only():
    from prism_service.api import workflows as wf

    paths = {(r.path, tuple(sorted(r.methods or ())))
             for r in wf.router.routes}
    assert ("/{workflow_id}/runs", ("GET",)) in paths, sorted(
        p for p, _m in paths)

    src = _read(_SRC / "api" / "workflows.py")
    tree = ast.parse(src)
    handler = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call) and dec.args and \
                        isinstance(dec.args[0], ast.Constant) and \
                        dec.args[0].value == "/{workflow_id}/runs" and \
                        isinstance(dec.func, ast.Attribute) and \
                        dec.func.attr == "get":
                    handler = node
    assert handler is not None
    body = _calls(handler)
    assert "runs_for_task" in body, (
        "the read path serves STORED rows; it must not reverse-map "
        "task_history the way /{workflow_id}/instances does")
    assert "_run_one_check" not in body and "run_all" not in body, (
        "stop_if: a node panel recomputes a check instead of reading the "
        "stored execution")


# ---------------------------------------------------------------------------
# AC-3b / oracle — the flow carries a TERMINAL node AFTER green_gate, and it
# is NOT bolted onto the FSM enum (likely_misfire: editing WORKFLOW_STEPS in
# conductor_service/models is a POLICY change that fails the
# candidate-controls-judge tooth).
#
# SUPERSEDED 2026-08-30: SHIPPED_NODE used to be the literal string
# "shipped", which matches NO node bot.json actually declares -- a real
# ship could never paint the canvas's own drawn "land" bot node (the
# pipeline's real, 13th behaviorId) as reached. Renamed to "land" so the
# recorded terminal node and the visible one are the same id; every
# assertion below moved with it.
# ---------------------------------------------------------------------------

def test_ac3b_the_canvas_flow_ends_on_land_without_touching_the_fsm_enum(
        recorder):
    from prism_service.models.workflow import WORKFLOW_STEPS

    nodes = list(recorder.CONDUCTOR_NODES)
    assert nodes[-1] == "land"
    assert nodes.index("green_gate") < nodes.index("land")
    for step in WORKFLOW_STEPS:
        assert step["id"] in nodes, step["id"]

    assert "land" not in {s["id"] for s in WORKFLOW_STEPS}, (
        "the terminal node is drawn from the real ship record; adding it to "
        "WORKFLOW_STEPS edits a control_plane.POLICY_FILES module")


def test_ac3b_a_task_that_reaches_land_stays_on_the_board_as_finished(
        recorder, scores_db):
    """stop_if: 'A task that reaches the terminal node disappears from the
    board instead of showing as finished'."""
    recorder.record_node_execution(scores_db, _row(node_id="green_gate"))
    recorder.record_node_execution(
        scores_db, _row(node_id="land", actor="ship-worker",
                        outcome="pass", reason="landed on origin/main"))
    runs = recorder.runs_for_task(scores_db, "8fbd5cf0")
    assert runs[-1]["node_id"] == "land"
    assert recorder.is_finished(runs) is True
    assert recorder.is_visible(runs) is True, (
        "arriving at land is the win state — the row must stay visible, "
        "not drop out the way managed_tasks drops every done row")


# ---------------------------------------------------------------------------
# AC-4 — the movement is STREAMED and the token ANIMATES along the edge.
# RED today: `grep -c "flow.node" routes/sse.py` = 0.
# stop_if: "The token jumps between nodes instead of animating along the edge".
# ---------------------------------------------------------------------------

def test_ac4_recording_publishes_one_flow_node_event_that_sse_forwards(
        recorder, scores_db, monkeypatch):
    from prism_service import events
    seen: list[dict] = []
    monkeypatch.setattr(events.bus, "publish", lambda e: seen.append(e))

    recorder.record_node_execution(
        scores_db, _row(node_id="verify_plan"), project="prism")

    flow_events = [e for e in seen if e.get("type") == "flow.node"]
    assert len(flow_events) == 1, seen
    ev = flow_events[0]
    assert ev["project"] == "prism"
    assert ev["task_id"] == "8fbd5cf0"
    assert ev["node_id"] == "verify_plan"
    assert isinstance(ev["flow_version"], int)

    from prism_service.routes import sse
    assert "flow.node" in sse._WORK_EVENT_TYPES, (
        "the canvas subscribes to the EXISTING bus; an event the work "
        "stream filters out never reaches the page")


def test_ac4_the_canvas_plays_the_token_along_the_wire_on_a_flow_node_event():
    src = _read(_WORKFLOWS_TSX)
    assert "flow.node" in src, (
        "WorkflowsPage must subscribe to the flow.node stream — no reload")
    assert re.search(r"animateMotion|animateTokenAlong", src), (
        "the token must travel the drawn wire path; a re-render at the new "
        "node is the teleport the stop_if forbids")


# ---------------------------------------------------------------------------
# AC-5 — NO progress value derives from a clock. This is the first stop_if.
# RED today: conductorLivePhase computes Math.min(0.97, inStepS / typical)
# from Date.now() and step.average_duration_seconds.
# ---------------------------------------------------------------------------

def _conductor_live_phase_block(src: str) -> str:
    start = src.index("const conductorLivePhase")
    end = src.index("const conductorLiveActivity", start)
    return src[start:end]


def test_ac5_the_live_tile_progress_reads_no_clock():
    block = _conductor_live_phase_block(_read(_WORKFLOWS_TSX))
    for banned in ("Date.now()", "average_duration_seconds", "typical",
                   "in_step_s", 'basis: "time"'):
        assert banned not in block, (
            f"conductorLivePhase still derives progress from {banned!r} — "
            "stop_if: any progress value derives from elapsed time")


def test_ac5_progress_source_never_reads_a_clock(recorder):
    fn = _fn(_SRC / "services" / "flow_run_recorder.py", "progress_source")
    called = _calls(fn)
    for banned in ("time", "now", "utcnow", "monotonic", "perf_counter"):
        assert banned not in called, (
            f"progress_source calls {banned}() — a counted unit never needs "
            "a clock")
    text = ast.unparse(fn)
    assert "elapsed" not in text and "typical" not in text


# ---------------------------------------------------------------------------
# AC-6 — the bar reports REAL work: teeth decided/total for a gate node,
# the drive heartbeat's own work_units for an agent node.
# RED today: `grep -c work_units WorkflowsPage.tsx` = 0.
# ---------------------------------------------------------------------------

def test_ac6_a_gate_node_bar_counts_teeth_decided_over_teeth_total(
        recorder, scores_db, monkeypatch):
    teeth = [{"id": "rubric", "status": "passed"},
             {"id": "ac_ids", "status": "passed"},
             {"id": "diagram", "status": "failed"},
             {"id": "design_packet", "status": "unknown"}]
    monkeypatch.setattr(recorder, "gate_teeth",
                        lambda project, task_id, step: teeth)

    got = recorder.progress_source(scores_db, "8fbd5cf0", "plan_gate",
                                   project="prism")
    assert got == {"basis": "teeth", "done": 3, "total": 4}, got


def test_ac6_gate_teeth_come_from_the_registry_node_status_already_uses():
    fn = _fn(_SRC / "services" / "flow_run_recorder.py", "gate_teeth")
    text = ast.unparse(fn)
    assert re.search(r"plan_gate_checks|node_status|NODE_CHECKS|run_all", text), (
        "no second teeth registry — read the one /node-status reports")


def test_ac6_an_agent_node_bar_counts_the_drive_heartbeats_work_units(
        recorder, scores_db):
    """No history yet for this node -- an honest indeterminate state (no
    total), never a fabricated percentage, while still showing the
    heartbeat's own real climbing count."""
    from prism_service.services import drive_heartbeat
    drive_heartbeat.record_heartbeat(scores_db, {
        "task_id": "8fbd5cf0", "step": "implement_tasks", "elapsed_s": 412,
        "last_tool": "Edit", "work_units": 17})

    got = recorder.progress_source(scores_db, "8fbd5cf0", "implement_tasks",
                                   project="prism")
    assert got["basis"] == "work_units"
    assert got["done"] == 17
    assert got["total"] is None, got
    assert "in_step_s" not in got and "typical_s" not in got


def test_ac6_an_agent_node_with_history_fills_against_its_own_wall_time(
        recorder, scores_db):
    """SUPERSEDED 2026-08-30 answers the plan's own open design point --
    owner: 'progress bars based on historical durations against true wall
    time ... like a real factory game, where the factory makes tasks.' Once
    THIS node (implement_tasks) has concluded before, its own past runs'
    MEDIAN duration is the total, and the bar fills against the heartbeat's
    own already-measured elapsed_s. Never a duration borrowed from another
    node (stop_if), never a fabricated percentage, never a shared typical_s."""
    for i, ended in enumerate((
            "2026-08-30T09:01:40Z",  # 100s
            "2026-08-30T09:03:20Z",  # 200s
            "2026-08-30T09:05:00Z",  # 300s
    )):
        recorder.record_node_execution(scores_db, _row(
            task_id=f"other-{i}", node_id="implement_tasks",
            started_at="2026-08-30T09:00:00Z", ended_at=ended))

    from prism_service.services import drive_heartbeat
    drive_heartbeat.record_heartbeat(scores_db, {
        "task_id": "8fbd5cf0", "step": "implement_tasks", "elapsed_s": 150,
        "last_tool": "Edit", "work_units": 9})

    got = recorder.progress_source(scores_db, "8fbd5cf0", "implement_tasks",
                                   project="prism")
    assert got["basis"] == "wall_time", got
    assert got["total"] == 200.0, got   # median of 100/200/300 -- THIS node only
    assert got["done"] == 150.0, got    # the heartbeat's own measured seconds
    assert 0 < got["done"] / got["total"] < 1


def test_ac6_the_canvas_renders_those_two_counted_sources():
    src = _read(_WORKFLOWS_TSX)
    assert "work_units" in src
    assert re.search(r"teeth|node-status", src)


# ---------------------------------------------------------------------------
# AC-7 — a concluded node is READ, never recomputed. The record is what the
# node said at decision time, even when the live check now answers
# differently. stop_if: "A node panel recomputes a check instead of reading
# the stored execution".
# ---------------------------------------------------------------------------

def test_ac7_a_concluded_node_reads_the_stored_verdict_after_the_check_flips(
        recorder, scores_db, monkeypatch):
    recorder.record_node_execution(scores_db, _row(
        node_id="red_gate", actor="conductor-adjudicator", outcome="pass",
        reason="pinned suite failed at 2aef5dfd (rc=1) as required"))

    first = recorder.runs_for_task(scores_db, "8fbd5cf0")[-1]

    def _flipped(*a, **kw):
        raise AssertionError(
            "the read path re-ran a live check for a concluded node")

    monkeypatch.setattr(recorder, "gate_teeth", _flipped)
    from prism_service.services import plan_gate_checks
    monkeypatch.setattr(plan_gate_checks, "run_all", _flipped, raising=False)

    second = recorder.runs_for_task(scores_db, "8fbd5cf0")[-1]

    assert second == first
    assert second["outcome"] == "pass"
    assert second["reason"].startswith("pinned suite failed at 2aef5dfd")
    assert second["actor"] == "conductor-adjudicator"
    assert second["started_at"] == "2026-08-30T10:00:00Z"
    assert second["ended_at"] == "2026-08-30T10:00:41Z"


def test_ac7_the_node_panel_reads_runs_not_node_status():
    src = _read(_WORKFLOWS_TSX)
    assert "/runs?task_id=" in src or "workflows/conductor/runs" in src, (
        "clicking a finished node must fetch the stored run record")
