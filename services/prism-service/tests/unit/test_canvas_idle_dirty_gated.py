"""Pins the fix for "canvas idle redraw burns CPU" (task fix/canvasidle).

The PRISM SPA has NO JS test runner, so this asserts the ACTUAL TS/TSX
source -- same convention as test_conductor_page_animated_cleanup_ui.py.

Measured live: a headless Chrome tab sitting on /workflows?workflow=conductor
with nothing changing for 2 minutes held the renderer process at ~36% CPU and
the GPU process at ~29%, 61 requestAnimationFrame callbacks/s, because both
canvas rAF loops (LivePage.tsx, WorkflowsPage.tsx) unconditionally re-armed
themselves at the end of every frame regardless of whether anything on
screen had changed. This pins: (1) GraphState/WorkflowGraph each expose a
hasActiveAnimation() the loop can ask; (2) neither loop's `frame` callback
ends with a bare, unconditional requestAnimationFrame(frame) any more; (3)
each loop checks document.hidden before rescheduling.
"""

from __future__ import annotations

import re
from pathlib import Path

_HERE = Path(__file__).resolve()
_SRC = _HERE.parent.parent.parent / "prism_service" / "web" / "src"
_LIVE_PAGE = _SRC / "pages" / "LivePage.tsx"
_WORKFLOWS_PAGE = _SRC / "pages" / "WorkflowsPage.tsx"
_GRAPH_STATE = _SRC / "live" / "graphState.ts"
_WORKFLOW_GRAPH = _SRC / "live" / "workflowGraph.ts"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_graph_state_exposes_has_active_animation():
    src = _read(_GRAPH_STATE)
    assert "hasActiveAnimation(now: number): boolean" in src, \
        "GraphState must expose a dirty-check the /live rAF loop can ask"


def test_workflow_graph_exposes_has_active_animation():
    src = _read(_WORKFLOW_GRAPH)
    assert "hasActiveAnimation(): boolean" in src, \
        "WorkflowGraph must expose a dirty-check the /workflows rAF loop can ask"


def _frame_loop_body(src: str) -> str:
    # Isolate from the `const frame = (now` (or `frame = (now`) declaration
    # through the matching closing `};` of that arrow function, by brace
    # depth -- robust to the large amount of unrelated code inside it.
    m = re.search(r"const frame = \(now: number\) => \{", src)
    assert m, "expected a `const frame = (now: number) => { ... }` rAF callback"
    start = m.end()
    depth = 1
    i = start
    while depth > 0:
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    return src[start:i]


def test_live_page_loop_is_dirty_gated_not_unconditional():
    body = _frame_loop_body(_read(_LIVE_PAGE))
    # The old bug: draw(...) directly followed by an unconditional rearm.
    assert not re.search(r"draw\(ctx, stateRef\.current, now, versionLabel\);\s*raf = requestAnimationFrame\(frame\);\s*\};", body), \
        "LivePage's frame() must not unconditionally re-arm requestAnimationFrame right after draw()"
    assert "document.hidden" in body, \
        "LivePage's frame() must check document.hidden before rescheduling"
    assert "hasActiveAnimation(now)" in body, \
        "LivePage's frame() must consult GraphState.hasActiveAnimation before choosing 60fps vs. an idle tick"
    assert "window.setTimeout" in body, \
        "LivePage's frame() must drop to a slow (setTimeout-scheduled) tick when idle, not stop dead or spin at 60fps"


def test_workflows_page_loop_is_dirty_gated_not_unconditional():
    body = _frame_loop_body(_read(_WORKFLOWS_PAGE))
    assert not re.search(r"drawWorkflows\([^;]*\);\s*raf = requestAnimationFrame\(frame\);\s*\};", body), \
        "WorkflowsPage's frame() must not unconditionally re-arm requestAnimationFrame right after drawWorkflows()"
    assert "document.hidden" in body, \
        "WorkflowsPage's frame() must check document.hidden before rescheduling"
    assert "graphRef.current.hasActiveAnimation()" in body, \
        "WorkflowsPage's frame() must consult WorkflowGraph.hasActiveAnimation before choosing 60fps vs. an idle tick"
    assert "window.setTimeout" in body, \
        "WorkflowsPage's frame() must drop to a slow (setTimeout-scheduled) tick when idle, not stop dead or spin at 60fps"


def test_both_pages_resume_on_visibility_change():
    for path in (_LIVE_PAGE, _WORKFLOWS_PAGE):
        src = _read(path)
        assert "visibilitychange" in src, \
            f"{path.name} must listen for visibilitychange to resume a stopped loop when the tab becomes visible again"
