"""A node's live state must reach the screen in about a second (owner:
"Nodes should be very fast linked to view"), not the catalog poll's own
10s cadence -- GET /api/workflows measured 21-52s under load.

The PRISM SPA has NO JS test runner, so these acceptance criteria are pinned
by asserting the ACTUAL TS/TSX source, same style as
test_layer_nodes_show_their_real_state.py: blocks are extracted by brace
depth, never by a fixed character window.
"""
from __future__ import annotations

from pathlib import Path

_WEB = (Path(__file__).resolve().parent.parent.parent
        / "prism_service/web/src")
_HOOK = _WEB / "lib/useWorkflowLive.ts"
_PAGE = _WEB / "pages/WorkflowsPage.tsx"


def _block(src: str, anchor: str) -> str:
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


def test_the_hook_polls_the_live_route_every_1000ms():
    src = _HOOK.read_text(encoding="utf-8")
    assert "const POLL_MS = 1000;" in src, (
        "the live channel must poll at 1000ms -- a node's live state has "
        "to reach the screen in about a second")
    assert "/api/workflows/live" in src


def test_the_hook_pauses_while_hidden_and_resumes_on_visibilitychange():
    src = _HOOK.read_text(encoding="utf-8")
    tick = _block(src, "const tick = ()")
    assert 'document.visibilityState !== "visible") return;' in tick, (
        "a hidden tab must not keep polling every second in the background")
    listener = _block(src, "const onVisibility = ()")
    assert 'document.visibilityState === "visible"' in listener
    assert "tick();" in listener, (
        "visibilitychange must resume the poll IMMEDIATELY, not wait for "
        "the next tick")
    assert 'addEventListener("visibilitychange", onVisibility)' in src


def test_the_hook_backs_off_after_three_consecutive_failures():
    src = _HOOK.read_text(encoding="utf-8")
    assert "const BACKOFF_MS = 5000;" in src
    assert "const FAILURES_BEFORE_BACKOFF = 3;" in src
    catch_block = _block(src, ".catch(() => {")
    assert "failures += 1;" in catch_block
    assert "failures >= FAILURES_BEFORE_BACKOFF" in catch_block


def test_the_page_overlays_live_data_under_the_same_freeze_guard():
    src = _PAGE.read_text(encoding="utf-8")
    assert 'import { useWorkflowLive' in src
    overlay = _block(src, "const liveWorkflows = useWorkflowLive(project);")
    # Reused VERBATIM from the existing 10s catalog poll -- never a
    # different guard for the fast channel.
    assert ("if (selected && (!viewingInstanceRef.current || "
            "selected.parent_id)) {") in overlay
    assert "graphRef.current.setDef(workflowForGraph(overlaid));" in overlay


def test_the_overlay_only_replaces_occupancy_and_live():
    src = _PAGE.read_text(encoding="utf-8")
    overlay = _block(src, "const liveWorkflows = useWorkflowLive(project);")
    assert "...selected," in overlay, (
        "the overlay must start from the LAST full def already held, not "
        "rebuild structure from scratch")
    assert "occupancy: overlay.occupancy," in overlay
    assert "live: overlay.live," in overlay
