"""LiveBar.tsx is the SECOND consumer of the claim signal shipped by
task 2dfa94bd (PR #1305, commit 1136c3a) — that fix made ConductorPage.tsx's
TaskTile honest by gating drive chrome on `claimed`, and explicitly flagged
LiveBar.tsx in its own blast radius as a second consumer that still needed
wiring. It never was.

BUG: LiveBar.tsx's `ManagedTask` type (lines 31-39) carries no `claimed`
field, and the `working` bucket (lines 200-201) is computed purely from
`m.activity?.state === "working" || m.activity?.state === "driving"`.
Because `managed_tasks()` synthesizes workflow_step="intake" and a
working/driving activity state for ANY in_progress task with an empty
workflow_step/gate_state (mx-ccba40), an unclaimed in_progress task lands in
`working` and renders full DRIVING chrome at lines 280-304: the step
Lozenge, the ACTIVITY_META "working"/"driving" Lozenge, and the
`m.assigned_agent || "claude-code"` actor line — directly contradicting the
/conductor TaskTile's honest "NOT CLAIMED" for the same task on the same
screen.

FIX CONTRACT pinned by this suite (LiveBar.tsx only, non-policy):
  - `ManagedTask` gains `claimed?: boolean`.
  - The `working` bucket filter must key on `m.claimed` (additively, next to
    the existing activity.state check) — NOT on workflow_step name, the
    synthesized "intake" literal, or step position (stop_if #2).
  - The base `roots` list must NOT itself filter on `claimed` — an unclaimed
    task must stay visible (stop_if #1: never a silent vanish). Once
    `working` excludes it, the task falls through to the existing `inflow`
    bucket automatically (workflow_step is non-empty, and it is no longer in
    `working`/`gated`), which already renders an honest, non-drive-claiming
    row (muted activity label via ACTIVITY_META, no step-driving Lozenge, no
    actor line) per mx-ccba40's own precedent — no new JSX branch required,
    only the upstream filter needs to change.

Convention (no JS test runner in this repo, tests/unit/test_conductor_page_
animated_cleanup_ui.py:4-6 and the sibling test_conductor_tile_requires_
claim.py): pytest parses the ACTUAL TSX source directly. Statements are
extracted with a paren-balancing helper (never a fixed character window or
a match against an explanatory comment), matching the precedent this task's
sibling frontend tests set for ConductorPage.tsx.

ALL of these FAIL against current source: LiveBar.tsx never declares
`claimed` on ManagedTask, and the `working` filter never references it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SERVICE_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_SRC = _SERVICE_ROOT / "prism_service" / "web" / "src"
_LIVEBAR = _SRC / "components" / "LiveBar.tsx"
# Task 40c29b83 (FR-1): ManagedTask's declaration moved out of LiveBar.tsx
# into the shared hook LiveBar and ConductorPage now both consume.
_LIVEBAR_STATE_HOOK = _SRC / "lib" / "useConductorState.ts"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _balanced_statement(src: str, marker: str) -> str:
    """Slice src[marker ... matching-close-paren ... next ';'] by counting
    parens from the FIRST '(' after marker — robust to optional-chaining
    (`?.`) and nested calls inside the statement, unlike a fixed-width
    window or a bare substring search."""
    start = src.index(marker)
    paren_start = src.index("(", start)
    depth = 0
    i = paren_start
    for i in range(paren_start, len(src)):
        c = src[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                break
    else:
        raise AssertionError(f"unbalanced parens scanning from {marker!r}")
    end = src.index(";", i)
    return src[start:end + 1]


# ===========================================================================
# ManagedTask must actually be able to carry the claim signal in this file.
# ===========================================================================


def test_managed_task_type_carries_claimed_field():
    # SUPERSEDED location (task 40c29b83, FR-1): ManagedTask no longer lives
    # as a private type in LiveBar.tsx -- it moved to the shared
    # useConductorState hook that ConductorPage.tsx now also consumes, so the
    # real invariant (the bucketing logic can read m.claimed) is pinned
    # against ITS type declaration rather than a copy LiveBar no longer owns.
    src = _read(_LIVEBAR_STATE_HOOK)
    idx = src.index("type ManagedTask")
    type_block = src[idx: idx + 900]
    assert "claimed" in type_block, (
        "ManagedTask (lib/useConductorState.ts) must declare `claimed?: "
        f"boolean` so the bucketing logic can read it — not present today:\n{type_block!r}"
    )


# ===========================================================================
# The `working` bucket is the actual bug site: it must key on `claimed`,
# never on workflow_step name / the synthesized "intake" literal / position.
# ===========================================================================


def test_working_bucket_gated_on_claimed_field():
    src = _read(_LIVEBAR)
    stmt = _balanced_statement(src, "const working = roots.filter(")
    assert "claimed" in stmt, (
        "the `working` bucket filter must reference m.claimed — gating on "
        "activity.state alone renders full DRIVING chrome (step Lozenge, "
        "ACTIVITY_META Lozenge, assigned_agent actor line) for a task the "
        f"/conductor tile is simultaneously showing NOT CLAIMED for:\n{stmt}"
    )


def test_working_bucket_not_gated_on_workflow_step_or_intake_literal():
    src = _read(_LIVEBAR)
    stmt = _balanced_statement(src, "const working = roots.filter(")
    assert "intake" not in stmt, (
        "must not key the claim gate off the synthesized 'intake' literal "
        f"(stop_if #2 — would strip chrome from genuinely-claimed intake-"
        f"step tasks too):\n{stmt}"
    )
    assert "workflow_step" not in stmt, (
        f"must not gate off workflow_step name/position either:\n{stmt}"
    )


# ===========================================================================
# Visibility: an unclaimed task must never be hidden outright (stop_if #1).
# The base `roots` projection (feeding every bucket) must stay claim-blind.
# ===========================================================================


def test_roots_never_filters_out_unclaimed_tasks():
    src = _read(_LIVEBAR)
    stmt = _balanced_statement(src, "const roots = managed.filter(")
    assert "claimed" not in stmt, (
        "the base `roots` list must not drop unclaimed tasks — they must "
        "stay visible in an honest alternate row (e.g. the existing inflow "
        f"bucket), never disappear from the strip entirely:\n{stmt}"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
