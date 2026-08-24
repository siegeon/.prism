"""RED -- a stale-tab reload must be time-boxed against a flapping daemon
(task b15e84b2).

lib/version.ts already detects a daemon-version change via two channels (the
SSE hello in startLiveWatchdog's onVersion, and the dev-mode 2s poll in
startDevBundleWatch) and reloads the page on mismatch -- but BOTH call sites
call `window.location.reload()` directly, unguarded by the time-boxed
anti-loop pattern main.tsx already uses for its own stale-chunk recovery
(RELOAD_FLAG/COOLDOWN_MS). A daemon that flaps between versions mid-bounce
can therefore reload-loop the tab -- exactly the failure this task's own
likely_misfire names. Convention: no JS runner -- parse the comment-stripped
source (see test_stale_tab_reloads_ui.py, task 6a094d45).
"""

from __future__ import annotations

import re
from pathlib import Path

_HERE = Path(__file__).resolve()
_WEB_SRC = _HERE.parent.parent.parent / "prism_service" / "web" / "src"
_VERSION_TS = _WEB_SRC / "lib" / "version.ts"
_SHARED_STREAM_TS = _WEB_SRC / "lib" / "sharedStream.ts"


def _nocomments(path: Path) -> str:
    src = path.read_text(encoding="utf-8")
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", src)


def _enclosing_function(src: str, signature: str) -> str:
    """Extract signature's body via brace counting -- not a fixed char window,
    so a stray comment or later helper can never leak into the slice."""
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


def test_boot_version_captured_before_comparison():
    """AC-1: startLiveWatchdog's onVersion records the first value it sees
    (the boot version) before ever comparing a later value against it."""
    src = _nocomments(_VERSION_TS)
    on_version = _enclosing_function(src, "const onVersion = (v: string | undefined) => {")
    capture_idx = on_version.index("initial === null")
    compare_idx = on_version.index("v !== initial")
    assert capture_idx < compare_idx, (
        "the boot-version capture branch must run (and be textually ordered) "
        "before the mismatch-comparison branch")


def test_live_watchdog_reload_is_guarded():
    """AC-2: the SSE-driven reload call site must route through a shared,
    named guarded-reload helper -- not call window.location.reload() bare --
    so a flapping daemon during a bounce sequence cannot loop the tab."""
    src = _nocomments(_VERSION_TS)
    on_version = _enclosing_function(src, "const onVersion = (v: string | undefined) => {")
    assert "window.location.reload()" not in on_version, (
        "onVersion must not call window.location.reload() directly -- it "
        "needs to go through a time-boxed guard like main.tsx's "
        "RELOAD_FLAG/COOLDOWN_MS pattern, or a daemon that flaps between "
        "versions mid-bounce reload-loops the tab")
    assert re.search(r"guardedReload\(", on_version), (
        "onVersion's mismatch branch must call a guarded-reload helper")


def test_dev_bundle_watch_reload_is_guarded():
    """AC-2: the dev-mode bundle-watch poll's reload call site must route
    through the SAME guarded-reload helper (shared cooldown key) as the SSE
    path, so both channels tripping in quick succession still reload once."""
    src = _nocomments(_VERSION_TS)
    poll = _enclosing_function(src, "const poll = () => {\n    fetch(\"/api/version\"")
    assert "window.location.reload()" not in poll, (
        "startDevBundleWatch's poll callback must not call "
        "window.location.reload() directly -- it needs the same guard as "
        "the SSE watchdog path")
    assert re.search(r"guardedReload\(", poll), (
        "startDevBundleWatch's poll callback must call the guarded-reload "
        "helper")


def test_guarded_reload_helper_is_time_boxed_and_shared():
    """AC-2: a single guardedReload helper exists, mirrors main.tsx's
    sessionStorage-timestamp + cooldown-comparison guard, and both call
    sites in this file share the SAME cooldown key so one bounce sequence
    that trips both channels still reloads only once."""
    src = _nocomments(_VERSION_TS)
    assert "function guardedReload" in src, (
        "version.ts must define a guardedReload helper mirroring "
        "main.tsx's RELOAD_FLAG/COOLDOWN_MS pattern")
    fn = _enclosing_function(src, "function guardedReload")
    assert "sessionStorage" in fn and "reload()" in fn, (
        "guardedReload must gate window.location.reload() behind a "
        "sessionStorage timestamp, exactly like main.tsx's "
        "recoverFromStaleChunk")
    assert re.search(r"Date\.now\(\)\s*-\s*\w+\s*<\s*\w*[Cc]ooldown", fn), (
        "guardedReload must compare Date.now() against the stored "
        "timestamp under a cooldown window -- without it a flapping "
        "daemon reload-loops the tab")

    calls = re.findall(r"guardedReload\(\s*([\"'])(.*?)\1", src)
    assert len(calls) == 2, (
        f"expected exactly 2 guardedReload(...) call sites (SSE onVersion "
        f"and the dev-mode poll), found {len(calls)}: {calls}")
    keys = {key for _, key in calls}
    assert len(keys) == 1, (
        "both guardedReload call sites must share the SAME cooldown key -- "
        f"otherwise a bounce that trips the SSE path and the dev-poll path "
        f"in the same window reloads twice, found distinct keys: {keys}")


def test_no_new_poll_loop_introduced():
    """AC-3: the fix rides the existing SSE hello and the existing 2s/15s
    fallback polls -- it must not add a new setInterval anywhere in this
    file (the pre-fix baseline is exactly 2: the dev-bundle 2s poll and the
    live-watchdog 15s fallback poll)."""
    src = _nocomments(_VERSION_TS)
    assert len(re.findall(r"setInterval\(", src)) == 2, (
        "version.ts must not gain a new setInterval -- the mismatch check "
        "has to ride the existing SSE hello / fallback polls, per this "
        "task's stop_if")


def test_sse_disconnect_never_triggers_reload():
    """AC-4: sharedStream's onerror/onHealth handling must never reach a
    reload -- a bounce briefly drops the SSE connection before the new
    daemon is up, and reloading at that moment lands the user on a
    connection-refused page instead of the new build."""
    stream_src = _nocomments(_SHARED_STREAM_TS)
    on_error = _enclosing_function(stream_src, "es.onerror = () => {")
    assert "reload" not in on_error.lower(), (
        "sharedStream.ts's es.onerror handler must not call reload or "
        "guardedReload -- disconnect alone is not a version mismatch")
    assert "guardedReload" not in stream_src, (
        "sharedStream.ts must not import or reference guardedReload at "
        "all -- the reload decision belongs solely to version.ts's "
        "version-comparison branches, gated on a successfully parsed "
        "differing version value")
