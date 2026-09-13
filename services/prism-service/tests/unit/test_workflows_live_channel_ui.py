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


def test_the_hook_reads_the_live_route_via_the_shared_sse_gate():
    # Task fix/polling (SSE coordination round): a node's live state now
    # reaches the screen via a real GET /sse/changes task_changed push
    # (usePolledResource), not a bare 1000ms setTimeout poll -- "very fast
    # linked to view" is satisfied by the push itself, not a fixed clock.
    src = _HOOK.read_text(encoding="utf-8")
    assert "/api/workflows/live" in src
    assert "usePolledResource" in src
    assert "TASK_CHANGED_KINDS" in src
    assert "setTimeout(" not in src
    assert "setInterval(" not in src


def test_the_hook_keeps_its_external_contract_unchanged():
    # The Workflows canvas (owned by a sibling fixer) calls
    # useWorkflowLive(project) and reads a WorkflowLivePayload | null --
    # that contract must survive the poll -> push swap unchanged.
    src = _HOOK.read_text(encoding="utf-8")
    assert "export function useWorkflowLive(project: string): WorkflowLivePayload | null {" in src


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
