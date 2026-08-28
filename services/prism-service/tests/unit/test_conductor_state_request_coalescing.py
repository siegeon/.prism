"""useConductorState coalesces concurrent same-project fetches (task 04359d5b).

THE BUG, confirmed live 2026-08-28: MIN_REFRESH_MS already throttles each
MOUNTED CONSUMER of useConductorState to one fetch per window, but does
nothing across consumers. Sidebar.tsx is mounted on every page; whichever
page-level component also calls the hook (WorkflowsPage.tsx,
ConductorPage.tsx) runs its OWN doLoad() too. A single SSE push event fans
out to every mounted consumer's own load(), so N mounted consumers still
fire N near-simultaneous GET /api/conductor/state requests. A live browser
network capture on /workflows caught 13 concurrent calls in one burst,
while the page's own one-time GET /api/workflows fetch was starved for a
connection-pool slot and the directory sidebar rendered empty.

THE FIX: a module-scope in-flight-request map (`_inFlight`), shared by
every hook instance in the tab (not component state, not a Context/store --
mx-d412e0 already rejected hoisting this hook's state into a store as
heavier than the seam needs). A second concurrent caller for the same
project gets the SAME promise instead of starting its own fetch.

The SPA ships no JS test runner, so this AC is pinned by reading the actual
TS source with comments stripped (convention:
test_conductor_page_animated_cleanup_ui.py:4-6).
"""

from __future__ import annotations

import re
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
_HOOK = _SERVICE_ROOT / "prism_service" / "web" / "src" / "lib" / "useConductorState.ts"


def _read() -> str:
    assert _HOOK.exists(), f"expected source missing: {_HOOK}"
    return _HOOK.read_text(encoding="utf-8")


def _strip_comments(src: str) -> str:
    """Remove // and /* */ comments, honoring string/template state so a
    // or /* inside a quoted literal is never mistaken for a comment
    opener. Mirrors test_conductor_panel_single_source_ui.py."""
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
        if src[i:i + 2] == "//":
            j = src.find("\n", i)
            i = n if j == -1 else j
            continue
        if src[i:i + 2] == "/*":
            j = src.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def test_module_scope_inflight_map_exists():
    src = _strip_comments(_read())
    assert re.search(r"const\s+_inFlight\s*=\s*new Map", src), (
        "expected a module-scope in-flight request map shared across "
        "every useConductorState instance in the tab")
    # Module scope, not inside the hook function -- must appear BEFORE
    # `export function useConductorState(`.
    hook_idx = src.index("export function useConductorState(")
    map_idx = src.index("const _inFlight")
    assert map_idx < hook_idx, (
        "_inFlight must be declared at module scope, before the hook "
        "function, so it is shared across every mounted instance -- "
        "declaring it inside useConductorState would make it per-instance "
        "state again, defeating the whole point")


def test_coalescer_returns_the_same_promise_for_a_concurrent_call():
    src = _strip_comments(_read())
    m = re.search(
        r"function fetchConductorState\([^)]*\)[^{]*\{(.*?)\n\}",
        src, re.S)
    assert m, "expected a fetchConductorState(project) function"
    body = m.group(1)
    assert "_inFlight.get(" in body, (
        "fetchConductorState must check the shared map for an in-flight "
        "request before starting a new one")
    assert re.search(r"if\s*\(\s*existing\s*\)\s*return\s+existing", body), (
        "a second concurrent caller for the same project must receive the "
        "SAME promise, not trigger its own fetch")
    assert "_inFlight.set(" in body, (
        "a genuinely new fetch must be recorded in the shared map so the "
        "NEXT concurrent caller can find and reuse it")
    assert "_inFlight.delete(" in body, (
        "the map entry must be cleared once the request settles (success "
        "or failure), or every later fetch for this project would "
        "incorrectly reuse a dead promise forever")


def test_doload_routes_through_the_shared_coalescer_not_a_bare_fetch():
    src = _strip_comments(_read())
    m = re.search(r"const doLoad = useCallback\(\(\) => \{(.*?)\n  \}, \[project\]\);", src, re.S)
    assert m, "expected doLoad's useCallback body"
    body = m.group(1)
    assert "fetchConductorState(project)" in body, (
        "doLoad must call the shared fetchConductorState(project), not "
        "api.get(...) directly -- calling api.get directly here would "
        "bypass the coalescer entirely and reintroduce the duplicate-"
        "request burst")
    assert "api.get<ConductorState>" not in body, (
        "doLoad must not call api.get directly anymore; the fetch belongs "
        "solely inside fetchConductorState")


def test_min_refresh_ms_still_present_unchanged():
    """Regression guard: the existing per-instance burst coalescer must
    survive this change untouched -- the fix is additive (cross-instance),
    not a replacement for the existing per-instance throttle."""
    src = _strip_comments(_read())
    assert "export const MIN_REFRESH_MS = 3_000;" in src
    assert "lastStartRef" in src and "trailingRef" in src
