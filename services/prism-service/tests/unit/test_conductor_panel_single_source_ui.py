"""Red tests for task 40c29b83 — "Conductor page stops contradicting its own
live bar".

THE BUG: LiveBar.tsx (app-shell pulse) and ConductorPage.tsx ("Under
conductor" panel) each run their OWN independent fetch of the same
`/api/conductor/state` endpoint, hardened unequally. LiveBar seeds `polled`
false and gates its idle copy behind it (:77,334); ConductorPage seeds
`data` null with no such guard, swallows fetch failures into `setData(null)`
(:81-85), and renders "No tasks under conductor management" unconditionally
whenever `managed.length === 0` — collapsing "not fetched yet", "fetch
failed", and "genuinely empty" into one sentence. So a fresh load can show
the live bar naming a task in flow while the panel says nothing is managed,
for the identical payload.

THE FIX (plan_doc): extract ONE shared hook, web/src/lib/useConductorState.ts,
owning the single fetch, the polled/observed guard, an error flag, the SSE
push refresh, and the staleness sweep (lifted from LiveBar's existing
machinery). ConductorPage and LiveBar both consume it; ConductorPage branches
on observed/error/empty instead of `data?.managed_tasks ?? []` alone, and
imports the shared ACTIVITY_META instead of its own stale ACT_TILE map.

The SPA ships no JS test runner, so these ACs are pinned by parsing the
ACTUAL TSX/TS source with comments stripped and JSX {..} expression
containers walked by BRACE DEPTH -- never a fixed character window, a bare
identifier, or a comment (convention: test_conductor_page_animated_cleanup_ui.py,
test_livebar_refresh_contract.py, test_dashboard_hydration_skeleton_ui.py).
"""

from __future__ import annotations

import re
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
_WEB = _SERVICE_ROOT / "prism_service" / "web" / "src"
_CONDUCTOR_PAGE = _WEB / "pages" / "ConductorPage.tsx"
_LIVEBAR = _WEB / "components" / "LiveBar.tsx"
_HOOK = _WEB / "lib" / "useConductorState.ts"


def _read(p: Path) -> str:
    assert p.exists(), f"expected source missing: {p}"
    return p.read_text(encoding="utf-8")


def _strip_comments(src: str) -> str:
    """Remove `//...` and `/* ... */` JS/TSX comments honestly -- tracking
    string/template state so a `//` or `/*` INSIDE a quoted literal is never
    mistaken for a comment opener. Mirrors test_livebar_refresh_contract.py."""
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


def _all_brace_spans(src: str):
    """(start, end) for every balanced `{...}` in `src`, scanning every open
    brace (not skipping past a match) so NESTED spans are captured too --
    lets a caller pick the INNERMOST span enclosing a given index."""
    spans = []
    i, n = 0, len(src)
    while i < n:
        if src[i] == "{":
            try:
                body = _walk_braces(src, i)
            except AssertionError:
                i += 1
                continue
            spans.append((i, i + len(body)))
        i += 1
    return spans


def _innermost_enclosing(spans, idx: int):
    best = None
    for s, e in spans:
        if s <= idx < e and (best is None or (e - s) < (best[1] - best[0])):
            best = (s, e)
    return best


def _ternary_condition_before(src: str, span, marker_idx: int):
    """The text of a top-level `COND ? ... : ...` ternary's COND, where
    `marker_idx` sits inside the `?`-branch of a ternary living directly
    inside the JSX `{..}` expression container `span` -- found by scanning
    forward from the container's open brace and tracking (), [], {} depth so
    a `?` nested inside a call/array/object is never mistaken for the
    ternary operator."""
    s, e = span
    inner = src[s + 1:e - 1]
    marker_rel = marker_idx - (s + 1)
    depth = 0
    i = 0
    while i < len(inner) and i < marker_rel:
        c = inner[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "?" and depth == 0:
            return inner[:i].strip()
        i += 1
    return None


# ---------------------------------------------------------------------------
# Self-test — prove the comment stripper is honest before trusting it.
# ---------------------------------------------------------------------------

def test_comment_stripping_instrument_rejects_comment_only_tokens():
    synthetic = (
        "// useConductorState observed polled /api/conductor/state\n"
        "/* ACTIVITY_META ACT_TILE error */\n"
        "export default function X() { return null; }\n"
    )
    stripped = _strip_comments(synthetic)
    for token in ("useConductorState", "observed", "/api/conductor/state", "ACTIVITY_META"):
        assert token not in stripped, (
            f"comment-stripping helper failed to remove comment-only token "
            f"{token!r} -- a real assertion could be satisfied by a comment "
            f"instead of rendered code; stripped: {stripped!r}")
    assert "export default function X" in stripped


# ---------------------------------------------------------------------------
# FR-1 — one exported resolved source owns the single fetch + SSE + sweep.
# ---------------------------------------------------------------------------

def test_hook_module_owns_the_single_fetch():
    src = _strip_comments(_read(_HOOK))
    assert re.search(
        r"export\s+function\s+useConductorState\b|export\s+const\s+useConductorState\s*=",
        src,
    ), "expected an exported `useConductorState` hook in lib/useConductorState.ts"
    assert "/api/conductor/state" in src, (
        "the hook must own the fetch of /api/conductor/state -- FR-1's "
        "'single resolved source'")


def test_hook_owns_sse_and_staleness_machinery():
    src = _strip_comments(_read(_HOOK))
    assert "/sse/sessions" in src, (
        "the hook must own the SSE push-refresh subscription lifted from "
        "LiveBar (FR-1) -- not merely re-fetch on an interval")
    assert re.search(r"STALE_AFTER_MS|STALENESS", src), (
        "the hook must own a NAMED staleness-sweep threshold (lifted from "
        "LiveBar's STALE_AFTER_MS), so both consumers share one sweep, not "
        "two independently-tuned ones")


def test_hook_exposes_observed_flag_distinct_from_fetched_empty():
    src = _strip_comments(_read(_HOOK))
    assert re.search(r"\b(observed|polled)\b", src), (
        "FR-4: the hook must expose a named observed/polled flag so a "
        "consumer can tell 'no resolved fetch yet' from 'resolved and "
        "empty' -- none found in useConductorState.ts")


def test_hook_exposes_a_distinct_error_flag():
    src = _strip_comments(_read(_HOOK))
    assert re.search(r"\berror\b", src), (
        "FR-6: the hook must expose an error flag so a fetch failure "
        "renders as a distinct reachability branch, not silence")


# ---------------------------------------------------------------------------
# FR-2 — ConductorPage consumes the hook and retains no independent fetch.
# ---------------------------------------------------------------------------

def test_conductor_page_imports_the_shared_hook():
    src = _strip_comments(_read(_CONDUCTOR_PAGE))
    assert re.search(
        r'import\s*\{[^}]*\buseConductorState\b[^}]*\}\s*from\s*'
        r'["\']@/lib/useConductorState["\']',
        src,
    ), "ConductorPage.tsx must import useConductorState from @/lib/useConductorState"


def test_conductor_page_no_longer_fetches_state_directly():
    src = _strip_comments(_read(_CONDUCTOR_PAGE))
    assert "/api/conductor/state" not in src, (
        "FR-2: ConductorPage must not itself fetch /api/conductor/state -- "
        "that fetch belongs SOLELY to useConductorState now; found the "
        "literal directly in ConductorPage.tsx")
    assert not re.search(r"setInterval\(\s*load\s*,\s*5000\s*\)", src), (
        "FR-2: ConductorPage must retain no independent "
        "setInterval(load, 5000) poll of that endpoint")


# ---------------------------------------------------------------------------
# FR-3 — LiveBar consumes the same hook.
# ---------------------------------------------------------------------------

def test_livebar_imports_the_shared_hook():
    src = _strip_comments(_read(_LIVEBAR))
    assert re.search(
        r'import\s*\{[^}]*\buseConductorState\b[^}]*\}\s*from\s*'
        r'["\']@/lib/useConductorState["\']',
        src,
    ), "LiveBar.tsx must import useConductorState from @/lib/useConductorState"


# ---------------------------------------------------------------------------
# FR-4/5/8 — empty-state render requires the observed flag, brace-depth pinned.
# ---------------------------------------------------------------------------

def test_conductor_page_empty_state_gated_on_observed_flag():
    src = _strip_comments(_read(_CONDUCTOR_PAGE))
    marker = "<Empty"
    idx = src.find(marker)
    assert idx != -1, "expected a reachable <Empty ...> render in ConductorPage.tsx (FR-8)"
    span = _innermost_enclosing(_all_brace_spans(src), idx)
    assert span is not None, (
        "expected <Empty> to sit inside a JSX {..} expression container")
    cond = _ternary_condition_before(src, span, idx)
    assert cond is not None, (
        f"expected a ternary condition gating <Empty>; container text: "
        f"{src[span[0]:span[1]]!r}")
    assert re.search(r"\b(observed|polled)\b", cond, re.IGNORECASE), (
        f"FR-4/FR-5: the <Empty> guard must require the hook's "
        f"observed/polled flag -- `managed.length === 0` alone cannot tell "
        f"'not fetched yet' from 'fetched and genuinely empty'; got "
        f"condition: {cond!r}")


# ---------------------------------------------------------------------------
# FR-6 — a fetch failure renders as a distinct reachability branch.
# ---------------------------------------------------------------------------

def test_conductor_page_has_distinct_reachability_branch():
    src = _strip_comments(_read(_CONDUCTOR_PAGE))
    m = re.search(r"\berror\b\s*(\?|&&)", src)
    assert m, (
        "FR-6: expected a JSX conditional guarded on an `error` flag in "
        "ConductorPage.tsx -- today a fetch failure is swallowed into "
        "setData(null) with no distinct branch")
    window = src[m.start():m.start() + 400]
    assert "No tasks under conductor management" not in window, (
        "the error branch must render copy DISTINCT from the empty-state "
        "sentence, not reuse it")
    assert re.search(r"reach|unreachable|couldn.?t|failed|unavailable|retry", window, re.IGNORECASE), (
        f"FR-6: the error branch's copy must read as a reachability "
        f"problem, not an empty-queue claim; got window: {window!r}")


# ---------------------------------------------------------------------------
# FR-7 — one shared activity vocabulary; ACT_TILE deleted.
# ---------------------------------------------------------------------------

def test_conductor_page_imports_activity_meta_and_retires_act_tile():
    src = _strip_comments(_read(_CONDUCTOR_PAGE))
    assert re.search(
        r'import\s*\{[^}]*\bACTIVITY_META\b[^}]*\}\s*from\s*'
        r'["\']@/components/conductor/SdlcProgress["\']',
        src,
    ), "ConductorPage.tsx must import ACTIVITY_META from components/conductor/SdlcProgress"
    assert "ACT_TILE" not in src, (
        "FR-7: ConductorPage's own stale ACT_TILE map (no `driving` entry) "
        "must be deleted in favor of the shared ACTIVITY_META -- superseded "
        "by SdlcProgress.tsx's ACTIVITY_META (the map every other surface "
        "already renders through)")


# ---------------------------------------------------------------------------
# FR-9 — empty-state copy names the second symptom (untracked, not absent).
# ---------------------------------------------------------------------------

def test_empty_state_copy_names_second_symptom():
    src = _strip_comments(_read(_CONDUCTOR_PAGE))
    m = re.search(r"<Empty>(.*?)</Empty>", src, re.DOTALL)
    assert m, "expected an <Empty>...</Empty> block in ConductorPage.tsx"
    body = m.group(1)
    assert re.search(r"no (task|work)|zero", body, re.IGNORECASE), (
        f"expected the genuine-zero case named in the empty-state copy; "
        f"got: {body!r}")
    assert re.search(
        r"not track|status flips? only|workflow.?claimed|outside (the )?conductor",
        body, re.IGNORECASE,
    ), (
        "FR-9 (second symptom): empty-state copy must distinguish 'no work' "
        "from 'work we are not tracking' (status-flip-only tasks the "
        f"conductor's own endpoint cannot see) -- got: {body!r}")


# ---------------------------------------------------------------------------
# NFR-5 — PRISM_VERSION patch-bumped, notes name this task.
# ---------------------------------------------------------------------------

def test_version_patch_bumped_with_task_note():
    from prism_service.__version__ import PRISM_VERSION, PRISM_VERSION_NOTES

    parts = tuple(int(x) for x in PRISM_VERSION.split("."))
    assert parts >= (7, 10, 35), (
        f"expected PRISM_VERSION >= 7.10.35 (patch-bumped for this "
        f"user-visible fix); got {PRISM_VERSION!r}")
    assert "40c29b83" in PRISM_VERSION_NOTES, (
        "PRISM_VERSION_NOTES must name task 40c29b83; got head: "
        f"{PRISM_VERSION_NOTES[:200]!r}")
