"""A unit travels the belt when a task ACTUALLY advances a step.

Task 0b5dd37c / owner 2026-08-28: "tokens and processing feeding the
workflow ... Factorio". Measured on 7.13.151: the board renders live
occupancy counts, but nothing ever moves between steps. `sendTransition`
exists in live/workflowGraph.ts and injects one visible item onto an FSM
transition -- and no caller ever invokes it from real task movement, so
the only motion the page can produce is ambient bot-to-step traffic.

This pins the seam: WorkflowsPage watches each managed task's
workflow_step, and when a task's step changes it sends exactly one unit
along that transition. Motion therefore MEANS a task advanced; an idle
board stays honestly still.

The SPA has no JS test runner, so this pins the actual TSX source -- the
convention in tests/unit/test_conductor_page_animated_cleanup_ui.py.
"""

from __future__ import annotations

from pathlib import Path

_SRC = (Path(__file__).resolve().parent.parent.parent
        / "prism_service" / "web" / "src")
_PAGE = _SRC / "pages" / "WorkflowsPage.tsx"
_GRAPH = _SRC / "live" / "workflowGraph.ts"

_MARKER = "// BELT: one unit per real step advance"


def _belt_effect() -> str:
    """The effect that turns real task movement into belt units.

    Anchored on its own marker comment, not a line window, so a comment
    added above cannot push the real code out of view.
    """
    src = _PAGE.read_text(encoding="utf-8")
    assert _MARKER in src, (
        "no belt effect in WorkflowsPage.tsx: nothing turns a real task "
        "advance into a moving unit, so the board can never show work "
        "flowing between steps")
    start = src.index(_MARKER)
    return src[start:start + 2000]


def test_the_page_sends_a_unit_when_a_task_changes_step():
    effect = _belt_effect()
    assert "sendTransition(" in effect, (
        "the belt effect must call graph.sendTransition to inject the unit")
    assert "workflow_step" in effect, (
        "the belt must key off each task's own workflow_step, so one unit "
        "means one real advance")


def test_the_belt_compares_against_the_previous_step_per_task():
    """Aggregate occupancy cannot tell WHICH task moved. The effect must
    hold a per-task record of the last step it saw."""
    effect = _belt_effect()
    assert "prevStepsRef" in effect
    assert ".get(" in effect and ".set(" in effect


def test_the_first_observation_never_fires_the_belt():
    """On first paint every task looks 'new'. Firing then would spray the
    board with units for work that did not move."""
    effect = _belt_effect()
    assert "prev === undefined" in effect or "!prev" in effect, (
        "the effect must skip a task it has never seen before")


def test_send_transition_still_exists_to_call():
    graph = _GRAPH.read_text(encoding="utf-8")
    assert "sendTransition(source: string, target: string)" in graph
