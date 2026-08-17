"""LiveBar refreshes while SSE is healthy (task 4d399e0a).

THE BUG (components/LiveBar.tsx): the /sse/live EventSource wires only
onopen -> sseHealthy.current = true / onerror -> false, no onmessage. The
sole refresh path is `if (!document.hidden && !sseHealthy.current) load();`
on a 5s interval -- since onopen fires almost immediately on a healthy
connection, sseHealthy.current stays true forever and load() never runs
again after mount. The tile freezes on a HEALTHY connection (the inverse
of the honest-liveness property task 2d480b08 built).

THE FIX (plan_doc section 3): subscribe to /sse/sessions?project=<p>
(already forwards task.changed on every conductor step/gate write) with an
onmessage handler reaching load(); delete sseHealthy and its inverted
guard; keep a 2s fallback sweep gated on VISIBILITY + STALENESS only
(never connection health).

The SPA ships no JS test runner, so these ACs are pinned by parsing the
ACTUAL TSX source with comments stripped and enclosing if/brace scopes
walked -- never a fixed character window, a bare identifier, or a comment
(convention: tests/unit/test_conductor_page_animated_cleanup_ui.py:4-6,
test_task_page_payload_scope.py, test_heavy_polls_are_scoped.py).
"""

from __future__ import annotations

import re
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
_WEB = _SERVICE_ROOT / "prism_service" / "web" / "src"


def _read(rel: str) -> str:
    p = _WEB / rel
    assert p.exists(), f"expected source missing: {p}"
    return p.read_text(encoding="utf-8")


def _strip_comments(src: str) -> str:
    """Remove `//...` and `/* ... */` JS/TSX comments, honestly -- tracking
    string/template state so a `//` or `/*` INSIDE a quoted literal is never
    mistaken for a comment opener. AC-6 proves this instrument is honest: a
    synthetic source where every required token lives only in a comment must
    come out with none of those tokens present."""
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


def _walk_parens(src: str, paren: int) -> int:
    """Return the index just PAST the `)` matching the `(` at `paren`."""
    depth = 0
    j = paren
    while j < len(src):
        if src[j] == "(":
            depth += 1
        elif src[j] == ")":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    raise AssertionError(f"unbalanced parens scanning from index {paren}")


def _brace_body(src: str, marker: str) -> str:
    i = src.index(marker)
    return _walk_braces(src, src.index("{", i))


def _all_if_blocks(src: str):
    """Every `if (COND) { ... }` / `if (COND) STMT;` in `src`, as
    (cond_text, body_start, body_end) -- body is brace-balanced when a `{`
    follows, else the single statement up to the next top-level `;`."""
    blocks = []
    i = 0
    while True:
        m = re.search(r"\bif\s*\(", src[i:])
        if not m:
            break
        paren_open = i + m.end() - 1
        cond_end = _walk_parens(src, paren_open)
        cond_text = src[paren_open + 1:cond_end - 1]
        j = cond_end
        while j < len(src) and src[j] in " \t\n":
            j += 1
        if j < len(src) and src[j] == "{":
            body = _walk_braces(src, j)
            body_start, body_end = j, j + len(body)
        else:
            semi = src.index(";", j)
            body_start, body_end = j, semi + 1
        blocks.append((cond_text, body_start, body_end))
        i = body_end
    return blocks


def _find_if_guard_for_call(src: str, call_idx: int):
    """The condition of the INNERMOST `if (...)` whose body span contains
    `call_idx`, or None if the call site sits under no if-guard at all."""
    best_cond, best_span = None, None
    for cond, bstart, bend in _all_if_blocks(src):
        if bstart <= call_idx < bend:
            span = bend - bstart
            if best_span is None or span < best_span:
                best_cond, best_span = cond, span
    return best_cond


def _guard_permits_healthy_visible(cond_text) -> bool:
    """True iff `cond_text` CAN evaluate true when sseHealthy.current===True
    and document.hidden===False. Any clause we don't recognize (a staleness
    compare, a named constant) is treated as satisfiable -- this only exists
    to catch a guard that requires the health/visibility flags THEMSELVES to
    be false, never to model arbitrary JS."""
    if cond_text is None:
        return True
    expr = re.sub(r"\s+", " ", cond_text)
    expr = re.sub(r"!\s*document\.hidden", "True", expr)
    expr = re.sub(r"document\.hidden", "False", expr)
    expr = re.sub(r"!\s*sseHealthy\.current", "False", expr)
    expr = re.sub(r"sseHealthy\.current", "True", expr)
    tokens = re.split(r"(&&|\|\|)", expr)
    py = []
    for t in tokens:
        t = t.strip()
        if t == "&&":
            py.append("and")
        elif t == "||":
            py.append("or")
        elif t in ("True", "False"):
            py.append(t)
        elif t:
            py.append("True")  # unmodeled clause (staleness, etc.) -> satisfiable
    if not py:
        return True
    try:
        return bool(eval(" ".join(py), {"__builtins__": {}}, {}))
    except Exception:
        return True


def _setinterval_callback_spans(src: str):
    """(body_text, body_start) for each setInterval(fn, ms) callback body --
    resolving a named reference (`setInterval(poll, 5000)`) to its own
    `const NAME = () => { ... }` definition, or an inline arrow's own body."""
    spans = []
    for m in re.finditer(r"\bsetInterval\(\s*", src):
        i = m.end()
        if i < len(src) and src[i] == "(":
            arrow = src.index("=>", i)
            brace = src.index("{", arrow)
            spans.append((_walk_braces(src, brace), brace))
            continue
        comma = src.index(",", i)
        name = src[i:comma].strip()
        defn = f"const {name} = "
        if defn in src:
            d = src.index(defn) + len(defn)
            arrow = src.index("=>", d)
            brace = src.index("{", arrow)
            spans.append((_walk_braces(src, brace), brace))
    return spans


def _onmessage_body_span(src: str):
    marker = ".onmessage = ("
    if marker not in src:
        return None
    i = src.index(marker)
    arrow = src.index("=>", i)
    brace = src.index("{", arrow)
    return (_walk_braces(src, brace), brace)


def _recurring_load_call_indices(src: str):
    """Absolute indices of every `load(` call site that lives inside a
    RECURRING trigger -- a setInterval callback or an SSE onmessage handler
    -- excluding the one-shot `load();` a mount effect fires exactly once
    and can never fire again, which must not count as a 'refresh path'."""
    indices = []
    for body, body_start in _setinterval_callback_spans(src):
        for m in re.finditer(r"\bload\(", body):
            indices.append(body_start + m.start())
    om = _onmessage_body_span(src)
    if om:
        body, body_start = om
        for m in re.finditer(r"\bload\(", body):
            indices.append(body_start + m.start())
    return indices


def _reachable_while_healthy_visible(src: str) -> bool:
    for call_idx in _recurring_load_call_indices(src):
        guard = _find_if_guard_for_call(src, call_idx)
        if _guard_permits_healthy_visible(guard):
            return True
    return False


def _fallback_sweep_guard(src: str):
    """The guard on the load() call inside the setInterval-driven fallback
    sweep (the recurring path that is NOT the SSE onmessage handler)."""
    for body, body_start in _setinterval_callback_spans(src):
        m = re.search(r"\bload\(", body)
        if m:
            return _find_if_guard_for_call(src, body_start + m.start())
    return None


# ---------------------------------------------------------------------------
# AC-6 (instrument self-test) -- prove the stripper is honest BEFORE trusting
# it against the real component.
# ---------------------------------------------------------------------------

def test_comment_stripping_instrument_rejects_comment_only_tokens():
    synthetic = (
        "// sseHealthy.current gate removed, load() reachable, /sse/sessions wired\n"
        "/* updated Ns ago; STALE_AFTER_MS guard; document.hidden checked */\n"
        "export default function LiveBar() { return null; }\n"
    )
    stripped = _strip_comments(synthetic)
    for token in ("sseHealthy", "/sse/sessions", "updated", "STALE_AFTER_MS", "document.hidden"):
        assert token not in stripped, (
            f"comment-stripping helper failed to remove comment-only token "
            f"{token!r} -- a real assertion could be satisfied by a comment "
            f"instead of rendered code; stripped: {stripped!r}")
    assert "LiveBar" in stripped and "return null" in stripped, (
        "the stripper must not eat real (non-comment) code too")


# ---------------------------------------------------------------------------
# AC-1 -- at least one refresh path fires while SSE is healthy and visible.
# ---------------------------------------------------------------------------

def test_at_least_one_load_path_reachable_while_sse_healthy_and_visible():
    # SUPERSEDED 2026-08-12 (task 40c29b83): this machinery (load-path
    # reachability, EventSource target, staleness sweep, sseHealthy
    # retirement, heartbeat timing) moved OUT of LiveBar.tsx into the shared
    # lib/useConductorState.ts hook (FR-1/FR-3) -- the invariant survives,
    # only its location moved. Re-anchored here rather than deleted.
    src = _strip_comments(_read("lib/useConductorState.ts"))
    assert _reachable_while_healthy_visible(src), (
        "every recurring load() call site's enclosing guard requires "
        "sseHealthy to read false (or the tab to be hidden) before it can "
        "fire -- the tile can never refresh on a genuinely healthy, "
        f"visible connection. Recurring call sites found: "
        f"{_recurring_load_call_indices(src)!r}, guards: "
        f"{[_find_if_guard_for_call(src, i) for i in _recurring_load_call_indices(src)]!r}")


# ---------------------------------------------------------------------------
# AC-2 -- EventSource targets /sse/sessions (not /sse/live) and its
# onmessage handler's body reaches load() (a rendered call).
# ---------------------------------------------------------------------------

def test_eventsource_targets_sse_sessions_and_onmessage_reaches_load():
    # SUPERSEDED 2026-08-12 (task 40c29b83): this machinery (load-path
    # reachability, EventSource target, staleness sweep, sseHealthy
    # retirement, heartbeat timing) moved OUT of LiveBar.tsx into the shared
    # lib/useConductorState.ts hook (FR-1/FR-3) -- the invariant survives,
    # only its location moved. Re-anchored here rather than deleted.
    src = _strip_comments(_read("lib/useConductorState.ts"))
    m = re.search(r"new EventSource\(\s*(`[^`]*`|\"[^\"]*\"|'[^']*')", src)
    assert m, "LiveBar must open `new EventSource(...)`"
    es_arg = m.group(1)
    assert "/sse/sessions" in es_arg, (
        f"the EventSource must target /sse/sessions, not /sse/live -- "
        f"/sse/live only ever emits a connect payload plus keepalive "
        f"comments, it can never fire onmessage; got {es_arg!r}")
    assert "/sse/live" not in es_arg, f"got {es_arg!r}"

    om = _onmessage_body_span(src)
    assert om is not None, "the EventSource must wire an onmessage handler (`es.onmessage = (...) => { ... }`)"
    handler_body, _ = om
    assert re.search(r"\bload\(", handler_body), (
        "the onmessage handler's body must reach load() via a RENDERED "
        f"call, not a bare identifier or a comment; got body: {handler_body!r}")


# ---------------------------------------------------------------------------
# AC-3 -- refresh does not depend SOLELY on the visibilitychange listener:
# deleting that registration from stripped source must leave load() still
# reachable from another (recurring) path.
# ---------------------------------------------------------------------------

def test_load_still_reachable_without_the_visibilitychange_listener():
    # SUPERSEDED 2026-08-12 (task 40c29b83): this machinery (load-path
    # reachability, EventSource target, staleness sweep, sseHealthy
    # retirement, heartbeat timing) moved OUT of LiveBar.tsx into the shared
    # lib/useConductorState.ts hook (FR-1/FR-3) -- the invariant survives,
    # only its location moved. Re-anchored here rather than deleted.
    src = _strip_comments(_read("lib/useConductorState.ts"))
    mutated = re.sub(
        r'document\.addEventListener\(\s*["\']visibilitychange["\'][^;]*;',
        "",
        src,
    )
    assert mutated != src, (
        "expected a document.addEventListener(\"visibilitychange\", ...) "
        "registration in LiveBar.tsx to delete for this check")
    assert _reachable_while_healthy_visible(mutated), (
        "deleting the visibilitychange listener leaves NO recurring path "
        "to load() while SSE is healthy and the tab is visible -- refresh "
        "must not depend solely on that listener; the SSE onmessage "
        "handler and/or the fallback sweep must reach load() on their own")


# ---------------------------------------------------------------------------
# AC-4 -- the remaining interval-driven fallback keeps BOTH a visibility
# check and a named staleness threshold compare; never an unconditional
# poll of /api/conductor/state (tripwire against task 2d480b08's fix).
# ---------------------------------------------------------------------------

def test_fallback_sweep_guards_on_visibility_and_named_staleness_threshold():
    # SUPERSEDED 2026-08-12 (task 40c29b83): this machinery (load-path
    # reachability, EventSource target, staleness sweep, sseHealthy
    # retirement, heartbeat timing) moved OUT of LiveBar.tsx into the shared
    # lib/useConductorState.ts hook (FR-1/FR-3) -- the invariant survives,
    # only its location moved. Re-anchored here rather than deleted.
    src = _strip_comments(_read("lib/useConductorState.ts"))
    guard = _fallback_sweep_guard(src)
    assert guard is not None, (
        "expected a setInterval-driven fallback sweep whose callback body "
        "calls load() under an if-guard")
    assert "document.hidden" in guard, (
        f"fallback sweep guard must check tab visibility; got {guard!r}")
    assert re.search(r"[A-Z][A-Z0-9_]{2,}", guard), (
        f"fallback sweep guard must compare against a NAMED (SCREAMING_"
        f"SNAKE) staleness threshold constant, not a bare literal; got {guard!r}")
    assert re.search(r">=|>", guard), (
        f"fallback sweep guard must include a staleness comparison "
        f"(>= / >); got {guard!r}")
    assert "sseHealthy" not in guard, (
        f"fallback sweep guard must not gate on connection health -- that "
        f"is the exact bug this task fixes; got {guard!r}")


# ---------------------------------------------------------------------------
# AC-5 -- heartbeat label reads as time-since-last-fetch, never a
# poll-interval reading.
# ---------------------------------------------------------------------------

def test_heartbeat_label_reads_as_time_since_fetch_not_poll_interval():
    # SUPERSEDED 2026-08-12 (task 40c29b83): this machinery (load-path
    # reachability, EventSource target, staleness sweep, sseHealthy
    # retirement, heartbeat timing) moved OUT of LiveBar.tsx into the shared
    # lib/useConductorState.ts hook (FR-1/FR-3) -- the invariant survives,
    # only its location moved. Re-anchored here rather than deleted.
    src = _strip_comments(_read("lib/useConductorState.ts"))
    i = src.index("const heartbeat = ")
    stmt = src[i:src.index(";", i) + 1]
    assert re.search(r"poll\s*\$\{", stmt) is None, (
        f"heartbeat label must not read as a poll-interval "
        f"(`poll ${{...}}`); got {stmt!r}")
    assert "updated" in stmt and "ago" in stmt, (
        f"heartbeat label must read as time-since-last-fetch "
        f"('updated ... ago'); got {stmt!r}")
    assert "sinceFetchS" in stmt, (
        f"the 'ago' phrasing must interpolate sinceFetchS; got {stmt!r}")


# ---------------------------------------------------------------------------
# AC-7 -- PRISM_VERSION patch-bumped with a note naming this task, in the
# same commit as the LiveBar.tsx implementation change.
# ---------------------------------------------------------------------------

def test_version_patch_bumped_with_task_note():
    from prism_service.__version__ import PRISM_VERSION, PRISM_VERSION_NOTES

    parts = tuple(int(x) for x in PRISM_VERSION.split("."))
    assert parts >= (7, 10, 11), (
        f"expected PRISM_VERSION >= 7.10.11 (patch-bumped for this "
        f"user-visible fix); got {PRISM_VERSION!r}")
    assert "4d399e0a" in PRISM_VERSION_NOTES, (
        "PRISM_VERSION_NOTES must name task 4d399e0a; got head: "
        f"{PRISM_VERSION_NOTES[:200]!r}")


# ---------------------------------------------------------------------------
# Tripwire -- sseHealthy is retired entirely, not merely bypassed.
# ---------------------------------------------------------------------------

def test_sseHealthy_is_fully_retired():
    # SUPERSEDED 2026-08-12 (task 40c29b83): this machinery (load-path
    # reachability, EventSource target, staleness sweep, sseHealthy
    # retirement, heartbeat timing) moved OUT of LiveBar.tsx into the shared
    # lib/useConductorState.ts hook (FR-1/FR-3) -- the invariant survives,
    # only its location moved. Re-anchored here rather than deleted.
    src = _strip_comments(_read("lib/useConductorState.ts"))
    assert "sseHealthy" not in src, (
        "sseHealthy must be deleted along with its inverted guard, not "
        f"left dangling unused; still present in stripped source (first "
        f"400 chars around it omitted for brevity, len={len(src)})")
