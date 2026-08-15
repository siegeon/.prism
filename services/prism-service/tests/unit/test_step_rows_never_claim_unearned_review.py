"""A currently-EXECUTING step row must never claim review it has not had.

Task 122ff356 (plan_doc has the full FR/AC list; this suite pins AC-1..AC-5).
The PRISM SPA has no JS test runner, so these ACs are pinned by asserting the
ACTUAL rendered web source (same pattern as
tests/unit/test_conductor_page_animated_cleanup_ui.py:4-6 and
tests/unit/test_cancelled_task_gate_card_inert.py:52-77, whose
`_strip_comments`/balanced-brace-extraction helpers are reused verbatim below
— a comment or an inserted line must never be able to satisfy an assertion
the real guard is supposed to own).

Bug: StepRail.tsx:319 renders a "verified by {Role}" pill on the CURRENT
non-gate step row purely off step POSITION — `verifierPersona`
(StepRail.tsx:250-251) is a lookahead into the static WORKFLOW_STEPS_ORDERED
table, never a query against the `gates: GanttGate[]` array of REAL
gate_decide receipts that `gateFor`/`gateInfo` (StepRail.tsx:217-224) already
resolve for GateRow. Observed live: task 2419bff2 mid implement_tasks showed
"VERIFIED BY STEWARD" beside the live blue implement row while the builder
was still working. Same pattern in SdlcProgress.tsx's "awaiting review"
header caption, which currently flows `activity?.state` straight into
ACTIVITY_META with no cross-check against the current step's own resolved
`type` — even though SdlcProgress already has `steps`/`curIdx` in hand.

AC-4 pins the flip side: GateRow's real passed/override receipt path
(StepRail.tsx:302-303) must render completely unchanged.
"""

from __future__ import annotations

import re
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
_SRC = _SERVICE_ROOT / "prism_service" / "web" / "src"
_RAIL = _SRC / "components" / "conductor" / "StepRail.tsx"
_PROGRESS = _SRC / "components" / "conductor" / "SdlcProgress.tsx"


def _read(p: Path) -> str:
    assert p.exists(), f"missing source file: {p}"
    return p.read_text(encoding="utf-8")


def _strip_comments(src: str) -> str:
    """Drop /* */, {/* */} and // comments so a comment can never satisfy a
    source assertion. Reused verbatim from
    tests/unit/test_cancelled_task_gate_card_inert.py:52-64."""
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


def _reviewer_tag(src: str) -> str:
    """The rendered tag for the forward-looking reviewer pill component —
    `<VerifiedBy` today, `<ReviewRole` once the fix lands (plan_doc renames
    it so a bare completed-action claim is structurally unreachable without
    a `decided` prop). Matched on the RENDERED TAG, never a bare name or a
    comment naming the old one."""
    for tag in ("ReviewRole", "VerifiedBy"):
        if f"<{tag}" in src:
            return tag
    raise AssertionError(
        "no reviewer-pill component render tag (<ReviewRole ...> or "
        "<VerifiedBy ...>) found in StepRail.tsx"
    )


def _enclosing_jsx_expr(src: str, tag: str) -> str:
    """The balanced `{...}` JSX expression that renders `<tag`, found by
    scanning OUTWARD from the tag's first use to its innermost enclosing
    brace pair — never a fixed line/character window (a line-wrap or
    reformat must not desync this)."""
    idx = src.index(f"<{tag}")
    depth = 0
    open_idx = None
    i = idx
    while i >= 0:
        c = src[i]
        if c == "}":
            depth += 1
        elif c == "{":
            if depth == 0:
                open_idx = i
                break
            depth -= 1
        i -= 1
    assert open_idx is not None, f"no enclosing brace found for <{tag}"
    close_idx = _balanced(src, open_idx, "{", "}")
    return src[open_idx : close_idx + 1]


def _component_function_body(src: str, name: str) -> str:
    """The `function <name>(...) { ... }` body, found by paren/brace
    balancing from the `function <name>(` marker — never a fixed window."""
    marker = f"function {name}("
    start = src.index(marker)
    paren_open = src.index("(", start)
    paren_close = _balanced(src, paren_open, "(", ")")
    brace_open = src.index("{", paren_close)
    brace_close = _balanced(src, brace_open, "{", "}")
    return src[brace_open : brace_close + 1]


def _const_stmt(src: str, name: str, start: int = 0) -> str:
    """A `const <name> = ...;` statement, found by scanning to the
    terminating `;` at paren/brace/bracket depth 0 — never a fixed
    character window. Pattern reused from
    tests/unit/test_gate_actor_chip_ui.py."""
    marker = f"const {name} ="
    begin = src.index(marker, start)
    depth = 0
    i = begin
    while i < len(src):
        c = src[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == ";" and depth == 0:
            return src[begin : i + 1]
        i += 1
    raise AssertionError(f"unterminated const {name} statement")


def _caption_state_region(src: str) -> str:
    """The exact computation deciding which ACTIVITY_META entry (and thus
    which caption string, e.g. 'awaiting review') reaches the screen: from
    `const state = (activity?.state ...)` through the `const stateLabel =`
    statement. Bounded so an UNRELATED `.type === "gate"` check used for
    segment styling elsewhere in the same file (the steps.map loop further
    down, computing bar opacity) can't satisfy this assertion."""
    start = src.index("const state = (activity?.state")
    stmt = _const_stmt(src, "stateLabel", start)
    end = src.index(stmt, start) + len(stmt)
    return src[start:end]


# ---------------------------------------------------------------------------
# Self-check: the comment-stripper actually strips a planted trap comment
# that names every string this suite otherwise searches for.
# ---------------------------------------------------------------------------

def test_strip_comments_removes_a_trap_comment():
    trap = (
        "const x = 1; // verified by Steward gateFor implement_tasks\n"
        "/* reviews next decided */\n"
        "{/* verified by reviews next */}\n"
        "const y = 2;"
    )
    stripped = _strip_comments(trap)
    assert "verified by" not in stripped
    assert "reviews next" not in stripped
    assert "const x = 1" in stripped and "const y = 2" in stripped


# ---------------------------------------------------------------------------
# AC-1 — the current non-gate row's reviewer pill must be guarded by a real
# gate record, not by step position (verifierPersona) alone.
# ---------------------------------------------------------------------------

def test_ac1_current_step_reviewer_pill_requires_a_real_gate_record():
    src = _strip_comments(_read(_RAIL))
    tag = _reviewer_tag(src)
    expr = _enclosing_jsx_expr(src, tag)
    assert "cur" in expr and "!isGate" in expr, (
        f"expected the reviewer-pill expression to stay scoped to the "
        f"current non-gate row: {expr!r}"
    )
    assert re.search(r"gateFor\(|\bdecided\b", expr), (
        "the reviewer-pill render condition carries nothing beyond step "
        "POSITION (cur / !isGate / verifierPersona) — it must additionally "
        "consult a REAL gate record (gateFor(...) against the resolved "
        f"`gates` array, or a `decided` flag derived from one): {expr!r}"
    )


# ---------------------------------------------------------------------------
# AC-2 — the retained forward-looking hint reads as a PREDICTION until a
# real record exists; the completed-action string is not unconditional.
# ---------------------------------------------------------------------------

def test_ac2_reviewer_pill_is_future_tense_until_a_record_exists():
    src = _strip_comments(_read(_RAIL))
    assert "reviews next" in src, (
        "the forward-looking reviewer hint on an executing step must read "
        "as a prediction ('{Role} reviews next'), never a completed-action "
        "claim, until a real gate record exists"
    )
    tag = _reviewer_tag(src)
    body = _component_function_body(src, tag)
    assert "verified by" in body, f"{tag} must still render 'verified by' once a record exists: {body!r}"
    assert "reviews next" in body, f"{tag} must render 'reviews next' before a record exists: {body!r}"
    # The two strings must be mutually exclusive branches of one decision —
    # a conditional keyword must separate them, not just two sibling spans
    # that both always render.
    lo, hi = sorted((body.index("verified by"), body.index("reviews next")))
    between = body[lo:hi]
    assert re.search(r"\?|:\s*<|&&|if\s*\(", between), (
        f"'verified by' and 'reviews next' must be mutually exclusive "
        f"branches of the same guard, not two always-rendered spans: {body!r}"
    )


# ---------------------------------------------------------------------------
# AC-3 — SdlcProgress's "awaiting review" header caption must cross-check
# the CURRENT step's own resolved type before it reaches the screen.
# ---------------------------------------------------------------------------

def test_ac3_awaiting_review_caption_requires_the_current_step_to_be_a_gate():
    src = _strip_comments(_read(_PROGRESS))
    region = _caption_state_region(src)
    assert re.search(r'\.type\s*===\s*["\']gate["\']', region) or "curStepType" in region, (
        "SdlcProgress must cross-check the CURRENT step's own resolved "
        "type (steps[curIdx].type === 'gate') as part of computing `state` "
        "/ `stateLabel` — today that computation flows `activity?.state` "
        "straight into ACTIVITY_META with no such check, so nothing stops "
        "the 'awaiting review' claim from reaching the screen on a "
        "non-gate step. (An unrelated `.type === \"gate\"` check used "
        f"elsewhere in the file, e.g. for segment styling, does not count "
        f"— it must be part of THIS computation): {region!r}"
    )
    assert 'awaiting_gate: { label: "awaiting review"' in src, (
        "the awaiting_gate key/label must still exist in ACTIVITY_META — "
        "this is a defensive cross-check on WHEN the label reaches the "
        "screen, never a removal of the honest state itself (a "
        "neighbouring suite pins the key's presence)"
    )


# ---------------------------------------------------------------------------
# AC-4 — non-regression: a genuinely completed gate's real receipt pill
# (GateRow) must render exactly as before.
# ---------------------------------------------------------------------------

def test_ac4_gate_row_real_receipts_are_unchanged():
    src = _read(_RAIL)
    assert 'isGate && gi && gi.state !== "future"' in src, (
        "GateRow's real passed/override receipt branch (StepRail.tsx:302-"
        "303) must be left rendering exactly as before"
    )
    assert 'g.override ? "override" : "passed"' in src, (
        "gateInfo's resolution of a real gate_decide record into "
        "passed/override (StepRail.tsx:217-224) must be unchanged"
    )


# ---------------------------------------------------------------------------
# AC-5 — no guard special-cases a step id; a new workflow step can't
# reintroduce the same phantom claim.
# ---------------------------------------------------------------------------

def test_ac5_no_guard_hardcodes_a_step_name():
    for path in (_RAIL, _PROGRESS):
        src = _strip_comments(_read(path))
        assert "implement_tasks" not in src, (
            f"{path.name}: a guard must not special-case the literal step "
            "id that surfaced the bug — it must key off gate-record "
            "existence or the step's own `type` field so a NEW workflow "
            "step can't reintroduce the same phantom claim"
        )
