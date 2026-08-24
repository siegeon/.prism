"""The tab must never go blank/white when the backend restarts (owner
live, 2026-08-24, watching a dev restart through remote assist): "this was
a native app that can exist without the server running it, and it should
NEVER go white -- it should have the banner letting the customer know
it's updating".

THE BUG: App.tsx's lazyRoute() caught a failed chunk import (e.g. mid-
restart) and called window.location.reload() immediately, with no check
that the server would actually answer. If the restart was still in
progress, that reload navigation itself failed to connect -- the
BROWSER's own blank/error page for a failed top-level navigation, which
no React error-boundary or Suspense fallback can intercept, because the
SPA has already been torn down by that point.

THE FIX: lib/reconnect.ts's waitForServerThenReload() never reloads until
a real probe of /api/version succeeds; ReconnectBanner.tsx shows a
persistent "updating" strip while it waits, mounted at the top of the app
shell so it's visible regardless of route.

The PRISM SPA has NO JS test runner, so this is pinned by asserting the
ACTUAL source -- same convention as
tests/unit/test_conductor_page_animated_cleanup_ui.py.
"""

from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).resolve()
_SRC = _HERE.parent.parent.parent / "prism_service" / "web" / "src"
_RECONNECT = _SRC / "lib" / "reconnect.ts"
_BANNER = _SRC / "components" / "ReconnectBanner.tsx"
_APP = _SRC / "App.tsx"


def _read(p: Path) -> str:
    assert p.exists(), f"expected source missing: {p}"
    return p.read_text(encoding="utf-8")


def test_reload_only_happens_after_a_successful_probe():
    src = _read(_RECONNECT)
    # The reload call must live INSIDE the probe's success branch, not be
    # reachable unconditionally.
    assert "if (ok) {" in src and "window.location.reload();" in src
    idx_if = src.index("if (ok) {")
    idx_reload = src.index("window.location.reload();")
    idx_close = src.index("}", idx_if)
    assert idx_if < idx_reload < idx_close, (
        "window.location.reload() must be called INSIDE the `if (ok)` "
        "branch of the probe result, never unconditionally"
    )


def test_probe_hits_the_real_api_with_a_bounded_timeout():
    src = _read(_RECONNECT)
    assert '"/api/version"' in src
    assert "AbortController()" in src and "setTimeout(() => controller.abort()" in src, (
        "the probe must not hang forever waiting on a dead connection"
    )


def test_backoff_is_capped_not_a_tight_retry_loop():
    src = _read(_RECONNECT)
    assert "POLL_DELAYS_MS" in src or "POLL_DELAY_MAX_MS" in src, (
        "retries must back off, not hammer a server that's still coming up"
    )


def test_banner_renders_nothing_while_the_server_is_reachable():
    src = _read(_BANNER)
    assert "if (!reconnecting) return null;" in src, (
        "the banner must render nothing (not even an empty bar) when the "
        "server is reachable — it's a strip for a real event, not a "
        "permanent fixture"
    )
    assert "reconnecting automatically" in src.lower() or "updating" in src.lower(), (
        "the banner must actually tell the customer what's happening, not "
        "just show a generic spinner"
    )


def test_app_shell_defers_reload_to_the_safe_helper():
    app = _read(_APP)
    assert 'import { waitForServerThenReload } from "@/lib/reconnect";' in app
    assert "waitForServerThenReload()" in app
    # The banner must be mounted at the top of the shell, not nested inside
    # a route or a conditional that itself could fail to render.
    assert 'import ReconnectBanner from "@/components/ReconnectBanner";' in app
    assert "<ReconnectBanner />" in app


def test_lazy_route_catch_no_longer_reloads_blind():
    app = _read(_APP)
    start = app.index("function lazyRoute<")
    end = app.index("\n}", start)
    body = app[start:end]
    assert "waitForServerThenReload()" in body
    assert "window.location.reload()" not in body, (
        "lazyRoute's own catch handler must not call window.location."
        "reload() directly anymore — it must defer to "
        "waitForServerThenReload(), which only reloads after a real probe "
        "succeeds"
    )
