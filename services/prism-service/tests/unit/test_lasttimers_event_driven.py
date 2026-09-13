"""lib/version.ts and components/Sidebar.tsx must own no fixed-interval
poll of their own, and the three new wakeups kinds this fix introduces
must actually reach a browser.

Task fix/lasttimers (owner 2026-09-13, live idle-tab /workflows
measurement: 37 requests in 33s, 22 of them GET /api/version). Three
fixed timers are retired here:

  - lib/version.ts's 2s dev-bundle poll and 15s SSE-unhealthy fallback
    poll -> both now trigger only on a real "deployed" wakeups signal
    (subscribeToChangeKind) plus window focus/visibilitychange.
  - lib/scan-activity.ts's bespoke 2s/10s setTimeout loop backing
    Sidebar's useScanActivity()/useJobs() -> now usePolledResource(...,
    kinds=["jobs"]).
  - WorkflowsPage.tsx's staleness+consolidation usePolledEffect, which
    (per that call's own pre-fix comment) refetched on ANY /sse/changes
    event for lack of a dedicated kind -> now kinds=["staleness"].

A signal with no matching entry in routes/sse.py's _CHANGE_KINDS never
reaches a browser at all (GET /sse/changes only streams kinds in that
set) -- the regression this would produce is silent, since the browser
just falls back to its focus/60s-floor safety net and looks fine on a
quick glance. This file pins both halves together: the frontend source
text, and the backend wiring that makes the events real.

The SPA has no JS test runner, so this pins the actual TSX source, the
convention used by tests/unit/test_conductor_page_animated_cleanup_ui.py
and tests/unit/test_workflows_page_no_setinterval.py.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
_WEB_SRC = _ROOT / "prism_service" / "web" / "src"


def _read(rel: str) -> str:
    return (_WEB_SRC / rel).read_text(encoding="utf-8")


def _read_py(rel: str) -> str:
    return (_ROOT / "prism_service" / rel).read_text(encoding="utf-8")


def test_no_fixed_interval_timer_in_version_ts():
    src = _read("lib/version.ts")
    assert "setInterval" not in src, (
        "lib/version.ts must not use setInterval anywhere (code or "
        "comments) -- both watchers refetch on a real 'deployed' wakeups "
        "signal plus window focus/visibilitychange, never a fixed clock")


def test_no_fixed_interval_timer_in_sidebar_tsx():
    src = _read("components/Sidebar.tsx")
    assert "setInterval" not in src, (
        "components/Sidebar.tsx must not use setInterval anywhere (code "
        "or comments) -- its staleness fetch and the shared jobs poller "
        "it consumes are both event-driven")
    assert "setTimeout(" not in src, (
        "components/Sidebar.tsx must not use setTimeout( anywhere")


def test_version_ts_subscribes_to_deployed_change_kind():
    src = _read("lib/version.ts")
    assert 'import { subscribeToChangeKind } from "@/lib/useChanges";' in src
    assert src.count('subscribeToChangeKind("deployed"') == 2, (
        "both the dev-bundle watcher and the live watchdog's fallback "
        "poll must trigger off a real 'deployed' /sse/changes frame")


def test_scan_activity_rides_usepolledresource_with_jobs_kind():
    src = _read("lib/scan-activity.ts")
    assert "setInterval" not in src and "setTimeout(" not in src, (
        "the shared /api/jobs poller must not self-reschedule on a fixed "
        "timer any more -- it rides usePolledResource's event/focus/floor "
        "gate instead")
    assert 'const JOBS_KINDS = ["jobs"];' in src
    assert "usePolledResource<{ jobs: ScanJob[] }>(JOBS_URL, \"\", JOBS_KINDS)" in src


def test_workflows_staleness_pair_has_its_own_kind():
    src = _read("pages/WorkflowsPage.tsx")
    idx = src.index("usePolledEffect(useCallback(() => {\n    let cancelled = false;\n    Promise.all([\n      api.get<{ brain: boolean; graph: boolean }>")
    end = src.index("}, [project]), project, [\"staleness\"]);", idx)
    assert end > idx, (
        "the staleness+consolidation composite fetch must pass "
        'kinds=["staleness"] to usePolledEffect instead of refetching on '
        "any /sse/changes event")


def test_change_kinds_whitelist_covers_the_three_new_signals():
    src = _read_py("routes/sse.py")
    for kind in ("jobs", "staleness", "deployed"):
        assert f'"{kind}"' in src.split("_CHANGE_KINDS = frozenset({", 1)[1].split("})", 1)[0], (
            f"routes/sse.py's _CHANGE_KINDS must include {kind!r} -- "
            "otherwise wakeups.signal(...) calls of that kind never reach "
            "a browser over GET /sse/changes at all")


def test_job_queue_signals_jobs_kind_on_every_mutation():
    src = _read_py("inference/queue.py")
    assert 'wakeups.signal("jobs", self._project or "*")' in src
    for method in ("def enqueue(", "def cancel_stale_pending(", "def drain(",
                   "def complete(", "def fail("):
        idx = src.index(method)
        window = src[idx:idx + 1400]
        assert "_signal_jobs()" in window, (
            f"{method} must call self._signal_jobs() so the SPA's shared "
            "/api/jobs poller refetches on the real mutation")


def test_staleness_kind_signalled_from_the_three_named_sources():
    understand = _read_py("engines/understand_engine.py")
    assert 'wakeups.signal("staleness", self.project or "*")' in understand

    clock = _read_py("services/maintenance_clock.py")
    assert 'wakeups.signal("staleness", project or "*")' in clock

    drift = _read_py("services/drift_worker.py")
    assert 'wakeups.signal("staleness", pid or "*")' in drift


def test_deployed_kind_signalled_from_deploy_worker_and_main():
    deploy = _read_py("services/deploy_worker.py")
    assert 'wakeups.signal("deployed", "*")' in deploy

    main = _read_py("main.py")
    assert 'wakeups.signal("deployed", "*")' in main
