"""UI contract tests for the SPA's shared poll layer (task fix/polling).

The PRISM SPA has NO JS test runner, so this pins the ACTUAL TypeScript
source of the shared hooks -- same convention as
test_conductor_page_animated_cleanup_ui.py.

HISTORY: round 1 replaced ~10 independently-polled endpoints with a
shared GET /api/changes 1Hz counter poll. Owner, live: "why are you
hammering the server with polling rather than updating with streaming".
Round 2 (this file, current form) replaces that counter POLL with a real
PUSH: lib/useChanges.ts subscribes to GET /sse/changes (one EventSource
per tab, via the existing lib/sharedStream.ts leader-election machinery)
instead of running any timer of its own; lib/usePolledResource.ts's
generic gate refetches on a matching-kind SSE event, on focus/visibility,
or a 60s floor used ONLY as a reconnect safety net while the stream looks
unhealthy -- never as a routine poll substitute.
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
# lib/useChanges.ts -- ONE SSE subscription (GET /sse/changes) per tab,
# NO setInterval/setTimeout of its own for change detection.
# ---------------------------------------------------------------------------

def test_use_changes_subscribes_to_the_sse_changes_stream():
    src = _read(_USE_CHANGES)
    assert "/sse/changes" in src


def test_use_changes_uses_the_shared_stream_not_a_private_eventsource():
    src = _read(_USE_CHANGES)
    # ONE EventSource per tab means riding sharedStream.ts's existing
    # per-URL dedup + cross-tab leader election, never `new EventSource(`
    # constructed directly here.
    assert "subscribeStream" in src
    assert "new EventSource(" not in src


def test_use_changes_runs_no_timer_for_change_detection():
    src = _read(_USE_CHANGES)
    assert "setInterval(" not in src
    assert "setTimeout(" not in src


def test_use_changes_exposes_health_for_a_reconnect_safety_floor():
    src = _read(_USE_CHANGES)
    assert "healthy" in src


# ---------------------------------------------------------------------------
# lib/usePolledResource.ts -- the generic event/focus/reconnect-floor gate
# ---------------------------------------------------------------------------

def test_use_polled_resource_gates_on_sse_change_events():
    src = _read(_USE_POLLED)
    assert "useChangeEvents" in src


def test_use_polled_resource_supports_narrowing_by_event_kind():
    src = _read(_USE_POLLED)
    assert "kinds" in src


def test_use_polled_resource_has_a_60s_reconnect_safety_floor():
    src = _read(_USE_POLLED)
    assert re.search(r"60[_,]?000", src), \
        "the floor must be 60s and gated on stream health, not a routine poll"
    # The floor must be conditioned on the stream being UNhealthy -- never
    # fired just because time passed while the stream is fine.
    assert "if (healthy) return;" in src


def test_use_polled_resource_refetches_on_focus_and_visibility():
    src = _read(_USE_POLLED)
    assert "focus" in src
    assert "visibilitychange" in src


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


def test_use_polled_effect_also_supports_kind_narrowing():
    src = _read(_USE_POLLED)
    assert "export function usePolledEffect(load: () => void, project = \"\", kinds?: string[])" in src


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
