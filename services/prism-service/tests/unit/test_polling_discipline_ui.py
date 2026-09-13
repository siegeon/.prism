"""UI contract tests for the SPA's shared poll layer (task fix/polling).

The PRISM SPA has NO JS test runner, so this pins the ACTUAL TypeScript
source of the new shared hooks -- same convention as
test_conductor_page_animated_cleanup_ui.py.

THE BUG: an idle Workflows tab issued ~10 independent requests every
1-2s because every consumer polled its own endpoint on its own fixed
interval with no notion of "did anything actually change", and several
of them (Sidebar's staleness poll) kept a private setInterval alive even
though nothing had moved. lib/useChanges.ts is the ONE shared 1s poll of
the new GET /api/changes counter; lib/usePolledResource.ts is the
generic gate ("refetch only when the counter moves, or on focus, or at
a floor") every data query should sit behind.

These FAIL against a tree with no useChanges.ts / usePolledResource.ts
and against Sidebar.tsx's old bare 5s setInterval staleness poll. They
go green only once the shared layer exists and Sidebar has been moved
onto it.
"""

from __future__ import annotations

import re
from pathlib import Path

_HERE = Path(__file__).resolve()
_SRC = _HERE.parent.parent.parent / "prism_service" / "web" / "src"
_USE_CHANGES = _SRC / "lib" / "useChanges.ts"
_USE_POLLED = _SRC / "lib" / "usePolledResource.ts"
_SIDEBAR = _SRC / "components" / "Sidebar.tsx"
_API = _SRC / "lib" / "api.ts"


def _read(p: Path) -> str:
    assert p.exists(), f"expected {p} to exist"
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# lib/useChanges.ts -- ONE shared 1s poll of GET /api/changes per tab
# ---------------------------------------------------------------------------

def test_use_changes_polls_the_changes_endpoint():
    src = _read(_USE_CHANGES)
    assert "/api/changes" in src


def test_use_changes_runs_exactly_one_interval_module_scope():
    src = _read(_USE_CHANGES)
    # Module-scope singleton, not one setInterval per component instance --
    # the exact bug class sharedStream.ts/useConductorState.ts already
    # exist to prevent for SSE and /api/conductor/state.
    assert src.count("setInterval(") == 1, \
        "useChanges must run exactly one shared poll timer, not one per subscriber"


def test_use_changes_stops_polling_while_the_tab_is_hidden():
    src = _read(_USE_CHANGES)
    assert "document.hidden" in src or "visibilityState" in src, \
        "the poll must pause while the tab is hidden"


def test_use_changes_resumes_promptly_on_focus_or_visibility():
    src = _read(_USE_CHANGES)
    assert "visibilitychange" in src or "addEventListener(\"focus\"" in src \
        or "addEventListener('focus'" in src


# ---------------------------------------------------------------------------
# lib/usePolledResource.ts -- the generic counter/focus/floor refetch gate
# ---------------------------------------------------------------------------

def test_use_polled_resource_gates_on_the_shared_change_counter():
    src = _read(_USE_POLLED)
    assert "useChanges" in src


def test_use_polled_resource_has_a_30s_refetch_floor():
    src = _read(_USE_POLLED)
    assert re.search(r"30[_,]?000", src), \
        "must define a 30s floor so an idle query still self-heals eventually"


def test_use_polled_resource_refetches_on_focus():
    src = _read(_USE_POLLED)
    assert "focus" in src


def test_use_polled_resource_stops_entirely_while_hidden():
    src = _read(_USE_POLLED)
    assert "document.hidden" in src


def test_use_polled_resource_caches_last_payload_module_scope_for_instant_paint():
    src = _read(_USE_POLLED)
    # Stale-while-revalidate: a module-scope Map (not component state) so
    # navigating back to an already-fetched URL paints instantly instead of
    # a fresh blank-loading flash, then revalidates in the background.
    assert re.search(r"new Map<", src), \
        "expected a module-scope cache keyed by URL/resource"


# ---------------------------------------------------------------------------
# lib/api.ts -- ETag-aware conditional fetch, available to any consumer
# ---------------------------------------------------------------------------

def test_api_supports_conditional_requests_via_etag():
    src = _read(_API)
    assert "If-None-Match" in src
    assert "304" in src


# ---------------------------------------------------------------------------
# Sidebar.tsx -- the concrete before/after: staleness moves off its own
# bare 5s setInterval onto the shared gate.
# ---------------------------------------------------------------------------

def test_sidebar_staleness_no_longer_runs_its_own_bare_five_second_interval():
    src = _read(_SIDEBAR)
    assert "setInterval(tick, 5000)" not in src, \
        "staleness must ride the shared usePolledResource gate, not its own timer"


def test_sidebar_staleness_uses_the_shared_polled_resource():
    src = _read(_SIDEBAR)
    assert "usePolledResource" in src
