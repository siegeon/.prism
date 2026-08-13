"""RED suite - "Gate card can't strand an approve on stale data" (task
a9f37cd1-053e-4195-b2fb-00467273b810).

Owner hit this 2026-08-12 on task b43b33b8: the task detail page was open
while a drive wrote plan_doc and parked plan_gate. The top banner (fed live
by /sse/tasks) correctly said "AWAITING PLAN_GATE"; the gate card (fed by a
mount-only readiness fetch, TaskDetailPage.tsx:1039-1062) still rendered a
red "BLOCKED - evidence not on file" with a disabled Approve. Only a hard
reload converged the two. See plan_doc for the full D-1..D-7 design this
suite pins: a single `refreshReadiness` choke point, a `readinessStale`
boolean derived from a fetched-at/changed-at pair plus a grace threshold,
push-triggered refresh inside the existing /sse/tasks handler, a
visibility-gated fallback sweep (no new poll interval), and an honest
neutral leading branch in both the headline/Approve/caption in
TaskDetailPage.tsx and the "evidence check - live" panel in PlanView.tsx.

The PRISM SPA has no JS test runner (test_conductor_page_animated_cleanup_ui.py:4-6),
so these are source-reading tests. Per the repeated failure mode logged in
CLAUDE.md's Lessons, every extraction below is BALANCED-BRACE, never a fixed
character window or a comment-satisfiable check - copied from the existing
`_extract_braced_expr` (test_gate_banner_honest_state_ui.py:51-67) and
`_gate_panel_block` (test_stale_gate_banner_inspect_vs_override.py:79-87)
conventions, plus the escaped-slash-guarded comment stripper
(test_stale_gate_banner_inspect_vs_override.py:171).

ALL of these are expected to FAIL against the current source (baseline
ca3ad69): `readinessStale`, `refreshReadiness` and `READINESS_STALE_AFTER_MS`
do not exist anywhere in the file yet; there is no `visibilitychange`
listener; the headline/disabled/caption expressions branch only on
`gateReadiness` with no staleness input; and PlanView.tsx's evidence panel
and prop type carry no staleness signal.
"""

from __future__ import annotations

import re
from pathlib import Path

_HERE = Path(__file__).resolve()
_SRC = _HERE.parent.parent.parent / "prism_service" / "web" / "src"
_TASK_DETAIL = _SRC / "pages" / "TaskDetailPage.tsx"
_PLAN_VIEW = _SRC / "components" / "plan" / "PlanView.tsx"


def _read(p: Path) -> str:
    assert p.exists(), f"{p} missing"
    return p.read_text(encoding="utf-8")


def _strip_comments(src: str) -> str:
    """Drop /* */, {/* */} and // comments so a comment can never satisfy a
    source assertion - with the escaped-slash guard
    (test_stale_gate_banner_inspect_vs_override.py:171) so an escaped `\\/`
    inside a regex literal is not mistaken for a `//` comment start."""
    src = re.sub(r"\{\s*/\*.*?\*/\s*\}", "", src, flags=re.S)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"(?m)(?<!:)(?<!\\)//.*$", "", src)
    return src


def _balanced(src: str, open_idx: int, open_ch: str, close_ch: str) -> int:
    """Index of the close char matching the open char at open_idx."""
    depth = 0
    for k in range(open_idx, len(src)):
        if src[k] == open_ch:
            depth += 1
        elif src[k] == close_ch:
            depth -= 1
            if depth == 0:
                return k
    raise AssertionError(f"unbalanced {open_ch}{close_ch} scanning from {open_idx}")


def _extract_braced_expr(src: str, marker: str) -> str:
    """Balanced `{...}` JSX expression starting at the first `{` at/after
    `marker` (test_gate_banner_honest_state_ui.py:51-67 convention)."""
    idx = src.index(marker)
    start = src.index("{", idx)
    return src[start:_balanced(src, start, "{", "}") + 1]


def _brace_block_from(src: str, marker: str) -> str:
    """Block starting AT the marker's own last char (must be `{`), closed
    by brace balance - for arrow-function bodies like
    `es.onmessage = (ev) => {` or `const refreshReadiness = ... => {`."""
    idx = src.index(marker)
    open_idx = idx + len(marker) - 1
    assert src[open_idx] == "{", f"marker {marker!r} does not end in '{{'"
    return src[idx:_balanced(src, open_idx, "{", "}") + 1]


def _paren_wrapped_jsx_block(src: str, marker: str) -> str:
    """`{cond && ( ... )}`-shaped block, found by paren balance from the
    marker's trailing '(' - same technique as
    test_stale_gate_banner_inspect_vs_override.py's `_gate_panel_block`."""
    start = src.index(marker)
    paren_open = start + len(marker) - 1
    assert src[paren_open] == "(", f"marker {marker!r} does not end in '('"
    close = _balanced(src, paren_open, "(", ")")
    return src[start:close + 1]


def _jsx_tag_span(src: str, open_marker: str) -> str:
    """The full `<Tag ...>` / `<Tag ... />` opening tag, scanning forward
    from `open_marker` and SKIPPING any `{...}` prop value by brace
    balance, so a `>` inside a prop expression can't be mistaken for the
    tag's own close."""
    start = src.index(open_marker)
    i = start
    while i < len(src):
        ch = src[i]
        if ch == "{":
            i = _balanced(src, i, "{", "}") + 1
            continue
        if ch == ">":
            return src[start:i + 1]
        i += 1
    raise AssertionError(f"no closing '>' found for tag at {open_marker!r}")


def _ternary_arm(expr: str, question_idx: int) -> str:
    """The TRUE arm of the ternary whose `?` sits at `question_idx` - text
    up to the matching TOP-LEVEL `:` (brace/paren depth 0), never a fixed
    slice, so a nested ternary/handler inside the arm can't confuse the
    boundary."""
    depth = 0
    i = question_idx + 1
    while i < len(expr):
        ch = expr[i]
        if ch in "{(":
            depth += 1
        elif ch in "})":
            depth -= 1
        elif ch == ":" and depth == 0:
            return expr[question_idx + 1:i]
        i += 1
    raise AssertionError(f"no matching top-level ':' for ternary at {question_idx}")


def _enclosing_use_effect(src: str, marker: str) -> str:
    """The nearest enclosing `useEffect(() => { ... }` body containing
    `marker`, found by scanning backward for the effect opener then
    forward by brace balance - so the visibility sweep's real body is
    inspected regardless of the internal handler's name."""
    idx = src.index(marker)
    ue_marker = "useEffect(() => {"
    start = src.rfind(ue_marker, 0, idx)
    assert start != -1, f"no enclosing useEffect found before {marker!r}"
    open_idx = start + len(ue_marker) - 1
    return src[start:_balanced(src, open_idx, "{", "}") + 1]


# ---------------------------------------------------------------------------
# AC-1 (FR-1) - the /sse/tasks onmessage handler also refreshes readiness,
# not only the task-field patch.
# ---------------------------------------------------------------------------


def test_ac1_sse_onmessage_also_refreshes_readiness():
    src = _strip_comments(_read(_TASK_DETAIL))
    body = _brace_block_from(src, "es.onmessage = (ev) => {")
    assert "setTask(" in body, "sanity: this must be the task-patch handler"
    assert re.search(r"refreshReadiness\s*\(", body), (
        "the /sse/tasks onmessage handler must also call refreshReadiness() "
        "when a gate-relevant field arrives - today it only patches `task`, "
        "so gateReadiness never converges while the tab stays open "
        "(task b43b33b8)"
    )


# ---------------------------------------------------------------------------
# AC-2 (FR-2, NFR-1) - visibility/focus fallback gates on document.hidden AND
# genuine staleness, matching LiveBar.tsx:134-142's push+fallback shape.
# ---------------------------------------------------------------------------


def test_ac2_visibility_fallback_gates_on_hidden_and_staleness():
    src = _strip_comments(_read(_TASK_DETAIL))
    assert "visibilitychange" in src, (
        "TaskDetailPage.tsx must register a visibilitychange listener as a "
        "fallback convergence path (FR-2), mirroring LiveBar.tsx:134-142"
    )
    block = _enclosing_use_effect(src, "visibilitychange")
    assert "document.hidden" in block, (
        "the visibility sweep must gate on document.hidden - it must not "
        "refetch while the tab is backgrounded"
    )
    assert re.search(r"performance\.now\(\)|Date\.now\(\)", block), (
        "the visibility sweep must compare elapsed time against a "
        "threshold, not fire unconditionally on every visibility event"
    )
    assert re.search(r"READINESS_STALE_AFTER_MS", block), (
        "the visibility sweep must gate on a named staleness threshold "
        "constant (matching LiveBar's STALE_AFTER_MS convention) so it "
        "does nothing when the last fetch is fresh"
    )
    assert re.search(r"refreshReadiness\s*\(\s*\)", block), (
        "the visibility sweep must call refreshReadiness() once it decides "
        "the data is genuinely stale"
    )


# ---------------------------------------------------------------------------
# AC-3 (FR-3) - a single refreshReadiness choke point stamps fetched-at on
# every path, and a named readinessStale boolean is derived from it plus a
# grace threshold.
# ---------------------------------------------------------------------------


def test_ac3_single_choke_point_stamps_fetched_at_and_derives_staleness():
    src = _strip_comments(_read(_TASK_DETAIL))
    occurrences = src.count("/api/conductor/gate/readiness")
    assert occurrences == 1, (
        f"the readiness URL literal must appear exactly once - a single "
        f"refreshReadiness() choke point every caller (mount, SSE, sweep, "
        f"mintEvidence) shares - found {occurrences} occurrence(s); with "
        f"more than one, a caller can forget to stamp fetched-at and "
        f"gateReadiness silently drifts out of sync with what the card "
        f"believes it knows"
    )
    m = re.search(r"const\s+refreshReadiness\s*=\s*useCallback\(", src)
    assert m, (
        "expected a single `const refreshReadiness = useCallback(...)` "
        "choke point that every readiness-fetch path calls"
    )
    body_start = src.index("{", m.end())
    body = src[body_start:_balanced(src, body_start, "{", "}") + 1]
    assert "setGateReadiness(" in body, (
        "refreshReadiness must be the function that actually sets "
        "gateReadiness"
    )
    assert re.search(r"[Ff]etchedAt", body), (
        "refreshReadiness must stamp a fetched-at timestamp on every "
        "successful read, so staleness has a real WHEN behind it"
    )
    assert re.search(r"const\s+readinessStale\s*=", src), (
        "TaskDetailPage.tsx must define a named readinessStale boolean "
        "derived from the fetch/change timestamps and a grace threshold"
    )
    stale_def_m = re.search(r"const\s+readinessStale\s*=([\s\S]*?);\n", src)
    assert stale_def_m and re.search(r"_MS\b", stale_def_m.group(1)), (
        "readinessStale must be derived against a named millisecond "
        "threshold constant, not a bare magic number"
    )


def _headline_stale_arm(src: str) -> tuple[str, str]:
    """(stale_true_arm, full_headline_expr) - the headline's own
    readinessStale condition and its true-arm text, extracted by balanced
    ternary scanning, never a fixed window."""
    expr = _extract_braced_expr(src, "● {")
    assert "readinessStale" in expr, (
        "the headline expression (marked '● {') must branch on "
        "readinessStale as its own condition"
    )
    cond_idx = expr.index("readinessStale")
    q_mark = expr.index("?", cond_idx)
    return _ternary_arm(expr, q_mark), expr


# ---------------------------------------------------------------------------
# AC-4 (FR-4) - the stale notice LEADS both BLOCKED arms, carries no alarm
# word, and bannerTone is not rose for the stale case.
# ---------------------------------------------------------------------------


def test_ac4_stale_headline_leads_blocked_and_tone_is_not_rose():
    src = _strip_comments(_read(_TASK_DETAIL))
    stale_arm, expr = _headline_stale_arm(src)

    cond_idx = expr.index("readinessStale")
    blocked_idx = expr.index("BLOCKED · evidence not on file")
    assert cond_idx < blocked_idx, (
        "readinessStale must be checked BEFORE the generic "
        "'BLOCKED · evidence not on file' arm - a leading branch, not a "
        "trailing one, so a stale card can never fall through to a hard "
        "BLOCKED verdict"
    )
    assert "BLOCKED" not in stale_arm.upper(), (
        "the stale headline's own copy must not itself say BLOCKED - "
        "unread data is not a verdict"
    )
    assert re.search(r"stale", stale_arm, re.IGNORECASE), (
        "the readinessStale branch's own copy should name staleness so "
        "the notice reads as 'unread data', not silence"
    )

    tone_m = re.search(r"const\s+bannerTone[^=]*=([\s\S]*?);\n", src)
    assert tone_m, "bannerTone const must still be defined"
    tone_def = tone_m.group(1)
    stale_tone_m = re.search(r'readinessStale\s*\?\s*"([a-z]+)"', tone_def)
    assert stale_tone_m, (
        "bannerTone must read `readinessStale ? \"<tone>\" : ...` so the "
        "stale case gets its own explicit tone"
    )
    assert stale_tone_m.group(1) != "rose", (
        "the stale tone must not be rose - that reads as alarm/blocked, "
        "not merely 'we have not looked recently'"
    )


# ---------------------------------------------------------------------------
# AC-5 (FR-4) - the stale notice carries a one-click Refresh affordance that
# is NOT a <button> nested inside the panel-toggle <button>.
# ---------------------------------------------------------------------------


def test_ac5_refresh_affordance_not_nested_inside_the_toggle_button():
    src = _strip_comments(_read(_TASK_DETAIL))
    stale_arm, _ = _headline_stale_arm(src)

    assert "<button" not in stale_arm, (
        "the Refresh control must not be a <button> - the headline lives "
        "inside the panel-toggle <button> (~TaskDetailPage.tsx:1587), and "
        "a <button> nested inside a <button> is invalid DOM"
    )
    assert re.search(r'role="button"', stale_arm), (
        "the Refresh affordance should be a role=\"button\" element "
        "(span/div), not a native nested button"
    )
    onclick_m = re.search(r"onClick=\{", stale_arm)
    assert onclick_m, "the stale branch must carry a click handler for Refresh"
    onclick_body = _brace_block_from(stale_arm, stale_arm[onclick_m.start():onclick_m.end()])
    assert re.search(r"refreshReadiness\s*\(\s*\)", onclick_body), (
        "the Refresh control's onClick must invoke refreshReadiness()"
    )


# ---------------------------------------------------------------------------
# AC-6 (FR-5) - Approve's disabled expr and the helper caption both consult
# readinessStale; the stale caption arm never says "override".
# ---------------------------------------------------------------------------


def test_ac6_approve_and_caption_consult_staleness():
    src = _strip_comments(_read(_TASK_DETAIL))

    disabled_expr = _extract_braced_expr(src, "disabled={busy || (gateVerdict")
    assert "readinessStale" in disabled_expr, (
        "the Approve disabled={...} expression must consult readinessStale "
        "- a merely-stale card must never be routed through the override "
        "checkbox path"
    )

    caption_expr = _extract_braced_expr(src, '{gateVerdict === "ready"')
    assert "readinessStale" in caption_expr, (
        "the helper caption ternary must also consult readinessStale"
    )
    q_idx = caption_expr.index("readinessStale")
    q_mark = caption_expr.index("?", q_idx)
    stale_caption_arm = _ternary_arm(caption_expr, q_mark)
    assert "override" not in stale_caption_arm.lower(), (
        "the stale caption arm must not say 'tick override to recover' - "
        "Refresh is the remedy that actually helps on merely-stale data"
    )


# ---------------------------------------------------------------------------
# AC-7 (FR-8, NFR-1) - the genuine BLOCKED path survives unchanged, and no
# new poll interval was introduced.
# ---------------------------------------------------------------------------


def test_ac7_blocked_path_survives_and_no_new_poll_interval():
    src = _strip_comments(_read(_TASK_DETAIL))
    expr = _extract_braced_expr(src, "● {")
    assert "BLOCKED · evidence not on file" in expr, (
        "the genuine evidence-not-on-file BLOCKED literal must survive "
        "unchanged once readiness is confirmed fresh"
    )
    assert "BLOCKED · verifier rejected current evidence" in expr, (
        "the genuine verifier-refusal BLOCKED literal must survive "
        "unchanged once readiness is confirmed fresh"
    )
    assert "setInterval" not in src, (
        "TaskDetailPage.tsx must not introduce a setInterval poll of "
        "server state - the fix piggybacks the existing SSE stream plus a "
        "visibility-gated sweep only (NFR-1 / this task's own stop_if)"
    )


# ---------------------------------------------------------------------------
# AC-9 (FR-7) - PlanView.tsx receives the staleness signal and stops
# claiming a live verdict while stale.
# ---------------------------------------------------------------------------


def test_ac9_planview_receives_and_renders_the_staleness_signal():
    src = _strip_comments(_read(_TASK_DETAIL))
    tag = _jsx_tag_span(src, "<PlanView")
    assert re.search(r"readinessStale=\{", tag), (
        "TaskDetailPage.tsx must pass the staleness signal into <PlanView "
        "(e.g. readinessStale={readinessStale}) - otherwise the panel it "
        "renders keeps claiming a live check on data the page already "
        "knows is stale"
    )

    plan_src = _strip_comments(_read(_PLAN_VIEW))
    assert re.search(r"readinessStale\??:\s*boolean", plan_src), (
        "PlanView.tsx's prop type must declare readinessStale?: boolean "
        "beside the existing gateReadiness prop"
    )
    panel = _paren_wrapped_jsx_block(plan_src, "{gateReadiness && (")
    assert "readinessStale" in panel, (
        "PlanView's 'evidence check · live' panel must consult the "
        "readinessStale prop, not just gateReadiness, so it stops "
        "claiming a live verdict while the data behind it is stale"
    )
    assert "may be stale" in panel, (
        "the panel must render a distinct, honest label while stale (e.g. "
        "'evidence check · may be stale'), not the unconditional "
        "'evidence check · live' plus a verdict"
    )
