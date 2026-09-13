"""WorkflowsPage.tsx must own no `setInterval` of its own.

Task fix/canvasplay (owner, live measurement 2026-09-13, on an idle /workflows
tab): ~160 requests in 8 seconds (staleness 2/s, workflows/live 1/s,
tasks+stranded 1/s, consolidation/workers 1/s), traced to three
`window.setInterval` calls in this file:

  - the "loading elapsed seconds" ticker (a purely visual counter -- no
    network of its own, converted to requestAnimationFrame),
  - the "how long has this fetch been running" ticker inside the
    def+occupancy fetch effect (also purely visual, converted to rAF),
  - the "validation" scripted-run reattach poll, which genuinely fetches
    GET /api/workflows/runs/:id every second while a real WorkflowCore run
    is in flight -- there is no wakeups.signal()/GET /sse/changes event for
    that EXTERNAL engine's own step progress, so this one stays a real
    poll, just self-rescheduled via `setTimeout` instead of `setInterval`
    (it never runs on an idle tab -- it requires a live workflowRun -- so it
    was never part of the measured idle-tab storm).

No other setInterval existed in this file at the time of this fix; if a
future change adds one, this test is the trip-wire.

The SPA has no JS test runner, so this pins the actual TSX source, the
convention used by tests/unit/test_conductor_page_animated_cleanup_ui.py.
"""

from __future__ import annotations

from pathlib import Path

_SRC = (Path(__file__).resolve().parent.parent.parent
        / "prism_service" / "web" / "src")
_PAGE = _SRC / "pages" / "WorkflowsPage.tsx"


def _read() -> str:
    return _PAGE.read_text(encoding="utf-8")


def test_no_setinterval_anywhere_in_workflows_page():
    src = _read()
    assert "setInterval" not in src, (
        "WorkflowsPage.tsx must not use setInterval anywhere (code or "
        "comments) -- every periodic refetch on this page is driven by a "
        "real GET /sse/changes event (usePolledEffect/usePolledResource), "
        "window focus, or requestAnimationFrame for a purely-visual, "
        "no-network tick")


def test_loading_elapsed_ticker_uses_requestanimationframe():
    src = _read()
    idx = src.index("const [loadingElapsedS, setLoadingElapsedS] = useState(0);")
    window = src[idx:idx + 700]
    assert "requestAnimationFrame(tick)" in window
    assert "cancelAnimationFrame(raf)" in window


def test_slow_poll_ticker_uses_requestanimationframe():
    src = _read()
    idx = src.index("const armSlowTick = () => {")
    window = src[idx:idx + 600]
    assert "requestAnimationFrame(tick)" in window
    assert "cancelAnimationFrame(slowTick)" in window


def test_validation_run_reattach_poll_self_reschedules_via_settimeout():
    # The one genuine network poll left on this page -- an external
    # scripted-workflow engine run with no push signal to replace it. It
    # must still avoid setInterval outright, self-rescheduling via
    # setTimeout at the same cadence instead.
    src = _read()
    start = src.index("fetchWorkflowRun(workflowRun.id).then((next) => {")
    end = src.index(
        "}, [project, selectedWorkflowId, workflowRun?.id, workflowRun?.status, refreshRunHistory]);",
        start,
    )
    effect = src[start:end]
    assert "window.setTimeout(poll, 1000)" in effect
    assert "window.clearTimeout(timer)" in effect
