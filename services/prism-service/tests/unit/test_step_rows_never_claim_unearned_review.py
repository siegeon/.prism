"""A currently-EXECUTING step row must never claim review it has not had.

Task 122ff356. The PRISM SPA has no JS test runner, so these ACs are pinned
by asserting the ACTUAL web source (same pattern as
test_conductor_card_tells_the_truth.py / test_conductor_page_animated_cleanup_ui.py).

Bug: StepRail.tsx's CURRENT non-gate step row renders `verified by {Role}`
(the VerifiedBy pill, StepRail.tsx ~319/425-440) purely off step POSITION —
`verifierPersona` is "the persona of the next gate step ahead", found by
`steps.slice(curIdx+1).find(s=>s.type==="gate")?.persona` — with NO check
against the resolved `gates: GanttGate[]` array (the real gate_decide
receipts, looked up via `gateFor(id)` at StepRail.tsx:217). So a step that is
still mid-execution, with its own gate genuinely pending/future, renders text
that reads as if a reviewer already signed off.

Fix must key off actual record existence (gateFor/the gates array), never off
a step id/name, so a newly-added workflow step doesn't reintroduce the same
phantom claim; and it must leave the GateRow real passed/override receipt
path (StepRail.tsx:302-303) completely unchanged.
"""

from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).resolve()
_SRC = _HERE.parent.parent.parent / "prism_service" / "web" / "src"
_RAIL = _SRC / "components" / "conductor" / "StepRail.tsx"
_PROGRESS = _SRC / "components" / "conductor" / "SdlcProgress.tsx"
_CONDUCTOR_PAGE = _SRC / "pages" / "ConductorPage.tsx"


def _read(p: Path) -> str:
    assert p.exists(), f"missing source file: {p}"
    return p.read_text(encoding="utf-8")


def _verified_by_render_line(src: str) -> str:
    """The exact JSX line that renders the VerifiedBy pill on the CURRENT
    non-gate step row. Matched on the rendered tag `<VerifiedBy`, never a
    bare name or a comment — a comment above the element has satisfied this
    kind of assertion before."""
    for line in src.splitlines():
        if "<VerifiedBy persona=" in line:
            return line
    raise AssertionError(
        "StepRail.tsx no longer renders a <VerifiedBy ...> element at all — "
        "if the pill was removed outright this test's premise changed, "
        "update it deliberately rather than letting it silently pass"
    )


# ---------------------------------------------------------------------------
# The core defect: the pill must require a REAL gate_decide/verification
# record for the upcoming gate, not merely "a gate exists later in the list".
# ---------------------------------------------------------------------------

def test_verified_by_pill_requires_a_real_gate_record():
    src = _read(_RAIL)
    line = _verified_by_render_line(src)
    # `verifierPersona` alone only proves a later gate STEP exists in
    # WORKFLOW_STEPS_ORDERED — it says nothing about whether that gate has
    # actually been decided. The guard must additionally consult the
    # resolved `gates` array (a real GanttGate receipt), via the same
    # `gateFor` lookup GateRow itself uses for genuine passed/override pills.
    assert "gateFor(" in line, (
        "the CURRENT non-gate step's 'verified by' pill renders off step "
        "POSITION only (verifierPersona) with no check that a real "
        "gate_decide/verification record exists for the upcoming gate — "
        f"this claims review before it happened. Offending line: {line!r}"
    )
    # Still scoped to the currently-executing, non-gate row — the fix must
    # not widen it to fire on rows it was never meant to touch.
    assert "cur" in line and "!isGate" in line, (
        f"the guard must stay scoped to the current non-gate row: {line!r}"
    )


def test_verified_by_guard_keys_off_record_existence_not_a_step_name():
    src = _read(_RAIL)
    line = _verified_by_render_line(src)
    # stop_if: never special-case a literal step id (e.g. "implement_tasks")
    # — the guard must generalize to any newly-added workflow step.
    assert '"implement_tasks"' not in src and "'implement_tasks'" not in src, (
        "the fix must not hardcode the specific step name that surfaced the "
        "bug; it must key off actual gate-record existence so a NEW step "
        "can't reintroduce the same phantom claim"
    )
    assert "implement_tasks" not in line, (
        f"the VerifiedBy guard line itself must not name a specific step: {line!r}"
    )


# ---------------------------------------------------------------------------
# Non-regression: a genuinely completed/verified earlier step (a real
# gate_decide receipt) must keep rendering its real pill unchanged.
# ---------------------------------------------------------------------------

def test_gate_row_real_receipts_still_render_unchanged():
    src = _read(_RAIL)
    assert 'isGate && gi && gi.state !== "future"' in src, (
        "GateRow's real passed/override receipt path (the legitimate "
        "'verified'/'passed' pill for a genuinely completed gate, "
        "StepRail.tsx:302-303) must be left rendering exactly as before — "
        "the fix must only touch the CURRENT non-gate row's forward-looking "
        "pill, never suppress real evidence on completed gates"
    )


# ---------------------------------------------------------------------------
# The header/caption "awaiting review" wording: already keyed strictly off
# the server's honest `activity.state` (conductor_service.activity_for only
# ever sets state=awaiting_gate on a real pending/failed gate step). Pinned
# here so this invariant can't silently drift while fixing the pill above.
# ---------------------------------------------------------------------------

def test_awaiting_review_wording_is_keyed_only_off_server_activity_state():
    for path in (_PROGRESS, _CONDUCTOR_PAGE):
        src = _read(path)
        assert 'awaiting_gate: { label: "awaiting review"' in src, (
            f"{path.name}: expected the honest ACTIVITY_META/ACT_TILE map to "
            "carry the 'awaiting review' label under the awaiting_gate key "
            "(server-driven), not as a free-standing string"
        )
        # Exactly one occurrence of the literal label per file: no second,
        # locally-computed "awaiting review" string driven by workflow_step
        # or any other client-side signal.
        assert src.count('"awaiting review"') == 1, (
            f"{path.name}: 'awaiting review' must appear exactly once, as "
            "the awaiting_gate entry in the honest activity-state map — a "
            "second occurrence would mean some OTHER, non-gate-backed path "
            "can print the same claim"
        )
