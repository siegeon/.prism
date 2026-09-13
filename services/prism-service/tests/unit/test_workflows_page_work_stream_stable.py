"""WorkflowsPage.tsx's /sse/work subscription must not reopen on every
catalog reload.

Task fix/stream (owner, live measurement 2026-09-13, idle /workflows tab,
60s window against a real daemon serving 946 tasks): GET /sse/work?project=
opened SIX times in that minute, and each reopen triggered a fresh round of
workflows/staleness/workflows-live/tasks/tasks-stranded refetches. A raw
`curl -N /sse/work` against the SAME daemon held one connection open past
two 25s keepalives with zero server-initiated closes -- this was never a
server-side drop.

The real cause: the effect that opens `/sse/work` depended on
`refreshFlowRuns`, whose own identity changes every time the routine
catalog reload (CATALOG_RELOAD_KINDS, gated on real `task_changed` events --
frequent on a live, busy daemon) gives `selectedWorkflow` a fresh object.
Each identity change tore the effect down and reopened a brand-new
EventSource through lib/sharedStream.ts's ref-counted close-on-zero-subs
path. The fix pins the effect's dependency array to `[project]` only --
the one value that actually changes the stream's URL -- and reads the
current task id / callbacks through a ref instead.

The SPA has no JS test runner, so this pins the actual TSX source, the
convention used by tests/unit/test_conductor_page_animated_cleanup_ui.py
and tests/unit/test_workflows_page_no_setinterval.py.
"""

from __future__ import annotations

from pathlib import Path

_SRC = (Path(__file__).resolve().parent.parent.parent
        / "prism_service" / "web" / "src")
_PAGE = _SRC / "pages" / "WorkflowsPage.tsx"


def _read() -> str:
    return _PAGE.read_text(encoding="utf-8")


def test_sse_work_subscription_effect_depends_only_on_project():
    src = _read()
    start = src.index('return subscribeStream(`/sse/work?project=')
    window = src[start:start + 700]
    assert "\n  }, [project]);" in window, (
        "the /sse/work subscription effect must depend ONLY on `project` -- "
        "listing refreshFlowRuns/animateTokenAlong/nodeStatusTaskId there "
        "reopens the EventSource every time any of their identities churn, "
        f"which is what caused the 6-reopens-in-60s regression. Window: {window!r}"
    )
    assert "[project, nodeStatusTaskId, animateTokenAlong, refreshFlowRuns]" not in src, (
        "the old unstable dependency list must not reappear anywhere in the file"
    )


def test_sse_work_frame_handler_reads_latest_state_through_a_ref():
    src = _read()
    idx = src.index('return subscribeStream(`/sse/work?project=')
    window = src[idx:idx + 500]
    assert "workEventRef.current" in window, (
        "the frame handler must read nodeStatusTaskId/animateTokenAlong/"
        "refreshFlowRuns through a ref (workEventRef) now that the effect "
        "no longer lists them as deps, so it still sees current values "
        "without forcing a resubscribe"
    )


def test_work_event_ref_is_kept_current_every_render():
    src = _read()
    assert "const workEventRef = useRef({ nodeStatusTaskId, animateTokenAlong, refreshFlowRuns });" in src
    assert "workEventRef.current = { nodeStatusTaskId, animateTokenAlong, refreshFlowRuns };" in src
