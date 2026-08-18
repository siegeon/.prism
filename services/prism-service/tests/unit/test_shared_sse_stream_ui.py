"""Task b835f639: ONE shared SSE stream per browser, not one per component per tab.

THE BUG. A fresh click on Live could hang for tens of seconds while the server
answered every request in single-digit milliseconds. PRISM was starving its own
browser connection pool: the global chrome opened THREE long-lived SSE streams on
every page (lib/version.ts's /sse/live watchdog, lib/useConductorState.ts's
/sse/sessions, and a SECOND /sse/sessions opened by components/LiveBar.tsx for the
identical URL the hook had already opened), plus /sse/work on Live. Against the
browser's ~6-per-origin HTTP/1.1 cap, two tabs reach the cap and a third tab cannot
fetch even its own HTML document.

Measured 2026-08-18 on v7.11.32: with three tabs open, navigating to /sessions never
completed in 60s; with the extra tabs closed the identical URL loaded in 62ms, while
curl served that same document in 2.9ms throughout. Aspire/OTel put GET
/api/work/graph at 26.7-44.3ms and the SPA document at 1.78ms — there was never a
server-side problem to fix.

Note the stall does NOT reproduce on a single tab (single-tab queue times were
0-13ms), which is why the acceptance oracles below are multi-tab.

THE FIX. lib/sharedStream.ts owns every EventSource in the SPA:
  - in-tab: a per-URL ref-counted registry, so N subscribers to one URL share ONE
    connection and it closes only when the last subscriber leaves;
  - cross-tab: Web Locks elects one leader tab that holds the real connections and
    republishes each frame over a BroadcastChannel; followers construct nothing.
LiveBar's duplicate subscription is deleted outright rather than routed through the
new module — the hook it already calls drives the same refresh().

Convention (test_workflows_section_ui.py:11-17, test_conductor_page_animated_cleanup_ui.py:4-6):
the PRISM SPA ships no JS test runner, so UI ACs are pinned by asserting the ACTUAL
TS/TSX source with comments stripped and enclosing scopes walked — never a fixed
character window, never a bare identifier, and never satisfiable by a comment.
AC-0 below proves the instrument is honest before anything trusts it.
"""

from __future__ import annotations

import re
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
_WEB = _SERVICE_ROOT / "prism_service" / "web" / "src"

_SHARED = "lib/sharedStream.ts"


def _strip_comments(src: str) -> str:
    """Remove `//...` and `/* ... */` comments while tracking string/template
    state, so a `//` or `/*` inside a quoted literal is never mistaken for a
    comment opener. Same instrument as test_livebar_refresh_contract.py."""
    out = []
    i, n = 0, len(src)
    in_str = None
    while i < n:
        c = src[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(src[i + 1])
                i += 2
                continue
            if c == in_str:
                in_str = None
            i += 1
            continue
        if c in ("'", '"', "`"):
            in_str = c
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            j = src.find("\n", i)
            i = n if j == -1 else j
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _read(rel: str) -> str:
    p = _WEB / rel
    assert p.exists(), f"expected source missing: {p}"
    return _strip_comments(p.read_text(encoding="utf-8"))


def _walk_braces(src: str, brace: int) -> str:
    depth = 0
    j = brace
    while j < len(src):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[brace:j + 1]
        j += 1
    raise AssertionError(f"unbalanced braces scanning from index {brace}")


def _all_sources() -> list[tuple[str, str]]:
    """(relative posix path, comment-stripped source) for every .ts/.tsx under
    web/src — the whole SPA, so a stray EventSource anywhere is caught."""
    out = []
    for p in sorted(_WEB.rglob("*")):
        if p.suffix in (".ts", ".tsx") and p.is_file():
            rel = p.relative_to(_WEB).as_posix()
            out.append((rel, _strip_comments(p.read_text(encoding="utf-8"))))
    assert out, f"no TS/TSX sources found under {_WEB}"
    return out


# ---------------------------------------------------------------------------
# AC-0 (instrument self-test) — prove the stripper is honest BEFORE trusting it.
# ---------------------------------------------------------------------------

def test_comment_stripping_instrument_rejects_comment_only_tokens():
    synthetic = (
        "// new EventSource( lives here, navigator.locks.request( too\n"
        "/* BroadcastChannel and subscribeStream( in a block comment */\n"
        "export const real = 1;\n"
    )
    stripped = _strip_comments(synthetic)
    for token in ("new EventSource(", "navigator.locks.request(",
                  "BroadcastChannel", "subscribeStream("):
        assert token not in stripped, (
            f"comment-stripping helper failed to remove comment-only token "
            f"{token!r} — a real assertion below could be satisfied by a "
            f"comment instead of rendered code; stripped: {stripped!r}")
    assert "export const real = 1;" in stripped, (
        "the stripper must not eat real (non-comment) code too")


def test_comment_stripper_keeps_tokens_that_live_inside_string_literals():
    """A `//` inside a URL literal must not swallow the rest of the line."""
    synthetic = 'const u = "http://x/sse/live"; const after = 2;\n'
    stripped = _strip_comments(synthetic)
    assert "after" in stripped and "/sse/live" in stripped, (
        f"a `//` inside a string literal was misread as a comment opener; "
        f"got {stripped!r}")


# ---------------------------------------------------------------------------
# AC-3 — every EventSource construction in the SPA lives in the shared module.
# ---------------------------------------------------------------------------

def test_event_source_is_constructed_only_in_the_shared_stream_module():
    offenders = [rel for rel, src in _all_sources()
                 if "new EventSource(" in src and rel != _SHARED]
    assert offenders == [], (
        f"every EventSource in the SPA must be owned by {_SHARED} so streams "
        f"can be shared per URL and across tabs; these files still construct "
        f"their own (each one costs a connection against the browser's ~6-per-"
        f"origin cap, in EVERY open tab): {offenders!r}")


def test_the_shared_module_really_does_construct_the_stream():
    """Tripwire for a vacuous pass of the check above: the assertion must fail
    if the SPA simply stopped opening streams altogether."""
    src = _read(_SHARED)
    assert "new EventSource(" in src, (
        f"{_SHARED} must be the one place that constructs EventSource — "
        "otherwise the 'nowhere else' assertion passes vacuously and the app "
        "has no live updates at all")


# ---------------------------------------------------------------------------
# AC-2 — LiveBar's duplicate /sse/sessions subscription is gone outright.
# ---------------------------------------------------------------------------

def test_livebar_opens_no_stream_of_its_own():
    src = _read("components/LiveBar.tsx")
    assert "new EventSource(" not in src, (
        "components/LiveBar.tsx must not construct an EventSource — it opened "
        "/sse/sessions for the IDENTICAL url lib/useConductorState.ts already "
        "opens for it, and both handlers only called the same refresh()")
    assert "subscribeStream(" not in src, (
        "LiveBar's duplicate subscription must be DELETED, not merely routed "
        "through the shared module — the hook it already calls at "
        "`useConductorState(project)` drives the same refresh(), so a second "
        "subscription is redundant work no matter how cheap the transport is")


def test_livebar_still_takes_its_refresh_from_the_shared_hook():
    """Deleting the duplicate must not cost LiveBar its push-driven refresh —
    the hook is now its only path, so the wiring to it must be real."""
    src = _read("components/LiveBar.tsx")
    assert re.search(r"useConductorState\(\s*project\s*\)", src), (
        "LiveBar must still read its state from useConductorState(project) — "
        "that hook's subscription is what refreshes the bar now that LiveBar "
        "opens no stream of its own")
    assert "refresh" in src, (
        "LiveBar must still consume the hook's refresh handle")


# ---------------------------------------------------------------------------
# AC-4 / AC-5 — one leader per browser holds the sockets; followers hold none
# and still receive frames.
# ---------------------------------------------------------------------------

def test_leader_is_elected_with_an_exclusive_web_lock():
    src = _read(_SHARED)
    m = re.search(r"navigator\.locks\.request\(", src)
    assert m, (
        "the shared module must elect a leader tab with the Web Locks API "
        "(navigator.locks.request) — chosen over a hand-rolled heartbeat "
        "election because the browser releases the lock automatically when the "
        "leader tab dies, so failover never has to guess at a dead leader")
    assert re.search(r"mode\s*:\s*[\"']exclusive[\"']", src), (
        "the leader lock must be requested in exclusive mode — a shared lock "
        "would let every tab believe it is the leader and open its own streams")


def test_frames_are_fanned_out_over_a_broadcast_channel():
    src = _read(_SHARED)
    assert "new BroadcastChannel(" in src, (
        "the leader must republish frames over a BroadcastChannel so follower "
        "tabs receive live updates while holding zero connections")


def test_eventsource_construction_is_gated_on_being_the_leader():
    """The whole saving rests on followers constructing nothing."""
    src = _read(_SHARED)
    idx = src.index("new EventSource(")
    before = src[:idx]
    assert re.search(r"\bleader\b|\bisLeader\b", before, re.IGNORECASE), (
        "the EventSource construction must sit behind a leader check — if a "
        "follower tab can reach it, every tab opens its own sockets again and "
        "the connection cap is exhausted exactly as before")


def test_followers_have_a_delivery_path_that_does_not_construct_a_stream():
    """stop_if on this task's contract: the fix must not drop live updates for
    background tabs. A follower's frames arrive via the channel's handler."""
    src = _read(_SHARED)
    assert re.search(r"\.onmessage\s*=|addEventListener\(\s*[\"']message[\"']", src), (
        "the shared module must handle inbound BroadcastChannel messages so a "
        "FOLLOWER tab's subscribers still fire — without this the connection "
        "saving is bought by silently freezing every non-leader tab")


# ---------------------------------------------------------------------------
# AC-1 — per-URL ref counting: N subscribers share one socket, and it closes
# only when the last one leaves.
# ---------------------------------------------------------------------------

def test_streams_are_registered_per_url_for_sharing():
    src = _read(_SHARED)
    assert re.search(r"new Map\(", src), (
        "the shared module must keep a per-URL registry (a Map keyed by url) "
        "so two components subscribing to the SAME url share one connection "
        "instead of opening two — that duplication is this task's root cause")


def test_teardown_closes_the_stream_only_at_zero_subscribers():
    src = _read(_SHARED)
    close_idx = [m.start() for m in re.finditer(r"\.close\(\)", src)]
    assert close_idx, (
        f"{_SHARED} must close the underlying stream when its last subscriber "
        "leaves, or a route change leaks the connection forever")
    guarded = False
    for ci in close_idx:
        window = src[max(0, ci - 400):ci]
        if re.search(r"(size|length)\s*(===|==|<=)\s*0", window) or \
           re.search(r"!\s*\w+\.(size|length)", window):
            guarded = True
            break
    assert guarded, (
        "the close() must be guarded by a zero-subscriber check (e.g. "
        "`subs.size === 0`) — closing while another component is still "
        "subscribed would break the sharing this whole task is built on; "
        f"found {len(close_idx)} close() call(s), none zero-guarded")


# ---------------------------------------------------------------------------
# AC-6 — the shared module surfaces stream health, because lib/version.ts's
# fallback poll is required to stay gated on it (D-6, task 2d480b08).
# ---------------------------------------------------------------------------

def test_shared_module_surfaces_stream_health_to_its_callers():
    src = _read(_SHARED)
    assert re.search(r"\bonHealth\b", src), (
        "subscribeStream must surface an onHealth signal — lib/version.ts's "
        "15s /api/version fallback poll is required to run ONLY while the "
        "stream looks unhealthy (D-6), and it can no longer read es.onerror "
        "itself now that it owns no EventSource")
    assert re.search(r"\.onerror\s*=", src), (
        "the shared module must observe the underlying stream's error state to "
        "have anything honest to report through onHealth")


def test_version_watchdog_still_gates_its_fallback_poll_on_stream_health():
    """Re-anchored from test_task_page_payload_scope.py::
    test_version_poll_is_gated_on_sse_health — same invariant (no always-on
    poll), new location for the health signal."""
    src = _read("lib/version.ts")
    assert "new EventSource(" not in src, (
        "lib/version.ts must subscribe through the shared module rather than "
        "holding its own /sse/live socket in every tab")
    assert "subscribeStream(" in src and "/sse/live" in src, (
        "the version watchdog must still follow /sse/live — via subscribeStream")
    assert re.search(r"\bonHealth\b|\bhealthy\b", src), (
        "the watchdog must still learn whether the stream is healthy, so its "
        "15s fallback poll stays gated (D-6) instead of running always-on")
    assert "sseHealthy" in src, (
        "the sseHealthy gate itself must survive the move — an always-on poll "
        "defeats the payload-scope fix task 2d480b08 landed")


# ---------------------------------------------------------------------------
# AC-8 — user-visible fix is versioned (repo convention).
# ---------------------------------------------------------------------------

def test_version_patch_bumped_with_task_note():
    from prism_service.__version__ import PRISM_VERSION, PRISM_VERSION_NOTES

    parts = tuple(int(x) for x in PRISM_VERSION.split("."))
    assert parts >= (7, 11, 33), (
        f"expected PRISM_VERSION >= 7.11.33 (patch-bumped for this "
        f"user-visible fix); got {PRISM_VERSION!r}")
    assert "b835f639" in PRISM_VERSION_NOTES, (
        "PRISM_VERSION_NOTES must name task b835f639; got head: "
        f"{PRISM_VERSION_NOTES[:200]!r}")
