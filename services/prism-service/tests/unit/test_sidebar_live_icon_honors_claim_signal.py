"""The Sidebar's LIVE nav icon must never paint itself green for an
UNCLAIMED task — originally task 2dfa94bd (PR #1305, commit 1136c3a), which
made ConductorPage.tsx's TaskTile honest by gating drive chrome on
`claimed`, then LiveBar.tsx as the second consumer that needed the same
wiring (test_livebar_honors_claim_signal.py, since retired).

RETARGETED AGAIN (task d9f082fe follow-up, owner live, 2026-08-24): LiveBar
itself — the shell pulse CARD — is deleted outright. Owner: "remove the
live pill and make the live icon in the activity view green". The live
signal now lives on Sidebar.tsx's own "/live" nav icon (`isLive`, computed
from the same useConductorState(project) data). The original CONCERN (an
unclaimed in_progress task must never read as "being driven") still
applies to this one remaining signal, so this suite re-anchors here.

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
_SIDEBAR = _SRC / "components" / "Sidebar.tsx"
# Task 40c29b83 (FR-1): ManagedTask's declaration moved out of the old
# LiveBar.tsx into the shared hook — Sidebar and ConductorPage both consume it.
_STATE_HOOK = _SRC / "lib" / "useConductorState.ts"


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
    # ManagedTask lives in the shared useConductorState hook that Sidebar
    # and ConductorPage.tsx both consume — pinned against ITS declaration.
    src = _read(_STATE_HOOK)
    idx = src.index("type ManagedTask")
    type_block = src[idx: idx + 900]
    assert "claimed" in type_block, (
        "ManagedTask (lib/useConductorState.ts) must declare `claimed?: "
        f"boolean` so the bucketing logic can read it — not present today:\n{type_block!r}"
    )


# ===========================================================================
# Sidebar's live icon — the one place the claim signal still matters now
# that per-task chrome (rows, Lozenges, actor line) is gone entirely.
# ===========================================================================


def test_is_live_gated_on_claimed_field():
    src = _read(_SIDEBAR)
    stmt = _balanced_statement(src, "const isLive = liveManaged.some(")
    assert "claimed" in stmt, (
        "the `isLive` check backing the LIVE icon's green tint must "
        "reference m.claimed — gating on activity.state alone paints the "
        "icon green for a task the /conductor tile is simultaneously "
        f"showing NOT CLAIMED for:\n{stmt}"
    )


def test_is_live_not_gated_on_workflow_step_or_intake_literal():
    src = _read(_SIDEBAR)
    stmt = _balanced_statement(src, "const isLive = liveManaged.some(")
    assert "intake" not in stmt, (
        "must not key the claim gate off the synthesized 'intake' literal "
        f"— would falsely paint the icon green for genuinely-unclaimed "
        f"intake-step tasks too:\n{stmt}"
    )
    assert "workflow_step" not in stmt, (
        f"must not gate off workflow_step name/position either:\n{stmt}"
    )


def test_live_icon_tint_is_conditional_on_the_live_indicator_item_only():
    # The green tint must be scoped to the "/live" nav item specifically —
    # every other icon (Dashboard, Work, Conductor, ...) must stay unlit
    # even while isLive is true, or the whole sidebar would read as "on".
    src = _read(_SIDEBAR)
    assert "isLiveIndicator?: boolean;" in src, (
        "Sidebar's Item type must declare a one-off isLiveIndicator flag, "
        "same precedent as the existing isNew flag"
    )
    assert 'icon: Radio, isLiveIndicator: true' in src, (
        "only the '/live' nav item may opt into the green tint"
    )
    assert "const isLiveNow = isLiveIndicator && isLive;" in src


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
