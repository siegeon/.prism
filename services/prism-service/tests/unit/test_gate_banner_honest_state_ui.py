"""UI contract tests for "Gate banner stops claiming evidence it lacks"
(task 72d3e0d1-dc43-4beb-a14a-e3336631c6da).

Owner hit this 2026-07-29 on a live task and reacted with "damnnit, how are
we at a green wait state": the gate card's headline said "READY - evidence
passing" while GET /api/conductor/gate/readiness had returned
{receipt_ok: true, manual_review: true, receipt: {adapter: "human",
passed: true, status: "your_review", reason: "visual/demo gate - your
review is the sign-off; Approve to release"}}. receipt_ok=true there means
only "a human review is the accepted evidence route for this proof_type" -
NOT "something passed". Nothing had passed; the human had not reviewed yet,
which is the entire reason the gate was parked. That sentence is exactly
what makes a reviewer click Approve without opening the artifacts - the
false green the honest-gate work exists to prevent.

The PRISM SPA has NO JS test runner, so UI acceptance criteria are pinned by
asserting the ACTUAL TSX source, the same pattern as
test_conductor_page_animated_cleanup_ui.py. Per that convention (and the
repeat failures it documents), these tests parse the BALANCED {...} JSX
EXPRESSION that follows a literal marker - never a fixed character window -
so a stray comment or an unrelated string elsewhere in the file can't
satisfy an assertion the way a comment above an element has before.

ALL of these FAIL against the current source: TaskDetailPage.tsx's
notification-banner headline keys ONLY on gateVerdict (receipt_ok), so it
renders "READY - evidence passing" for a bare human-entitlement receipt
exactly as it does for a real fresh machine receipt; its border/background
styling is two-way (sage/rose), with no calm "awaiting review" tone; and
PlanView.tsx's "evidence check - live" panel unconditionally renders
"FRESH and PASSING" whenever receipt_ok is true. They go green only once
both files key on `manual_review` + `receipt.adapter === "human"` (the
signal the backend already sends) to render an honest "AWAITING YOUR
REVIEW" state instead of "passing".
"""

from __future__ import annotations

import re
from pathlib import Path

_HERE = Path(__file__).resolve()
_SRC = _HERE.parent.parent.parent / "prism_service" / "web" / "src"
_TASK_DETAIL = _SRC / "pages" / "TaskDetailPage.tsx"
_PLAN_VIEW = _SRC / "components" / "plan" / "PlanView.tsx"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _extract_braced_expr(src: str, marker: str) -> str:
    """Return the JSX `{...}` expression that starts at the first `{` after
    `marker`, matched by BRACE DEPTH (not a fixed slice) so the assertion
    can't be satisfied by a comment or an unrelated literal near the marker
    instead of the real guard that decides what renders."""
    idx = src.index(marker)
    start = src.index("{", idx)
    depth = 0
    for i in range(start, len(src)):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    raise AssertionError(f"unbalanced braces scanning forward from marker {marker!r}")


# ---------------------------------------------------------------------------
# AC-1 / AC-2 - TaskDetailPage.tsx notification-banner HEADLINE
# ---------------------------------------------------------------------------

def test_headline_expression_distinguishes_bare_entitlement_from_a_real_pass():
    src = _read(_TASK_DETAIL)
    expr = _extract_braced_expr(src, "● {")

    # AC-1: the headline guard must key on the bare-entitlement signal, not
    # merely on gateVerdict/receipt_ok (which is also true for a real pass).
    assert "isAwaitingReview" in expr, (
        "the headline expression must branch on an awaiting-review "
        "condition, not on gateVerdict alone"
    )
    assert "AWAITING YOUR REVIEW" in expr, (
        "a bare human-entitlement receipt must render an honest "
        "'AWAITING YOUR REVIEW' headline"
    )

    # AC-1 (continued): the honest headline must NEVER contain the word
    # "passing" for the awaiting-review branch. Slice the ternary around
    # isAwaitingReview and check ONLY the branch that fires when it is true
    # (ordering-agnostic: works whether the true-branch is written before or
    # after the ':' relative to the "AWAITING YOUR REVIEW" literal).
    awaiting_idx = expr.index("AWAITING YOUR REVIEW")
    # The literal awaiting-review string itself must not smuggle "passing"
    # into the same sentence.
    line_start = expr.rfind("\n", 0, awaiting_idx) + 1
    line_end = expr.index("\n", awaiting_idx) if "\n" in expr[awaiting_idx:] else len(expr)
    awaiting_line = expr[line_start:line_end]
    assert "passing" not in awaiting_line.lower(), (
        "the awaiting-review headline text must not contain the word "
        "'passing' - nothing has passed"
    )

    # AC-2: the fix must not delete the affirmative state - a real fresh
    # receipt (or epic roll-up) must still read as evidence passing.
    assert "READY · evidence passing" in expr, (
        "the fix must preserve the affirmative headline for a real fresh "
        "passing receipt (or epic roll-up) - deleting it is the misfire, "
        "not the fix"
    )


def test_is_awaiting_review_keys_on_manual_review_and_human_adapter():
    src = _read(_TASK_DETAIL)
    m = re.search(r"const\s+isAwaitingReview\s*=([\s\S]*?);\n", src)
    assert m, (
        "TaskDetailPage.tsx must define an isAwaitingReview const that the "
        "headline (and banner tone) key on"
    )
    definition = m.group(1)
    assert "manual_review" in definition, (
        "isAwaitingReview must read the backend's manual_review flag - the "
        "signal that distinguishes 'a human may decide' from 'something "
        "passed'"
    )
    assert '"human"' in definition, (
        "isAwaitingReview must check receipt.adapter === \"human\" so an "
        "epic roll-up (adapter='epic-rollup', real child evidence) is NOT "
        "mistaken for bare entitlement"
    )


# ---------------------------------------------------------------------------
# AC-3 - banner tone is a calm three-way state, not a two-way sage/rose
# ---------------------------------------------------------------------------

def test_banner_tone_is_a_three_way_state_not_alarming_and_not_a_silent_pass():
    src = _read(_TASK_DETAIL)
    m = re.search(r"const\s+bannerTone[^=]*=([\s\S]*?);\n", src)
    assert m, (
        "TaskDetailPage.tsx must define a bannerTone (or equivalently-"
        "named) const so the awaiting-review state gets its own color, "
        "distinct from both blocked (rose) and a real pass (sage)"
    )
    definition = m.group(1)
    assert '"amber"' in definition, (
        "the awaiting-review tone must be amber - calm, not alarming "
        "(never rose/failed-looking) and not indistinguishable from a "
        "real pass (never sage)"
    )
    assert '"sage"' in definition, "a real pass must keep its sage tone"
    assert '"rose"' in definition, "a blocked gate must keep its rose tone"
    assert "isAwaitingReview" in definition, (
        "the tone must be keyed on isAwaitingReview, not just gateVerdict"
    )


# ---------------------------------------------------------------------------
# AC-4 - PlanView.tsx "evidence check · live" panel gets the same fix
# ---------------------------------------------------------------------------

def test_planview_evidence_panel_does_not_claim_passing_for_bare_entitlement():
    src = _read(_PLAN_VIEW)
    expr = _extract_braced_expr(src, "evidence check · live")

    assert "manual_review" in expr, (
        "PlanView.tsx's evidence-check panel must read manual_review, the "
        "same signal TaskDetailPage.tsx uses, so the two cards can't "
        "disagree about the same live readiness response"
    )
    assert '"human"' in expr, (
        "PlanView.tsx must check receipt.adapter === \"human\" so an epic "
        "roll-up's real aggregated evidence isn't demoted to "
        "awaiting-review wording"
    )

    # The pre-existing affirmative literal must still be present and must
    # still be reachable for a real passing receipt (fix must not delete it).
    assert "FRESH and PASSING" in expr, (
        "the fix must preserve 'FRESH and PASSING' for a real fresh "
        "machine receipt - deleting the affirmative state is the misfire"
    )

    # And the awaiting-review branch's own text (bounded so a passing
    # branch elsewhere in the expression can't satisfy this negatively)
    # must not itself claim PASSING.
    assert "Awaiting your review" in expr or "AWAITING YOUR REVIEW" in expr.upper(), (
        "PlanView.tsx must render an honest awaiting-review message, not "
        "just suppress the passing claim silently"
    )
    awaiting_marker = "Awaiting your review" if "Awaiting your review" in expr else None
    if awaiting_marker:
        aw_idx = expr.index(awaiting_marker)
        # Bound the awaiting-review sentence to the next JSX tag boundary
        # or 200 chars, whichever is smaller, so this does not silently
        # widen into the unrelated passing branch's text.
        window_end = min(aw_idx + 200, len(expr))
        tag_idx = expr.find("</", aw_idx)
        if tag_idx != -1:
            window_end = min(window_end, tag_idx)
        awaiting_sentence = expr[aw_idx:window_end]
        assert "PASSING" not in awaiting_sentence.upper(), (
            "the awaiting-review sentence in PlanView.tsx must not itself "
            "contain 'passing'"
        )
