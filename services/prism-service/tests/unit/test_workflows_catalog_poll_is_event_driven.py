"""The /workflows def+occupancy fetch no longer reschedules itself on a
blind fixed interval.

Task fix/canvasplay (owner 2026-09-13, verbatim): "why are you hammering
the server with polling rather than updating with streaming". Before this,
WorkflowsPage.tsx's definition+occupancy fetch unconditionally rearmed a
`window.setTimeout(load, POLL_MS)` every 10 seconds regardless of whether
the catalog's shape or any step's live occupancy had actually changed --
measured as the largest remaining request source on the page once the
System Activity panel and the shared /api/changes counter poll were fixed.

It now refetches only on a real GET /sse/changes signal of a kind that can
move either fact (a ship landed, a task's step/gate changed, the daemon's
own background activity ticked, or a workspace write), on window focus, or
via a last-resort safety net while the push stream itself looks unhealthy
-- all through the SAME `usePolledEffect` gate every other composite fetch
on this page already sits behind (see test_workflows_section_ui.py's own
history for that convention). The one remaining `window.setTimeout` in
this effect is the FAILURE-retry exponential backoff, a genuinely distinct
concern (reconnecting after an error, not routine refetching) pinned
separately by test_workflow_connection_interrupt_is_friendly_and_self_healing
and left untouched here.

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


def _poll_effect() -> str:
    src = _read()
    start = src.index(
        'useEffect(() => {\n    let cancel = false;\n    let timer = 0;')
    end = src.index("}, [project, reloadNonce]);", start)
    return src[start:end]


def test_no_fixed_interval_reschedules_the_def_and_occupancy_fetch():
    effect = _poll_effect()
    assert "POLL_MS" not in effect, (
        "the def+occupancy fetch must not reschedule itself on a fixed "
        "interval any more -- it refetches only on a real event")
    # The one remaining reschedule inside this effect is the failure-path
    # exponential backoff -- a reconnect safety net, not a routine poll.
    assert "window.setTimeout(load, delay)" in effect
    assert "timer = window.setTimeout(load, POLL_MS)" not in effect


def test_the_fetch_effect_reruns_on_reload_nonce():
    src = _read()
    assert "}, [project, reloadNonce]);" in src, (
        "the def+occupancy effect must depend on reloadNonce so bumping it "
        "re-runs a fresh load() -- the event-driven replacement for the "
        "old fixed-interval reschedule")


def test_reload_nonce_is_driven_by_real_change_events_not_a_setinterval():
    src = _read()
    # No page-owned setInterval left driving this refetch -- the reconnect
    # floor / focus / event gating all live inside usePolledEffect
    # (lib/usePolledResource.ts), not duplicated here.
    trigger_start = src.index("const initialCatalogReloadRef = useRef(true);")
    trigger_end = src.index("}, []), project, CATALOG_RELOAD_KINDS);", trigger_start) + len(
        "}, []), project, CATALOG_RELOAD_KINDS);")
    trigger = src[trigger_start:trigger_end]
    assert "setInterval" not in trigger
    assert "usePolledEffect(" in trigger
    assert "setReloadNonce((n) => n + 1)" in trigger


def test_catalog_reload_kinds_cover_what_can_move_the_board():
    src = _read()
    assert 'const CATALOG_RELOAD_KINDS = ["shipped", "workspace_written", "task_changed", "activity"];' in src, (
        "the reload-trigger kinds must cover every wakeups.signal() kind "
        "that can move the catalog's shape or a step's live occupancy -- "
        "see services/wakeups.py's real call sites")
