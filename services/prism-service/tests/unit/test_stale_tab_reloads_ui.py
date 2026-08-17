"""RED — a stale tab must survive a redeploy (task 6a094d45).

main.tsx already carries recoverFromStaleChunk (unhandledrejection/error
listeners + chunk-error regex + 30s cooldown, owner 2026-07-21), but React's
lazy machinery can swallow the rejection before the global listeners see it —
the owner's Work click no-op'd on 2026-08-17 with the recovery present. Vite's
`vite:preloadError` window event fires from the preload helper itself,
regardless of who later catches the promise, so wiring IT into the recovery
closes the gap. Convention: no JS runner — parse the comment-stripped source.
"""

from __future__ import annotations

import re
from pathlib import Path

_HERE = Path(__file__).resolve()
_MAIN = _HERE.parent.parent.parent / "prism_service" / "web" / "src" / "main.tsx"


def _src_nocomments() -> str:
    src = _MAIN.read_text(encoding="utf-8")
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", src)


def test_preload_error_triggers_one_reload():
    """AC-1: the entry listens for vite:preloadError (fired by vite's own
    preload helper even when React lazy swallows the rejection) and routes
    it into the stale-chunk recovery with preventDefault."""
    src = _src_nocomments()
    idx = src.find('addEventListener("vite:preloadError"')
    assert idx >= 0, (
        "main.tsx must register a vite:preloadError listener -- global "
        "unhandledrejection alone missed the owner's stale-tab no-op click")
    handler = src[idx: idx + 400]
    assert "preventDefault" in handler, (
        "the handler must preventDefault so vite does not rethrow after "
        "we take over recovery")
    assert "recoverFromStaleChunk" in handler or "location.reload" in handler, (
        "the handler must route into the existing recovery path")


def test_reload_guard_is_one_shot():
    """AC-2: the recovery keeps its loop protection -- a sessionStorage
    timestamp with a cooldown comparison gating window.location.reload."""
    src = _src_nocomments()
    fn = src[src.index("function recoverFromStaleChunk"):]
    fn = fn[: fn.index("window.addEventListener")]
    assert "sessionStorage" in fn and "reload()" in fn
    assert re.search(r"Date\.now\(\)\s*-\s*\w+\s*<\s*\w*COOLDOWN", fn), (
        "the cooldown comparison must gate the reload -- without it a "
        "genuinely missing asset would reload-loop forever")
