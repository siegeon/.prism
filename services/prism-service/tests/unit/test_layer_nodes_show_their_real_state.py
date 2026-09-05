"""A drilled-in behaviour layer must PAINT the state its checks report.

7.13.181 shipped the data: GET /api/workflows/{id}/node-status answers each
node of a gate behaviour for one task as passed, refused, not_reached or
unknown. The canvas was still wired only to `workflowRun.runtime`, which is
null on every behaviour layer (no WorkflowCore run backs a declarative FSM
behaviour), so it kept drawing a dead diagram -- owner 2026-08-29: "there is
no indication anywhere what the hell is going on."

The PRISM SPA has NO JS test runner, so these acceptance criteria are pinned
by asserting the ACTUAL TSX/TS source. Blocks are extracted by brace depth,
never by a fixed character window -- a slice around a match silently drifts
the moment a comment grows above the code it guards.
"""
from __future__ import annotations

from pathlib import Path

_WEB = (Path(__file__).resolve().parent.parent.parent
        / "prism_service/web/src")
_PAGE = _WEB / "pages/WorkflowsPage.tsx"
_GRAPH = _WEB / "live/workflowGraph.ts"


def _block(src: str, anchor: str) -> str:
    """The full body of the declaration that starts at `anchor`.

    Reads from the anchor to the brace that closes it, counting depth, so
    the block grows and shrinks with the real code.
    """
    start = src.index(anchor)
    open_at = src.index("{", start)
    depth = 0
    for i in range(open_at, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    raise AssertionError(f"unbalanced braces after {anchor!r}")


def _code_only(block: str) -> str:
    """The block with its comments removed.

    A source-reading assertion that a word is ABSENT must never be decided
    by prose: the comment explaining why there is no progress fill contains
    the word "progress". Strip `//` lines and `/* */` spans first.
    """
    import re
    without_blocks = re.sub(r"/\*.*?\*/", "", block, flags=re.S)
    return "\n".join(
        re.sub(r"//.*$", "", line) for line in without_blocks.splitlines())


def test_a_drilled_layer_asks_the_server_for_its_real_node_state():
    src = _PAGE.read_text(encoding="utf-8")
    assert "/node-status" in src, (
        "the layer canvas never asks for the per-node verdicts, so it can "
        "only draw a dead diagram")
    # The question needs BOTH a behaviour layer and a task: node-status
    # answers per node FOR ONE TASK.
    assert 'searchParams.get("task")' in src
    assert "workflowRun?.data.conductorTask?.id" in src, (
        "a layer drilled into from an attached conductor instance carries no "
        "?task= param, so it would have no task context at all")
    assert "selectedWorkflow?.parent_id" in src, (
        "node-status is a BEHAVIOUR-layer question; the guard must name the "
        "nesting that makes a workflow a layer")


def test_the_canvas_is_handed_the_verdicts():
    # SUPERSEDED 2026-08-30 (task 8fbd5cf0): the raw `nodeVerdicts` state
    # only ever populated for a drilled-in CHILD behaviour layer
    # (nodeStatusLayerId requires selectedWorkflow.parent_id) -- the
    # top-level conductor canvas, the actual walking skeleton, painted no
    # persistent trail on a passed node at all. drawWorkflows now reads
    # `effectiveNodeVerdicts`, which merges the SAME `nodeVerdicts` this
    # test already pins with a second source built from flowRuns.runs (the
    # recorded flow_node_runs rows) for the top-level canvas. The surviving
    # invariant -- verdicts reach drawWorkflows and the draw effect re-runs
    # when they change -- is asserted against the merged value instead.
    src = _PAGE.read_text(encoding="utf-8")
    # Task ce471e06 appended a `runView` argument after the verdicts, so the
    # call no longer ends there. The invariant is unchanged: the verdicts are
    # handed to drawWorkflows, positionally after activeProgress.
    assert "activeProgress, effectiveNodeVerdicts" in src, (
        "the verdicts never reach drawWorkflows, so nothing is painted")
    # ce471e06 added runView/runTrace/runMotionSeconds after it in the same
    # dep array, so it is no longer the last entry. The invariant is
    # unchanged: the verdicts are a dependency of the draw effect.
    assert "workflows, effectiveNodeVerdicts" in src, (
        "the draw effect does not re-run when the verdicts arrive")
    assert "...(nodeVerdicts ?? {}) }" in src, (
        "the drilled-in-layer node-status verdicts this test's sibling "
        "pins must still feed the merged value, not be dropped")


def test_a_failed_read_is_not_a_verdict():
    """A stale answer frozen on the canvas is worse than a plain layer."""
    src = _PAGE.read_text(encoding="utf-8")
    assert "if (!cancelled) setNodeVerdicts(null);" in src


def test_a_verdict_never_paints_a_progress_claim():
    """No fill width, no clock, no modulo. 7.13.174/175 removed exactly that.

    An 18-second wall-clock sawtooth (`elapsedSeconds % 18`) once swept a
    node 12%->80% and snapped back forever while nothing happened. A verdict
    is an ANSWER, not a reading, so nothing in its paint may be derived from
    elapsed time or from a fraction of the node's width.
    """
    src = _GRAPH.read_text(encoding="utf-8")
    paint = _code_only(_block(src, "function verdictPaint"))
    for forbidden in ("progress", "elapsed", "%", "Date.now",
                      "performance."):
        assert forbidden not in paint, (
            f"verdictPaint derives its look from {forbidden!r} -- a verdict "
            "must not be a progress claim")
    # Only a check that RAN gets the completion rail.
    assert 'label: "PASSED", rail: true' in paint
    assert 'label: "REFUSED", rail: true' in paint
    assert 'label: "NOT REACHED", rail: false' in paint
    assert 'label: "UNKNOWN", rail: false' in paint


def test_not_reached_does_not_look_like_passed():
    """A node inference never reached must not read as one that succeeded."""
    src = _GRAPH.read_text(encoding="utf-8")
    paint = _block(src, "function verdictPaint")
    passed = [line for line in paint.splitlines() if '"PASSED"' in line][0]
    not_reached = [line for line in paint.splitlines()
                   if '"NOT REACHED"' in line][0]
    assert "dim: false" in passed
    assert "dim: true" in not_reached, (
        "not_reached is drawn at the same weight as a pass it never earned")
    assert "#34d399" in passed and "#34d399" not in not_reached


def test_the_rail_a_ran_check_gets_is_full_width_and_still():
    """It is a completion mark, not a bar that fills."""
    src = _GRAPH.read_text(encoding="utf-8")
    node = _block(src, "function drawNode")
    rail = _code_only(_block(node, "if (verdictLook?.rail)"))
    assert "ctx.fillRect(x + 1, y + 1, w - 2, 3);" in rail, (
        "the verdict rail is not full width, so it reads as partial progress")
    assert "progress" not in rail and "fillWidth" not in rail


def test_a_live_run_still_wins_over_a_verdict():
    src = _GRAPH.read_text(encoding="utf-8")
    node = _block(src, "function drawNode")
    assert "const verdictLook = !active && verdict ? verdictPaint(verdict) : null;" in node


def test_a_refused_node_says_why_in_the_details_panel():
    src = _PAGE.read_text(encoding="utf-8")
    assert "selectedNodeVerdict.reason" in src, (
        "the check already computed a reason; leaving it on the server is "
        "what made the layer unreadable")
    # SUPERSEDED 2026-08-30 (task 8fbd5cf0, AC-7): the exact one-line
    # definition this used to pin unconditionally read the LIVE re-checked
    # nodeVerdicts map, which is exactly what AC-7 forbids for a node that
    # has already concluded ("a node panel recomputes a check instead of
    # reading the stored execution"). selectedNodeVerdict now prefers a
    # stored flow_node_runs record (selectedNodeRun) when one exists, and
    # only a node with NO stored run yet falls back to the live map -- the
    # invariant this test actually cares about (nodeVerdicts still backs
    # the panel for the live/unrecorded case) survives, asserted below.
    assert "nodeVerdicts?.[selectedNodeId] ?? null" in src
