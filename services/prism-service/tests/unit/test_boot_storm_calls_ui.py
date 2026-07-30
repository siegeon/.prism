"""UI contract tests for "Stop the five-call boot storm and the 2s job poll"
(task c38ef597).

The PRISM SPA has NO JS test runner, so UI-FIRST acceptance criteria are
pinned by asserting the ACTUAL web source (TS/TSX) — the same pattern as
tests/unit/test_conductor_page_animated_cleanup_ui.py.

A 2026-07-22 lane (commit 7c74df6) already fixed the obvious "poll forever,
even hidden" cases for /api/jobs, /api/staleness and /api/conductor/state
(document.hidden guard + visibilitychange resume), and routed
resolveInitialProject's raw fetch() through the lib/api.ts chokepoint. This
file pins what THAT fix did not touch, re-measured directly against main
today rather than the original 2026-07-22 ticket text:

  AC-1: /api/projects still fires twice on a genuine cold mount because
        PageHeader.tsx and lib/project.ts#resolveInitialProject each own an
        independent fetch. They must share ONE single-flight request.
  AC-2: useScanActivity (lib/scan-activity.ts) is a per-call useEffect/
        setInterval — Sidebar.tsx and LiveBar.tsx each instantiate it
        independently, so /api/jobs is polled twice as often as intended.
        It must become a shared, module-level singleton (one timer, many
        subscribers).
  AC-3: LiveBar is mounted unconditionally in App.tsx and only nulls its
        OWN render output off the activity-context routes (/, /tasks,
        /conductor) — its polling effects (the /api/conductor/state fetch
        and its useScanActivity subscription) keep running on every route,
        including /understand, /learning, /settings, /sessions. The route
        gate must move up to App.tsx so LiveBar actually UNMOUNTS (its
        effects tear down) off those routes.
  AC-4: regression guard — the document.hidden / visibilitychange pause-
        resume behavior from 7c74df6 must survive both refactors above.

ALL of these FAIL against the current source: fetchProjectsList does not
exist yet, useScanActivity's timer/subscriber state lives inside the hook
body rather than at module scope, and LiveBar.tsx's inActivityContext is
neither exported nor consulted from App.tsx. They go green only once
lib/project.ts, lib/scan-activity.ts, components/LiveBar.tsx and App.tsx
are wired per the plan.
"""

from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).resolve()
_SRC = _HERE.parent.parent.parent / "prism_service" / "web" / "src"
_PROJECT_TS = _SRC / "lib" / "project.ts"
_PAGE_HEADER = _SRC / "components" / "PageHeader.tsx"
_SCAN_ACTIVITY = _SRC / "lib" / "scan-activity.ts"
_LIVE_BAR = _SRC / "components" / "LiveBar.tsx"
_APP = _SRC / "App.tsx"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# AC-1: /api/projects — one single-flight fetch, shared by both callers
# ---------------------------------------------------------------------------

def test_projects_fetch_is_single_flight_shared_by_both_callers():
    project_src = _read(_PROJECT_TS)
    header_src = _read(_PAGE_HEADER)

    assert "export function fetchProjectsList" in project_src or \
        "export const fetchProjectsList" in project_src, \
        "lib/project.ts must export a single-flight fetchProjectsList() so " \
        "resolveInitialProject and PageHeader can share ONE in-flight " \
        "/api/projects request instead of firing two independent ones"

    # resolveInitialProject's body must route through the shared function,
    # not its own direct fetchJSON("/api/projects") call.
    resolve_start = project_src.index("export async function resolveInitialProject")
    resolve_body = project_src[resolve_start:project_src.index(
        "\nexport function useProject", resolve_start)]
    assert "fetchProjectsList(" in resolve_body, \
        "resolveInitialProject must call the shared fetchProjectsList(), " \
        "not its own independent fetch"

    # PageHeader's picker load must call the SAME shared function, not its
    # own api.get("/api/projects") — that direct call is exactly the second
    # half of the duplicate on a cold mount.
    assert "fetchProjectsList" in header_src, \
        "PageHeader must import and call the shared fetchProjectsList(), " \
        "not fetch /api/projects on its own"
    load_start = header_src.index("const loadProjects = useCallback(")
    load_body = header_src[load_start:header_src.index("}, []);", load_start)]
    assert 'api.get<{ projects: string[] }>("/api/projects")' not in load_body, \
        "PageHeader's loadProjects must no longer own a direct /api/projects " \
        "fetch — it must go through the shared single-flight function so " \
        "only one request is in flight per mount"

    # Single-flight, not a permanent cache: the in-flight slot must be
    # cleared once the request settles, so a call after
    # notifyProjectsChanged() (project create/delete) still gets a fresh
    # fetch rather than stale data forever.
    assert "notifyProjectsChanged" in project_src, \
        "the create/delete invalidation hook must still exist — a single-" \
        "flight fetch must not become a cache that never invalidates"


# ---------------------------------------------------------------------------
# AC-2: /api/jobs — useScanActivity becomes a shared singleton, not one
# independent poll loop per call site (Sidebar AND LiveBar each call it).
# ---------------------------------------------------------------------------

def test_scan_activity_is_shared_singleton_not_one_loop_per_caller():
    src = _read(_SCAN_ACTIVITY)
    hook_marker = "export function useScanActivity"
    assert hook_marker in src
    module_scope = src[:src.index(hook_marker)]
    hook_body = src[src.index(hook_marker):]

    # The poll's subscriber bookkeeping must live at MODULE scope (shared by
    # every component that calls the hook), not be declared fresh inside the
    # hook body on every call/mount.
    assert "Set<" in module_scope, \
        "the poll must be shared via a module-level subscriber set so " \
        "Sidebar.tsx and LiveBar.tsx (which each call useScanActivity " \
        "independently) drive ONE /api/jobs loop between them, not two"

    # The hook body itself must no longer own a private timer — that timer
    # must belong to the module-level singleton loop instead.
    assert "let timer" not in hook_body, \
        "useScanActivity's own body must not declare its own polling timer " \
        "any more — scheduling belongs to the shared module-level loop, " \
        "otherwise every caller still runs its own independent interval"

    # Regression guard (AC-4): the hidden-tab skip / visibilitychange resume
    # shipped in 7c74df6 must survive the move to a singleton.
    assert "document.hidden" in src, \
        "hidden-tab fetch skip must still exist after the singleton refactor"
    assert "visibilitychange" in src, \
        "visibilitychange resume must still exist after the singleton refactor"

    # Public API is unchanged — call sites need no changes.
    assert "export function useScanActivity(): ScanActivity" in src


# ---------------------------------------------------------------------------
# AC-3: LiveBar must not just render null off activity-context routes — it
# must not be MOUNTED there at all, so its polling effects tear down.
# ---------------------------------------------------------------------------

def test_livebar_unmounts_off_activity_routes_instead_of_rendering_null():
    livebar_src = _read(_LIVE_BAR)
    app_src = _read(_APP)

    assert "export function inActivityContext" in livebar_src or \
        "export const inActivityContext" in livebar_src, \
        "inActivityContext must be exported from LiveBar.tsx so App.tsx can " \
        "gate WHETHER LiveBar mounts at all, instead of LiveBar quietly " \
        "polling on every route and only hiding its own JSX"

    assert "inActivityContext" in app_src, \
        "App.tsx must consult inActivityContext to decide whether to mount " \
        "<LiveBar /> — otherwise LiveBar's /api/conductor/state poll and " \
        "its useScanActivity subscription keep running on every route " \
        "(e.g. /understand, /learning, /settings) where no task chrome " \
        "is ever shown"

    app_start = app_src.index("<LiveBar")
    # The 200 chars immediately before the tag must contain a conditional
    # short-circuit (the tag must not be an unconditional sibling anymore).
    preceding = app_src[max(0, app_start - 200):app_start]
    assert "&&" in preceding, \
        "<LiveBar /> must be conditionally rendered (route && <LiveBar />) " \
        "in App.tsx, not always present in the tree"

    # AC-6 regression guard: cadence on the routes where it IS shown must be
    # untouched — still a 5s poll, not stretched to buy quiet.
    assert "setInterval(tick, 5000)" in livebar_src, \
        "LiveBar's poll interval must stay 5000ms on the routes where it " \
        "renders — the fix must not buy quiet by slowing the live bar down"
