"""UI contract: the /workflows canvas must show a daemon-driven conductor
task as genuinely "running", and its node boxes must stay legible at
1440x900.

Observed live (screenshot ui-2-conductor-flow.png): /workflows?workflow=
conductor rendered "No run in progress · last run 9m · passed" while task
a65c66e5-b8a7-44b4-a223-f1342cfaaa14 sat `in_progress` at plan_gate,
gate_state=pending, being driven by the daemon's task runner and its gate
re-swept by conductor-adjudicator every PRISM_GATE_ADJUDICATOR_INTERVAL.
Owner: "real time progress moving items... it just looks exactly the same."
Also reported: the canvas's node boxes are tiny and unreadable at 1440x900.

Root cause of the banner/highlight half: conductor_service.py's
activity_for reports "awaiting_gate" (never "working"/"driving") for a task
standing at a `*_gate` step with gate_state pending/failed -- deliberately,
per its own docstring ("a WAIT for review, not work"). Three surfaces on
WorkflowsPage.tsx each independently asked only `activity.state ===
"working" || "driving"` to decide "is a task genuinely active", so all
three went blind for exactly this state: `liveRunning` (the page-wide
LiveTier feeding the status-line banner), `conductorRowLiveness` (the
directory's per-row live dot), and the canvas's own ambient node-highlight
(the "no instance open" branch of the rAF loop's activeProgress). The
existing `conductorRunGenuinelyActive` helper (for a single selected
run's conductorTask) already got this right by also treating gate_state
pending/failed as active -- the same test conductorPillTone's own fuchsia-
pulse branch already applies to a rail pill. The fix threads one shared
`conductorTaskGenuinelyActive(task: ManagedTask)` helper through all three
board-wide call sites instead of leaving them to drift.

Convention (test_conductor_page_animated_cleanup_ui.py, test_workflows_
section_ui.py): the PRISM SPA has no JS test runner, so UI-first
acceptance criteria are pinned by asserting the ACTUAL TSX source, parsed
by brace-balanced function body / comment-stripped substring match -- never
a fixed character window, which an explanatory comment above the real code
has been shown to satisfy instead (lesson e139295d).
"""

from __future__ import annotations

import re
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
_WEB = _SERVICE_ROOT / "prism_service" / "web" / "src"
_PAGE = _WEB / "pages" / "WorkflowsPage.tsx"
_GRAPH = _WEB / "live" / "workflowGraph.ts"


def _strip_comments(src: str) -> str:
    # Line comments first: a line comment can itself contain a literal
    # "/*" (e.g. a path wildcard), which a block-comment pass run first
    # would misread as a real opener and eat the rest of the file.
    src = re.sub(r"//[^\n]*", "", src)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    return src


def _read(path: Path) -> str:
    assert path.exists(), f"expected {path} to exist"
    return _strip_comments(path.read_text(encoding="utf-8"))


def _function_body(src: str, signature: str) -> str:
    """Everything between `signature`'s opening `{` and its matching
    closing `}`, counted by brace depth. `src` must already be comment-
    stripped (see _read)."""
    idx = src.find(signature)
    assert idx != -1, f"{signature!r} not found in source"
    brace_start = src.find("{", idx)
    assert brace_start != -1, f"no body opened after {signature!r}"
    depth = 0
    for i in range(brace_start, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[brace_start:i + 1]
    raise AssertionError(f"unbalanced braces after {signature!r}")


def _statement_after(src: str, marker: str) -> str:
    """The single statement (up to the next top-level `;`) that assigns
    the name bound at `marker`, e.g. "const liveRunning ="."""
    idx = src.find(marker)
    assert idx != -1, f"{marker!r} not found in source"
    end = src.find(";", idx)
    assert end != -1, f"no terminating ';' found after {marker!r}"
    return src[idx:end + 1]


# ---------------------------------------------------------------------------
# A shared "genuinely active" predicate exists and honors gate_state, not
# just activity.state — the exact clause conductor_service.py's
# awaiting_gate branch requires.
# ---------------------------------------------------------------------------

def test_a_shared_managed_task_liveness_helper_exists():
    src = _read(_PAGE)
    assert "function conductorTaskGenuinelyActive(" in src, (
        "WorkflowsPage.tsx must define ONE shared helper answering "
        "'is this ManagedTask genuinely being worked right now' -- three "
        "separate call sites independently re-deriving activity.state === "
        "'working' || 'driving' is exactly how the awaiting_gate blind spot "
        "was introduced three times over"
    )


def test_the_liveness_helper_treats_a_pending_or_failed_gate_as_active():
    # UPDATED 2026-09-13 (screenshot workflows-342.png, task 4c9b39e5): the
    # single conductorTaskGenuinelyActive boolean this test used to pin
    # DIRECTLY was too coarse -- it read a task blocked at green_gate with
    # NO live heartbeat as "genuinely active", and the status-line banner
    # then said "Driving 4c9b39e5 ... prism-task-runner" for a seat that
    # was not actually beating. The gate_state/activity checks now live in
    # two disjoint helpers (conductorTaskDriving, conductorTaskWaitingAtGate)
    # that conductorTaskGenuinelyActive ORs together -- this test follows
    # the checks to their new homes instead of one flat function body.
    src = _read(_PAGE)
    driving_body = _function_body(src, "function conductorTaskDriving(")
    assert re.search(
        r'task\.activity\?\.state\s*===\s*"working"\s*\|\|\s*task\.activity\?\.state\s*===\s*"driving"',
        driving_body,
    ), "conductorTaskDriving must be the honest live-heartbeat check"
    waiting_body = _function_body(src, "function conductorTaskWaitingAtGate(")
    assert re.search(
        r'task\.gate_state\s*===\s*"pending"\s*\|\|\s*task\.gate_state\s*===\s*"failed"',
        waiting_body,
    ), (
        "conductorTaskWaitingAtGate must treat gate_state pending/failed as "
        "a real (if not currently driven) gate fact -- the gate adjudicator "
        "seat re-sweeps it even with nobody mid-step"
    )
    assert "!conductorTaskDriving(task)" in waiting_body, (
        "conductorTaskWaitingAtGate must exclude a task a live heartbeat is "
        "already driving -- that case is 'Driving', never 'Waiting'"
    )
    active_body = _function_body(src, "function conductorTaskGenuinelyActive(")
    assert "conductorTaskDriving(task)" in active_body and "conductorTaskWaitingAtGate(task)" in active_body, (
        "conductorTaskGenuinelyActive (the board-wide occupancy fact used by "
        "the row dot and ambient highlight) must OR the two disjoint facts "
        "together, not re-derive its own gate_state/activity check"
    )


# ---------------------------------------------------------------------------
# The three board-wide call sites route through the shared helper, not a
# re-derived working/driving-only check.
# ---------------------------------------------------------------------------

def test_live_running_uses_the_shared_liveness_helper():
    src = _read(_PAGE)
    stmt = _statement_after(src, "const liveRunning =")
    assert "conductorTaskGenuinelyActive(task)" in stmt, (
        "liveRunning (the page-wide LiveTier input that drives the status-"
        "line banner) must ask conductorTaskGenuinelyActive, not a bare "
        "activity.state working/driving check -- otherwise a task the "
        "daemon is actively driving through a pending gate renders the "
        "banner as 'No run in progress'"
    )
    assert '"working" || task.activity?.state === "driving"))' not in stmt, (
        "liveRunning must not fall back to the old narrow working/driving-"
        "only predicate inline"
    )


def test_directory_row_liveness_uses_the_shared_liveness_helper():
    # UPDATED 2026-09-13: conductorRowLiveness is now tri-state ("driving" |
    # "waiting" | undefined) so the dot's colour can tell the two facts
    # apart -- see test_the_directory_dot_distinguishes_driving_from_waiting
    # below. It routes through the same two disjoint helpers as
    # conductorTaskGenuinelyActive rather than that one flattened boolean.
    src = _read(_PAGE)
    body = _function_body(src, "const conductorRowLiveness = useMemo(() => {")
    assert "conductorTaskDriving" in body and "conductorTaskWaitingAtGate" in body, (
        "conductorRowLiveness (the directory's per-row live dot) must ask "
        "both disjoint liveness facts, so a canvas whose only occupant is a "
        "pending-gate task with no heartbeat still shows its dot lit -- in "
        "the waiting colour, not the driving one"
    )


def test_the_directory_dot_distinguishes_driving_from_waiting():
    src = _read(_PAGE)
    assert src.count('dotLive === "waiting"') >= 2, (
        "both directory dot renderers (root rows and renderBranch's nested "
        "rows) must render a distinct 'waiting' colour/title -- a task "
        "parked at a gate with no heartbeat must not pulse the same "
        "'running now' teal dot as a task a real heartbeat is driving "
        "(2026-09-13, screenshot workflows-342.png)"
    )
    assert 'bg-fuchsia-400/70 animate-pulse' in src, (
        "the waiting dot must reuse the SAME fuchsia tone the rail pill "
        "(conductorPillTone) already uses for a pending/failed gate, not a "
        "third, newly-invented colour"
    )


def test_the_ambient_node_highlight_uses_the_shared_liveness_helper():
    src = _read(_PAGE)
    marker = 'isStateMachineWorkflow && !workflowRun && !viewingInstanceRef.current'
    idx = src.find(marker)
    assert idx != -1, "the 'no instance open' ambient-highlight branch was not found"
    # The activeTask lookup immediately follows this branch's condition.
    window = src[idx:idx + 1200]
    assert "const activeTask = conductorManaged" in window, (
        "expected the ambient activeTask lookup shortly after the "
        "'no instance open' branch guard"
    )
    lookup = _statement_after(window, "const activeTask = conductorManaged")
    assert "conductorTaskGenuinelyActive(task)" in lookup, (
        "the canvas's own ambient node-highlight (drawn when no specific "
        "instance is open) must use the shared liveness helper -- this is "
        "the 'which node is highlighted' half of the same defect: a task "
        "genuinely driving through a pending gate must light its node, not "
        "read as idle"
    )


# ---------------------------------------------------------------------------
# The status line names the actual task/step/seat being driven, not a bare
# "running" with no identifying detail.
# ---------------------------------------------------------------------------

def test_the_running_status_line_names_the_driven_task_and_seat():
    src = _read(_PAGE)
    body = _function_body(src, "const statusLineText = useMemo(() => {")
    assert "conductorRailTasks.find(conductorTaskGenuinelyActive)" in body, (
        "the running branch of statusLineText must locate the genuinely-"
        "active conductor task via the shared helper"
    )
    assert re.search(r"`Driving \$\{activeTask\.id\.slice\(0,\s*8\)\}", body), (
        "the banner must name WHICH task is being driven (e.g. "
        "'Driving a65c66e5 · plan_gate · conductor-adjudicator'), not just "
        "'running · plan gate' with no task identity"
    )
    assert "activeTask.activity?.seat" in body, (
        "the banner must include the driving seat (e.g. "
        "conductor-adjudicator) when the task's activity carries one"
    )
    # 2026-09-13 (screenshot workflows-342.png): the "Driving" claim must be
    # gated on a real heartbeat (conductorTaskDriving), and a task merely
    # parked at a gate with none must read as "Waiting", naming who owns
    # the next move instead of a seat that isn't beating.
    assert "if (conductorTaskDriving(activeTask))" in body, (
        "the 'Driving ...' return must be gated behind conductorTaskDriving, "
        "never rendered for a task whose only liveness fact is a pending/"
        "failed gate_state with no heartbeat"
    )
    assert re.search(r"`Waiting at \$\{step\} · \$\{activeTask\.id\.slice\(0,\s*8\)\}", body), (
        "a task waiting at a gate with no live driver must render "
        "'Waiting at <step> · <id8> · ...', never 'Driving'"
    )
    assert '"machine seat next"' in body and '"your review"' in body, (
        "the waiting branch must name WHO owns the next move -- the "
        "machine seat for a machine_only_gate step, the owner otherwise"
    )


# ---------------------------------------------------------------------------
# Node box legibility at 1440x900: the canvas's own auto-fit zoom floor
# must not shrink the 12px node label past readability.
# ---------------------------------------------------------------------------

def test_the_canvas_zoom_floor_is_raised_for_legibility():
    src = _read(_GRAPH)
    assert re.search(r"export const MIN_ZOOM\s*=\s*0\.55", src), (
        "workflowGraph.ts must export a MIN_ZOOM raised well above the old "
        "0.35 floor -- at 0.35, the node card's 12px label rendered at "
        "~4.2px on screen for the conductor canvas's 12-node width at "
        "1440x900, which is what read as 'tiny and unreadable'"
    )
    fit_body = _function_body(src, "fit(w: number, h: number, force = false): void {")
    assert "MIN_ZOOM" in fit_body and "0.35" not in fit_body, (
        "fit()'s own zoom clamp must use the raised MIN_ZOOM constant, not "
        "a hardcoded 0.35 floor"
    )


def test_the_wheel_zoom_out_floor_matches_the_canvas_minimum():
    page_src = _read(_PAGE)
    assert "MIN_ZOOM" in page_src, (
        "WorkflowsPage.tsx must import workflowGraph's MIN_ZOOM"
    )
    onwheel_body = _function_body(
        page_src, "const onWheel = useCallback((ev: React.WheelEvent<HTMLCanvasElement>) => {",
    )
    assert "Math.max(MIN_ZOOM," in onwheel_body, (
        "the wheel handler's zoom-out floor must reuse the same MIN_ZOOM "
        "constant fit() uses -- otherwise a person can wheel-zoom back down "
        "to the old illegible 0.35 size even though the initial fit no "
        "longer does"
    )
    assert "Math.max(0.35," not in onwheel_body, (
        "the wheel handler must not keep its own separate hardcoded 0.35 "
        "floor"
    )
