"""LiveBar.tsx must never paint its live/driving indicator green for an
UNCLAIMED task — originally task 2dfa94bd (PR #1305, commit 1136c3a), which
made ConductorPage.tsx's TaskTile honest by gating drive chrome on
`claimed` and flagged LiveBar.tsx as a second consumer that still needed
wiring.

SUPERSEDED SHAPE (task d9f082fe follow-up, owner live, 2026-08-24): the
original bug site was the `working` bucket (`roots.filter(...)`) that fed a
per-task row list — LiveBar rendered full DRIVING chrome (step Lozenge,
ACTIVITY_META Lozenge, `assigned_agent` actor line) for any task landing in
that bucket. LiveBar no longer renders ANY per-task chrome at all — it was
simplified to a single live/idle status dot (see LiveBar.tsx's own module
docstring: "that live panel is odd to me... I should not need the queue
metadata on that panel any longer it shows up elsewhere"). The original
CONCERN (an unclaimed in_progress task must never read as "being driven" on
this surface) still applies to the one signal LiveBar has left — the dot's
color — so this suite re-anchors there instead of to the retired
`working`/`roots` buckets.

Convention (no JS test runner in this repo, tests/unit/test_conductor_page_
animated_cleanup_ui.py:4-6): pytest parses the ACTUAL TSX source directly.
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
    # ManagedTask lives in the shared useConductorState hook that LiveBar
    # and ConductorPage.tsx both consume — pinned against ITS declaration.
    src = _read(_LIVEBAR_STATE_HOOK)
    idx = src.index("type ManagedTask")
    type_block = src[idx: idx + 900]
    assert "claimed" in type_block, (
        "ManagedTask (lib/useConductorState.ts) must declare `claimed?: "
        f"boolean` so the bucketing logic can read it — not present today:\n{type_block!r}"
    )


# ===========================================================================
# LiveBar's live/idle dot — the one place the claim signal still matters
# now that per-task chrome (rows, Lozenges, actor line) is gone entirely.
# ===========================================================================


def test_is_live_gated_on_claimed_field():
    src = _read(_LIVEBAR)
    stmt = _balanced_statement(src, "const isLive = managed.some(")
    assert "claimed" in stmt, (
        "the `isLive` check backing the status dot must reference m.claimed "
        "— gating on activity.state alone paints the dot green (\"Live\") "
        "for a task the /conductor tile is simultaneously showing NOT "
        f"CLAIMED for:\n{stmt}"
    )


def test_is_live_not_gated_on_workflow_step_or_intake_literal():
    src = _read(_LIVEBAR)
    stmt = _balanced_statement(src, "const isLive = managed.some(")
    assert "intake" not in stmt, (
        "must not key the claim gate off the synthesized 'intake' literal "
        f"— would falsely paint the dot green for genuinely-unclaimed "
        f"intake-step tasks too:\n{stmt}"
    )
    assert "workflow_step" not in stmt, (
        f"must not gate off workflow_step name/position either:\n{stmt}"
    )


def test_livebar_renders_no_per_task_chrome():
    # The strongest form of the original invariant: with no row list at
    # all, an unclaimed task cannot possibly render DRIVING chrome, because
    # nothing task-specific renders anymore.
    src = _read(_LIVEBAR)
    assert "assigned_agent" not in src, (
        "LiveBar must not render a per-task actor line — that chrome moved "
        "to TasksPage.tsx/ConductorPage.tsx, which already read this same "
        "shared state"
    )
    assert ".map(" not in src, (
        "LiveBar must not render a per-task row list at all — simplified to "
        "a single live/idle status dot"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
