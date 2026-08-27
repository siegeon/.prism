"""lib/sharedStream.ts's openStream() guards `if (e.es) return` -- so once a
connection dies, nothing ever recreates it unless onerror clears that
reference. Observed live (2026-08-27): a browser tab open through a daemon
restart stayed frozen on pre-restart task data indefinitely; the SSE
connection never healed even though the daemon came back healthy within
seconds and the tab's own /api/version poll kept succeeding the whole time.

Root cause: es.onerror only called setHealth(url, false) -- it never
distinguished a native auto-retry (readyState CONNECTING) from a truly dead
connection (readyState CLOSED), and never nulled `e.es`, so openStream()'s
own re-entry guard permanently believed a dead object was still live.

Convention (test_shared_sse_stream_ui.py): no JS runner -- parse the
comment-stripped source with brace-walked scopes, never a fixed window.
"""

from __future__ import annotations

import re
from pathlib import Path

_HERE = Path(__file__).resolve()
_SHARED_STREAM_TS = (
    _HERE.parent.parent.parent / "prism_service" / "web" / "src" / "lib" / "sharedStream.ts"
)


def _nocomments(path: Path) -> str:
    src = path.read_text(encoding="utf-8")
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", src)


def _enclosing_function(src: str, signature: str) -> str:
    start = src.index(signature)
    brace_open = src.index("{", start)
    depth = 0
    i = brace_open
    while i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start: i + 1]
        i += 1
    raise AssertionError(f"unbalanced braces scanning for {signature!r}")


def test_onerror_distinguishes_closed_from_retrying():
    """A permanently CLOSED EventSource must be told apart from one the
    browser is already auto-retrying (CONNECTING) -- only CLOSED needs help."""
    src = _nocomments(_SHARED_STREAM_TS)
    on_error = _enclosing_function(src, "es.onerror = () => {")
    assert "EventSource.CLOSED" in on_error, (
        "onerror must check es.readyState === EventSource.CLOSED before "
        "trying to self-heal -- a CONNECTING readyState means the browser "
        "is already retrying on its own")


def test_onerror_clears_the_dead_reference_on_permanent_close():
    """AC: once truly closed, `e.es` must be nulled -- otherwise
    openStream()'s `if (e.es) return` guard treats the dead object as a
    live connection forever and nothing can ever reopen it."""
    src = _nocomments(_SHARED_STREAM_TS)
    on_error = _enclosing_function(src, "es.onerror = () => {")
    assert re.search(r"e\.es\s*=\s*null", on_error), (
        "onerror's CLOSED branch must clear e.es so a later call to "
        "openStream() is not blocked by its own re-entry guard")


def test_onerror_schedules_a_reopen_attempt():
    """AC: a dead stream with live subscribers must retry itself -- a user
    should never need to hard-reload just to recover a routine daemon
    bounce."""
    src = _nocomments(_SHARED_STREAM_TS)
    on_error = _enclosing_function(src, "es.onerror = () => {")
    assert "setTimeout(" in on_error and "openStream(url)" in on_error, (
        "onerror's CLOSED branch must schedule a retry via openStream(url), "
        "not just mark the entry dead and give up")


def test_onerror_still_never_reloads():
    """Regression guard for the ALREADY-shipped contract (task b15e84b2): a
    reconnect attempt is not a version-mismatch reload, and must stay out of
    version.ts's guarded-reload territory entirely."""
    src = _nocomments(_SHARED_STREAM_TS)
    on_error = _enclosing_function(src, "es.onerror = () => {")
    assert "reload" not in on_error.lower(), (
        "sharedStream.ts's self-heal must not call reload or guardedReload "
        "-- a dropped connection is not a version mismatch")


def test_reconnect_only_fires_while_subscribers_remain():
    """AC: no reconnect attempt for a URL nobody is listening to anymore --
    otherwise a closed page/component leaks a retry loop against a URL with
    zero subscribers."""
    src = _nocomments(_SHARED_STREAM_TS)
    on_error = _enclosing_function(src, "es.onerror = () => {")
    assert "e.subs.size > 0" in on_error, (
        "the reconnect branch must be gated on e.subs.size > 0 -- a stream "
        "nobody subscribes to should not keep retrying itself")
