"""/workflows must open on the work happening NOW, not on a parked task.

Task a928f3d5 (child of 0b5dd37c). Measured live on 7.13.150: opening
/workflows showed a WorkflowCore run from 32 hours earlier and the label
LOADING RUN, while 31 tasks were in progress (8 in implement_tasks, 9 in
verify_plan). Cause: the auto-attach effect picked a task whose
`gate_state` was "pending" -- a task parked WAITING FOR A PERSON, not one
working -- and `openConductorInstance` then set `viewingInstanceRef`,
which stops the definition poll from re-applying live occupancy to the
board. One parked task froze the whole canvas.

The SPA has no JS test runner, so this pins the actual TSX source, the
convention used by tests/unit/test_conductor_page_animated_cleanup_ui.py.
"""

from __future__ import annotations

from pathlib import Path

_SRC = (Path(__file__).resolve().parent.parent.parent
        / "prism_service" / "web" / "src")
_PAGE = _SRC / "pages" / "WorkflowsPage.tsx"


def _auto_attach_effect() -> str:
    """The effect that attaches the canvas to a live task on load.

    Anchored on its own comment marker rather than a line window, so a
    comment added above it cannot silently push the real code out of view
    (the repo's own lesson about fixed character windows).
    """
    src = _PAGE.read_text(encoding="utf-8")
    marker = "if (!isStateMachineWorkflow || workflowRun || searchParams.get(\"task\")) return;"
    assert marker in src, "auto-attach effect not found; did the guard change?"
    start = src.index(marker)
    end = src.index("openConductorInstance(live)", start)
    return src[start:end]


def test_auto_attach_never_selects_a_task_parked_at_a_gate():
    """A task at gate_state 'pending' is waiting for a HUMAN. Attaching to
    it pins the board to a task that is doing nothing, and (via
    viewingInstanceRef) stops live occupancy for every other task."""
    effect = _auto_attach_effect()
    assert "gate_state" not in effect, (
        "the auto-attach selector still matches on gate_state; a task parked "
        "at a gate is waiting for a person, not working, and attaching to it "
        "freezes the whole board off live occupancy")


def test_auto_attach_selects_a_task_that_is_actually_working():
    """The positive half: it must still attach when real work is in
    flight, or the board never plays a live drive at all."""
    effect = _auto_attach_effect()
    assert 'activity?.state === "working"' in effect
    assert 'activity?.state === "driving"' in effect


def test_live_occupancy_is_the_default_view_when_nothing_is_working():
    """With no instance open the definition poll re-applies the board's
    real occupancy -- the honest whole-board view."""
    src = _PAGE.read_text(encoding="utf-8")
    assert "if (selected && !viewingInstanceRef.current) {" in src, (
        "the live-occupancy branch guard changed; the board would no longer "
        "fall back to whole-board occupancy")
