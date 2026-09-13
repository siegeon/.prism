"""/workflows must never read as broken while the backend is merely slow
to answer (owner, repeatedly: "the click through is a dark solid image
again like i asked you fix over and over").

Before this fix, WorkflowsPage.tsx had no distinct "not loaded yet" state:
`workflows` seeded as `[]` and every derived surface (the directory rail,
the canvas, the status-line banner's "No runs yet" / "No recent activity")
rendered its genuinely-empty copy identically whether the board was truly
idle or the very first GET /api/workflows / GET /api/conductor/state simply
hadn't answered yet -- observed live with the catalog fetch taking >10s
during a post-restart drift-reindex burst.

The PRISM SPA has NO JS test runner, so these acceptance criteria are
pinned by asserting the ACTUAL TSX source (same convention as
tests/unit/test_conductor_page_animated_cleanup_ui.py and
test_workflows_live_channel_ui.py): a loading overlay with a live elapsed
counter must exist, "No runs yet"/"No recent activity" must be gated on a
loaded flag rather than rendered unconditionally, a slow/failed refresh
poll must keep the last good data and show a small "backend slow" pill
instead of blanking, and a session-scoped cache must exist so navigating
between ?workflow= views and back repaints instantly rather than emptying
out while a refetch is in flight.
"""
from __future__ import annotations

from pathlib import Path

_PAGE = (Path(__file__).resolve().parent.parent.parent
          / "prism_service/web/src/pages/WorkflowsPage.tsx")


def _read() -> str:
    return _PAGE.read_text(encoding="utf-8")


def test_a_catalog_and_state_arrived_flag_exists():
    src = _read()
    assert "catalogArrived" in src, (
        "a distinct 'has the first catalog response arrived' flag must "
        "exist -- workflows.length === 0 alone cannot tell 'not loaded "
        "yet' apart from 'genuinely empty'")
    assert "stateObserved" in src or "conductorStateObserved" in src, (
        "the loading gate must also require the conductor STATE poll's "
        "own first-arrival flag (useConductorState's `observed`), not "
        "just the catalog fetch")
    assert "const dataLoaded =" in src, (
        "a single combined 'both the catalog and the state have answered "
        "at least once' flag must gate every empty-state render")


def test_loading_overlay_shows_elapsed_seconds_never_blank_canvas():
    src = _read()
    assert "loadingElapsedS" in src, (
        "the loading overlay must show a LIVE elapsed-seconds counter "
        "(\"catalog 12s\"), not a static spinner with no sense of how "
        "long the wait has been")
    assert "Loading workflows" in src, (
        "the canvas must render an explicit loading overlay while "
        "!dataLoaded, replacing the blank dark canvas the owner reported"
    )
    # The overlay must actually be conditioned on dataLoaded, not always-on
    # or always-off.
    idx = src.index("Loading workflows")
    window = src[max(0, idx - 400):idx]
    assert "dataLoaded" in window, (
        "the loading overlay text must sit inside a block gated on "
        "!dataLoaded -- otherwise it either never shows or never clears")


def test_no_runs_yet_and_no_recent_activity_are_gated_on_dataLoaded():
    src = _read()
    # statusLineText's fallback branch
    no_runs_idx = src.index('return "No runs yet"', src.index("statusLineText"))
    preceding = src[max(0, no_runs_idx - 300):no_runs_idx]
    assert "dataLoaded" in preceding, (
        "'No runs yet' must only be reachable after dataLoaded is true -- "
        "before this fix it was the unconditional fallback of "
        "statusLineText, so it rendered on every fresh, still-loading "
        "page load")
    # The "No recent activity" banner row
    no_activity_idx = src.index("No recent activity")
    preceding_activity = src[max(0, no_activity_idx - 300):no_activity_idx]
    assert "dataLoaded" in preceding_activity, (
        "'No recent activity' must be gated on dataLoaded too, or it "
        "reads as 'nothing has ever happened' during the first slow load")


def test_a_slow_or_failed_refresh_poll_never_blanks_prior_data():
    src = _read()
    assert "pollSlowS" in src, (
        "a refresh poll running long (or failing) must be tracked "
        "separately from the initial-load flag so already-rendered data "
        "is never discarded out from under the viewer")
    assert "backend slow" in src, (
        "a small 'backend slow · Ns' pill must render instead of silently "
        "blanking when a subsequent poll is slow or fails"
    )
    # The catch() branch of the definition-fetch poll must not clear
    # workflows/data -- it must be silent about content, only touching
    # connection-state flags.
    catch_idx = src.index(".catch(() => {", src.index("fetchWorkflowDef(project)"))
    catch_block_end = src.index("});", catch_idx)
    catch_block = src[catch_idx:catch_block_end]
    assert "setWorkflows([])" not in catch_block, (
        "a failed poll must never clear the workflows catalog to empty")
    assert "setData(null)" not in catch_block, (
        "a failed poll must never clear the workflow definition to null")


def test_a_session_cache_lets_navigation_repaint_instantly():
    src = _read()
    assert "sessionStorage" in src, (
        "a session-scoped cache of the last good catalog must exist so "
        "returning to a view mid-refetch repaints from cache instead of "
        "the empty seed state")
    assert "readCatalogCache" in src and "writeCatalogCache" in src, (
        "the catalog cache needs both a read (to seed initial state) and "
        "a write (on every successful poll) side"
    )
    # workflows state must be SEEDED from the cache, not from a bare [].
    seed_idx = src.index("useState<WorkflowCatalogEntry[]>(")
    seed_line = src[seed_idx:seed_idx + 120]
    assert "readCatalogCache" in seed_line, (
        "the workflows state's initial value must read the cache, not "
        "hardcode an empty array"
    )
