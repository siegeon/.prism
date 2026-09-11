"""UI contract tests for task 0b5dd37c "Workflows view lacks live progress
animation at every tier".

Owner directive (the real bar): "/workflows must feel like a game to watch.
Tokens and processing visibly flow through the steps of the workflow that is
running now, Factorio style. A person who opens /workflows while the runner
drives a task sees WHICH STEP IS ACTIVE, WHAT MOVES BETWEEN STEPS, and THE
LAST THREE THINGS THAT HAPPENED, within 5 seconds and with no click." Plus:
an idle page must read CALM, never frozen and never alarming.

The PRISM SPA has NO JS test runner, so these acceptance criteria are pinned
by parsing the ACTUAL TSX/TS source, comments stripped, structure (braces /
parens / JSX tag boundaries) walked -- never a fixed character window, a bare
identifier, or a comment satisfying a substring match. Convention and helper
style follow tests/unit/test_conductor_page_animated_cleanup_ui.py and
tests/unit/test_livebar_refresh_contract.py.

This file is written against the SHARED CONTRACT
(.claude/jobs/d139ee41/tmp/contract.md), not against whatever
WorkflowsPage.tsx/live/workflowGraph.ts happen to contain right now -- two
sibling agents are landing the implementation concurrently. It is expected
to be RED (missing identifiers: SETTLE_WINDOW_MS, LiveTier, useLiveTier,
data-live-dot, FLOW_UNIT_SPEED, drawFlowUnits, RECENT_EVENT_LIMIT, the
"Recent workflow activity" ticker, the quiet-state status-line copy) until
that work lands.

The four presentation tiers, exact spelling (contract.md):
  running | settling | quiet | disconnected
Quiet must show NO motion at all; settling is a one-shot verdict flourish
within SETTLE_WINDOW_MS (6000ms) of a run/task ending; disconnected is
evaluated BEFORE run state.
"""

from __future__ import annotations

import re
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
_WEB = _SERVICE_ROOT / "prism_service" / "web" / "src"
_PAGE = _WEB / "pages" / "WorkflowsPage.tsx"
_GRAPH = _WEB / "live" / "workflowGraph.ts"

_FORBIDDEN_QUIET_WORDS = ("idle", "stalled", "frozen", "no active driver")
_FORBIDDEN_QUIET_CLASSES = ("animate-pulse", "animate-[")


# ---------------------------------------------------------------------------
# Reading + structural parsing helpers (convention:
# test_livebar_refresh_contract.py) -- comments stripped so an explanatory
# comment can never satisfy an assertion meant for rendered code.
# ---------------------------------------------------------------------------

def _read(p: Path) -> str:
    assert p.exists(), f"expected source missing: {p}"
    return p.read_text(encoding="utf-8")


def _read_page() -> str:
    return _read(_PAGE)


def _read_graph() -> str:
    return _read(_GRAPH)


def _strip_comments(src: str) -> str:
    """Remove `//...` and `/* ... */` JS/TSX comments, tracking string state
    so a `//`/`/*` INSIDE a quoted literal is never mistaken for a comment
    opener (same instrument as test_livebar_refresh_contract.py)."""
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
    """Index just PAST the `)` matching the `(` at `paren`."""
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


def _const_arrow_body(src: str, name: str) -> str:
    """Body of `const NAME = (...) => { ... }` -- fails loudly (rather than
    silently grabbing an unrelated later `{`) if NAME is not a block-bodied
    arrow function."""
    marker = f"const {name} = "
    i = src.index(marker, 0)
    arrow = src.index("=>", i)
    j = arrow + 2
    while j < len(src) and src[j] in " \t\n":
        j += 1
    assert j < len(src) and src[j] == "{", f"{name} is not a block-bodied arrow function"
    return _walk_braces(src, j)


def _function_keyword_body(src: str, name: str) -> str:
    """Body of `function NAME(...) { ... }`."""
    marker = f"function {name}("
    i = src.index(marker)
    close_paren = _walk_parens(src, src.index("(", i))
    brace = src.index("{", close_paren)
    return _walk_braces(src, brace)


def _type_body(src: str, type_name: str) -> str:
    """Body of `export type NAME = { ... }`."""
    marker = f"export type {type_name} = {{"
    i = src.index(marker)
    brace = i + marker.index("{")
    return _walk_braces(src, brace)


def _binding_expr(src: str, ident: str) -> str:
    """RHS of the nearest `const/let IDENT[: Type] = ...;`, paren/brace/
    bracket-depth-aware so a nested arrow/object's own `;`-free body never
    truncates it early, and tolerant of a TS type annotation between the
    name and `=`."""
    m = re.search(rf"\b(?:const|let)\s+{re.escape(ident)}\s*(?::[^=]+)?=\s*", src)
    assert m, f"no `const {ident} = ...` binding found"
    i = m.end()
    depth = 0
    j = i
    n = len(src)
    while j < n:
        c = src[j]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == ";" and depth == 0:
            return src[i:j]
        j += 1
    return src[i:]


def _jsx_opening_tag_span(src: str, idx: int) -> str:
    """The full `<Tag ...>` / `<Tag .../>` opening-tag text containing the
    character at `idx`, walked so a `>` inside a `{...}` attribute
    expression (a ternary, a generic) never terminates the tag early."""
    start = src.rfind("<", 0, idx)
    assert start != -1, "no opening JSX tag found before this index"
    depth = 0
    j = start
    n = len(src)
    while j < n:
        c = src[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        elif c == ">" and depth == 0:
            return src[start:j + 1]
        j += 1
    raise AssertionError("unterminated JSX opening tag")


def _innermost_enclosing_braces(src: str, idx: int) -> str | None:
    """Text of the innermost `{ ... }` whose span contains `idx` (a LIFO
    brace stack: the first pair that closes AFTER passing idx while still
    containing it is, by construction, the most recently opened one) -- used
    to find a JSX conditional-render wrapper around an element. None if
    `idx` sits at top level, outside any `{}`."""
    stack: list[int] = []
    n = len(src)
    for j in range(n):
        c = src[j]
        if c == "{":
            stack.append(j)
        elif c == "}":
            if stack:
                start = stack.pop()
                if start <= idx <= j:
                    return src[start:j + 1]
        if j > idx and not stack:
            break
    return None


def _use_callback_body(src: str, name: str) -> str | None:
    """Body of `const NAME = useCallback((...) => { ... }, [...])`, or None
    if NAME isn't such a binding."""
    marker = f"const {name} = useCallback("
    try:
        i = src.index(marker)
    except ValueError:
        return None
    arrow = src.index("=>", i)
    j = arrow + 2
    while j < len(src) and src[j] in " \t\n":
        j += 1
    if j >= len(src) or src[j] != "{":
        return None
    return _walk_braces(src, j)


def _resolve_function_body(src: str, name: str) -> str | None:
    """Body of a `const NAME = (...) => {...}` or `const NAME = useCallback(
    (...) => {...})` binding, whichever exists -- used to follow a guard
    condition like `if (rowIsLive(workflow))` back to what actually decides
    liveness, since the gating logic may be factored into its own helper
    rather than inlined at the JSX site."""
    body = _use_callback_body(src, name)
    if body is not None:
        return body
    try:
        return _const_arrow_body(src, name)
    except (ValueError, AssertionError):
        return None


def _dot_class_candidates(src: str, dot_idx: int) -> list[str]:
    """Text spans that plausibly carry the live dot's conditional class or
    its gating: its own JSX opening tag; (if the class rides a
    `className={ident}` indirection) the bound expression of that
    identifier; the innermost enclosing `if (...)` condition (a factored-out
    dot renderer is typically gated by an `if` above the JSX, e.g. `if
    (rowIsLive(workflow)) { return <span data-live-dot .../>; }`); and, if
    that condition is a plain function call, the body of the called
    function -- so `rowIsLive(workflow)` resolves to what rowIsLive itself
    checks, not just its name."""
    tag = _jsx_opening_tag_span(src, dot_idx)
    candidates = [tag]
    for m in re.finditer(r"className=\{([A-Za-z_$][\w$]*)\}", tag):
        try:
            candidates.append(_binding_expr(src, m.group(1)))
        except AssertionError:
            pass
    blocks = _if_blocks_with_spans(src)
    guard = _innermost_if_guard(blocks, dot_idx)
    if guard:
        cond = guard[0]
        candidates.append(cond)
        call = re.match(r"\s*([A-Za-z_$][\w$]*)\s*\(", cond)
        if call:
            body = _resolve_function_body(src, call.group(1))
            if body:
                candidates.append(body)
    return candidates


def _top_level_directory_map_body(src: str) -> str:
    """The `.map((workflow) => { ... })` callback body under
    `nav[aria-label="Available workflows"]` -- the TOP-LEVEL directory row
    rendering (AC-4's "top-level `.map`")."""
    anchor = src.index('aria-label="Available workflows"')
    marker = ".map((workflow) => {"
    i = src.index(marker, anchor)
    brace = i + marker.index("{")
    return _walk_braces(src, brace)


def _render_branch_body(src: str) -> str:
    """The recursive `renderBranch` row-rendering body (WorkflowsPage.tsx,
    the `.flatMap((child) => { ... })` callback) -- AC-4's other required
    render site."""
    marker = ".flatMap((child) => {"
    i = src.index(marker)
    brace = i + marker.index("{")
    return _walk_braces(src, brace)


_ACTIVE_SIGNAL_RE = re.compile(
    r'"running"|\brunning\b|\bpending\b|\bworking\b|\bdriving\b|\bfailed\b|gate_state'
    r'|"settling"|\bsettling\b'
)
_DOT_SIGNAL_RE = re.compile(r'"running"|\brunning\b|activity\??\.state')

_FORBIDDEN_ANIMATED_PROPS = ("width", "top", "left", "height", "box-shadow")


def _keyframe_blocks(text: str) -> list[str]:
    blocks = []
    for m in re.finditer(r"@keyframes\s+[\w-]+\s*\{", text):
        blocks.append(_walk_braces(text, text.index("{", m.start())))
    return blocks


def _if_blocks_with_spans(src: str):
    """Like `_if_blocks` but also returns absolute (body_start, body_end)
    offsets, so the INNERMOST enclosing `if` for an arbitrary offset can be
    found -- a nested ternary inside an outer `if`'s body (e.g. `if (x) {
    return cond ? A : B; }`) must be judged on its OWN, closer guard, not the
    outer `if`'s condition alone."""
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
            stmt = _walk_braces(src, j)
            body_start, body_end = j, j + len(stmt)
        else:
            semi = src.index(";", j)
            body_start, body_end = j, semi + 1
        blocks.append((cond_text, body_start, body_end))
        i = body_end
    return blocks


def _innermost_if_guard(blocks, idx: int):
    """(cond, body_start, body_end) of the smallest-span `if` block
    containing `idx`, or None (convention: test_livebar_refresh_contract.py's
    `_find_if_guard_for_call`)."""
    best, best_span = None, None
    for cond, bstart, bend in blocks:
        if bstart <= idx < bend:
            span = bend - bstart
            if best_span is None or span < best_span:
                best, best_span = (cond, bstart, bend), span
    return best


def _tone_function_findings(name: str, src: str) -> list[str]:
    """Everywhere `name`'s body uses a forbidden quiet word, or applies a
    motion class (`animate-pulse`/`animate-[`) whose NEAREST guard -- the
    innermost enclosing `if` condition, plus any ternary condition text
    between that block's start (or, outside any `if`, the previous top-level
    `;`) and the occurrence itself -- names no real active-occupancy signal
    (running/pending/working/driving/failed/gate_state) and no one-shot
    `settling` tier (AC-5 explicitly allows a bounded, non-repeating
    flourish there)."""
    body = _const_arrow_body(src, name)
    findings = []
    for word in _FORBIDDEN_QUIET_WORDS:
        pattern = re.escape(word) if " " in word else rf"\b{re.escape(word)}\b"
        if re.search(pattern, body, re.IGNORECASE):
            findings.append(f"{name} body contains forbidden word {word!r}")
    blocks = _if_blocks_with_spans(body)
    for m in re.finditer(r"animate-pulse|animate-\[", body):
        idx = m.start()
        guard = _innermost_if_guard(blocks, idx)
        if guard:
            cond, bstart, _bend = guard
            context = cond + " " + body[bstart:idx]
        else:
            prior_semi = body.rfind(";", 0, idx)
            context = body[prior_semi + 1:idx] if prior_semi != -1 else body[:idx]
        if not _ACTIVE_SIGNAL_RE.search(context):
            findings.append(
                f"{name}: {m.group(0)!r} at body offset {idx} has no enclosing "
                f"active/settling signal -- nearest context: {context.strip()[-160:]!r}"
            )
    return findings


# ---------------------------------------------------------------------------
# quiet (AC-2): no motion, no alarm words, in the branches that run when
# nothing is happening.
# ---------------------------------------------------------------------------

def test_quiet_run_pill_tone_has_no_forbidden_motion_or_words():
    """runPillTone's non-active branches carry no animate-pulse/animate-[
    and no idle/stalled/frozen/"no active driver" wording."""
    src = _strip_comments(_read_page())
    findings = _tone_function_findings("runPillTone", src)
    assert not findings, "\n".join(findings)


def test_quiet_conductor_pill_tone_has_no_forbidden_motion_or_words():
    """conductorPillTone's non-active branches carry no animate-pulse/
    animate-[ and no idle/stalled/frozen/"no active driver" wording."""
    src = _strip_comments(_read_page())
    findings = _tone_function_findings("conductorPillTone", src)
    assert not findings, "\n".join(findings)


def test_quiet_directory_dot_has_no_forbidden_words():
    """Whatever conditional expression drives the live dot's class, the
    non-motion words idle/stalled/frozen/"no active driver" must never
    appear in it -- animate-pulse/animate-[ are excluded from THIS check
    because they legitimately appear on the dot's own running branch; their
    placement is pinned separately (see catalog_row tests below)."""
    src = _strip_comments(_read_page())
    idxs = [m.start() for m in re.finditer(r"data-live-dot", src)]
    assert idxs, "data-live-dot not present in the source yet"
    for idx in idxs:
        for cand in _dot_class_candidates(src, idx):
            for word in _FORBIDDEN_QUIET_WORDS:
                assert word not in cand.lower(), (
                    f"live dot expression must not use {word!r}: {cand[:160]!r}"
                )


def test_quiet_status_line_copy_has_no_forbidden_words():
    """The quiet status-line copy ("No run in progress" / "No runs yet")
    must not sit inside a conditional wrapper that also carries a motion
    class or an alarm word. Scoped to the exact ternary/expression bound to
    the status-line text (found by binding name, falling back to the
    innermost enclosing `{}` and then a bounded window) -- NOT the whole
    enclosing component function, which would false-positive on unrelated
    "idle" text elsewhere on the page (e.g. the pre-existing Brain-learning
    status badge)."""
    src = _strip_comments(_read_page())
    idx = src.find("No run in progress")
    assert idx != -1, "quiet status-line copy 'No run in progress' not found yet"
    region = None
    for ident in ("statusLineText", "statusText", "quietStatusText", "tierStatusText"):
        try:
            candidate = _binding_expr(src, ident)
        except AssertionError:
            continue
        if "No run in progress" in candidate:
            region = candidate
            break
    if region is None:
        region = _innermost_enclosing_braces(src, idx)
    if region is None:
        region = src[max(0, idx - 200):idx + 200]
    for word in _FORBIDDEN_QUIET_WORDS + _FORBIDDEN_QUIET_CLASSES:
        assert word not in region, f"quiet status line region must not contain {word!r}: {region[:300]!r}"


# ---------------------------------------------------------------------------
# status_line (AC-3): exact quiet-state copy.
# ---------------------------------------------------------------------------

def test_status_line_no_run_in_progress_copy():
    src = _read_page()
    assert "No run in progress" in src


def test_status_line_last_run_separator_copy():
    src = _read_page()
    assert " · last run " in src


def test_status_line_no_runs_yet_copy():
    src = _read_page()
    assert "No runs yet" in src


# ---------------------------------------------------------------------------
# catalog_row (AC-4 + AC-9): the live dot, everywhere it must render, gated
# correctly, animating only transform/opacity.
# ---------------------------------------------------------------------------

def test_catalog_row_live_dot_present_in_both_render_sites():
    """AC-4: the dot renders in BOTH the top-level directory `.map` AND the
    recursive renderBranch -- not just one of them. Accepts either the raw
    `data-live-dot` JSX inlined at both sites, or a call to a shared
    extracted renderer (e.g. `renderLiveDot(...)`) whose OWN body carries
    `data-live-dot` -- factoring the dot into one function and calling it
    from both sites is a legitimate way to satisfy "applies at every
    nesting depth"."""
    src = _strip_comments(_read_page())
    top_body = _top_level_directory_map_body(src)
    branch_body = _render_branch_body(src)

    renderer_name = None
    for m in re.finditer(r"\bconst\s+([A-Za-z_$][\w$]*)\s*=\s*useCallback\(", src):
        body = _use_callback_body(src, m.group(1))
        if body and "data-live-dot" in body:
            renderer_name = m.group(1)
            break
    call_marker = f"{renderer_name}(" if renderer_name else None

    def _has_dot(body: str) -> bool:
        return "data-live-dot" in body or bool(call_marker and call_marker in body)

    assert _has_dot(top_body), "the top-level workflow directory row must render a live dot (inline or via an extracted renderer)"
    assert _has_dot(branch_body), "renderBranch's nested rows must render a live dot too (inline or via an extracted renderer)"


def test_catalog_row_live_dot_running_class_is_exact():
    """AC-4: the running class string is exactly
    `bg-[color:var(--accent-solid)] animate-pulse`."""
    src = _strip_comments(_read_page())
    idxs = [m.start() for m in re.finditer(r"data-live-dot", src)]
    assert idxs, "data-live-dot not present in the source yet"
    found = any(
        "bg-[color:var(--accent-solid)] animate-pulse" in cand
        for idx in idxs
        for cand in _dot_class_candidates(src, idx)
    )
    assert found, "the live dot must apply the exact running class string bg-[color:var(--accent-solid)] animate-pulse"


def test_catalog_row_live_dot_gated_on_running_or_activity_state():
    """AC-4: the running class is reachable only under a real occupancy
    signal -- a running run status, or activity.state working/driving --
    never unconditionally. Recognizes two legitimate shapes: (1) an INLINE
    ternary carrying both the class and the signal in the same expression,
    and (2) the class sitting inside an element returned under a STRUCTURAL
    `if (cond) return <span className="...animate-pulse" />;` guard, where
    `cond` (or, if `cond` is a bare call like `rowIsLive(workflow)`, that
    function's own body) names the signal -- gating the RENDER rather than
    the class string is an equally valid, and arguably cleaner, way to
    satisfy "never unconditionally"."""
    src = _strip_comments(_read_page())
    idxs = [m.start() for m in re.finditer(r"data-live-dot", src)]
    assert idxs, "data-live-dot not present in the source yet"
    blocks = _if_blocks_with_spans(src)
    ok = False
    for idx in idxs:
        candidates = _dot_class_candidates(src, idx)
        has_class = any("bg-[color:var(--accent-solid)] animate-pulse" in c for c in candidates)
        if not has_class:
            continue
        inline_ok = any(
            "bg-[color:var(--accent-solid)] animate-pulse" in c
            and _DOT_SIGNAL_RE.search(c)
            and ("?" in c.replace("?.", "") or "&&" in c)
            for c in candidates
        )
        if inline_ok:
            ok = True
            continue
        guard = _innermost_if_guard(blocks, idx)
        if not guard:
            continue
        cond = guard[0]
        signal_here = bool(_DOT_SIGNAL_RE.search(cond))
        if not signal_here:
            call = re.match(r"\s*([A-Za-z_$][\w$]*)\s*\(", cond)
            if call:
                body = _resolve_function_body(src, call.group(1))
                signal_here = bool(body and _DOT_SIGNAL_RE.search(body))
        if signal_here:
            ok = True
    assert ok, (
        "the live dot's running class must be conditioned (inline ternary, or "
        "an enclosing if-guard possibly resolved through a helper function) "
        "on a real running/activity.state signal"
    )


def test_catalog_row_live_dot_has_no_depth_or_tier_guard():
    """AC-4: "No depth or `tier` guard" -- the dot must not be wrapped in a
    conditional that gates on nesting depth or workflow.tier."""
    src = _strip_comments(_read_page())
    idxs = [m.start() for m in re.finditer(r"data-live-dot", src)]
    assert idxs, "data-live-dot not present in the source yet"
    for idx in idxs:
        wrapper = _innermost_enclosing_braces(src, idx) or ""
        assert not re.search(r"\bdepth\b", wrapper), (
            f"live dot rendering must not be gated on nesting depth: {wrapper[:160]!r}"
        )
        assert not re.search(r"\.tier\b", wrapper), (
            f"live dot rendering must not be gated on workflow.tier: {wrapper[:160]!r}"
        )


def test_catalog_row_new_keyframe_animations_are_transform_opacity_only():
    """AC-9 (60fps budget): any @keyframes this feature adds -- for the
    settling flourish or the Factorio-style flow units -- may only animate
    transform/opacity, never width/top/left/height/box-shadow. Scans every
    CSS/TSX/TS source under web/src since a keyframe block may legally live
    in a stylesheet or an inline template string."""
    offenders = []
    for path in list(_WEB.rglob("*.css")) + list(_WEB.rglob("*.tsx")) + list(_WEB.rglob("*.ts")):
        text = path.read_text(encoding="utf-8")
        for block in _keyframe_blocks(text):
            for prop in _FORBIDDEN_ANIMATED_PROPS:
                if re.search(rf"(?<![\w-]){re.escape(prop)}\s*:", block):
                    offenders.append(f"{path.relative_to(_WEB)}: animates {prop!r}: {block[:160]!r}")
    assert not offenders, "\n".join(offenders)


# ---------------------------------------------------------------------------
# settling (AC-5): SETTLE_WINDOW_MS, the one-shot verdict flourish.
# ---------------------------------------------------------------------------

def test_settle_window_ms_exported_and_equals_6000():
    src = _read_page()
    assert re.search(r"export\s+const\s+SETTLE_WINDOW_MS\s*=\s*6000\s*;", src), (
        "WorkflowsPage.tsx must export const SETTLE_WINDOW_MS = 6000;"
    )


def test_active_node_progress_declares_tone_success_failure():
    src = _read_graph()
    type_body = _type_body(src, "ActiveNodeProgress")
    m = re.search(r"tone\?:\s*([^;]+);", type_body)
    assert m, "ActiveNodeProgress must declare an optional `tone` field"
    field = m.group(1)
    assert '"success"' in field and '"failure"' in field, (
        f'ActiveNodeProgress.tone must include "success" and "failure": {field!r}'
    )


def test_settling_tone_reaches_active_progress_construction():
    """A `tone: "success"|"failure"` value must reach at least one
    `activeProgress = {...}` construction, gated by the settling tier --
    checked via the construction's OWN body plus its innermost enclosing
    `if`/`else if` guard, since the gate is typically the surrounding
    condition (`else if (tier === "settling" ...) { activeProgress = {...
    tone: ... }; }`), not a line inside the object literal itself."""
    src = _strip_comments(_read_page())
    assert "SETTLE_WINDOW_MS" in src, "the page must reference SETTLE_WINDOW_MS to drive the settling flourish"
    blocks = _if_blocks_with_spans(src)
    found = False
    for m in re.finditer(r"activeProgress\s*=\s*\{", src):
        brace = src.index("{", m.start())
        body = _walk_braces(src, brace)
        if not re.search(r'tone:\s*.*("success"|"failure")', body, re.DOTALL):
            continue
        guard = _innermost_if_guard(blocks, brace)
        context = (guard[0] if guard else "") + " " + body
        if "SETTLE_WINDOW_MS" in context or '"settling"' in context:
            found = True
    assert found, (
        "at least one activeProgress construction carrying a success/failure "
        'tone must be gated by the settling tier (SETTLE_WINDOW_MS / "settling")'
    )


def test_settling_tone_reaches_last_rail_pill():
    """AC-5: the one-shot verdict-toned class reaches the last rail pill --
    runPillTone/conductorPillTone or the railPills construction around them
    must reference the settling tier (SETTLE_WINDOW_MS drives it; the
    consumption site itself typically reads the derived `tier === "settling"`
    rather than re-naming the constant)."""
    src = _strip_comments(_read_page())
    assert "SETTLE_WINDOW_MS" in src
    run_tone_body = _const_arrow_body(src, "runPillTone")
    conductor_tone_body = _const_arrow_body(src, "conductorPillTone")
    rail_pills_expr = _binding_expr(src, "railPills")
    combined = run_tone_body + conductor_tone_body + rail_pills_expr
    assert "SETTLE_WINDOW_MS" in combined or '"settling"' in combined, (
        "the last rail pill's tone must reference the settling tier for the one-shot settling flourish"
    )


def test_settling_tone_reaches_directory_dot():
    """AC-5: the one-shot verdict-toned class also reaches the directory
    dot -- checked directly on the top-level map / renderBranch bodies, and
    (since the dot is legitimately factored into a shared renderer, see
    test_catalog_row_live_dot_present_in_both_render_sites) on that
    renderer's own body too."""
    src = _strip_comments(_read_page())
    assert "SETTLE_WINDOW_MS" in src
    top_body = _top_level_directory_map_body(src)
    branch_body = _render_branch_body(src)
    renderer_body = ""
    for m in re.finditer(r"\bconst\s+([A-Za-z_$][\w$]*)\s*=\s*useCallback\(", src):
        body = _use_callback_body(src, m.group(1))
        if body and "data-live-dot" in body:
            renderer_body = body
            break
    combined = top_body + branch_body + renderer_body
    assert "SETTLE_WINDOW_MS" in combined or '"settling"' in combined, (
        "the directory-row dot must reference the settling tier for the one-shot settling flourish"
    )


# ---------------------------------------------------------------------------
# disconnected (AC-6): checked before run state, inside useLiveTier.
# ---------------------------------------------------------------------------

def test_disconnected_checked_before_running_inside_use_live_tier():
    src = _strip_comments(_read_page())
    assert "function useLiveTier(" in src, "useLiveTier must be a `function useLiveTier(...)` declaration"
    body = _function_keyword_body(src, "useLiveTier")
    conn = re.search(r"connectionInterrupted", body)
    running = re.search(r'"running"', body)
    assert conn, "useLiveTier must read connectionInterrupted"
    assert running, "useLiveTier must branch on the running tier"
    assert conn.start() < running.start(), (
        "useLiveTier must test connectionInterrupted BEFORE it tests run state "
        f"(connectionInterrupted at {conn.start()}, running check at {running.start()})"
    )


# ---------------------------------------------------------------------------
# flow (owner directive): Factorio-style tokens moving between steps, and
# the recent-events ticker.
# ---------------------------------------------------------------------------

def test_flow_unit_speed_exported():
    src = _read_graph()
    assert re.search(r"export\s+const\s+FLOW_UNIT_SPEED\s*[:=]", src), (
        "live/workflowGraph.ts must export const FLOW_UNIT_SPEED"
    )


def test_flow_draw_flow_units_defined():
    src = _read_graph()
    assert re.search(r"function\s+drawFlowUnits\s*\(", src), (
        "live/workflowGraph.ts must define function drawFlowUnits(...)"
    )


def test_flow_recent_events_ticker_has_aria_label():
    src = _strip_comments(_read_page())
    assert 'aria-label="Recent workflow activity"' in src, (
        'the page must render a container with aria-label="Recent workflow activity"'
    )


def test_flow_recent_event_limit_equals_3():
    src = _read_page()
    assert re.search(r"RECENT_EVENT_LIMIT\s*=\s*3\b", src), (
        "RECENT_EVENT_LIMIT must be defined and equal 3"
    )
