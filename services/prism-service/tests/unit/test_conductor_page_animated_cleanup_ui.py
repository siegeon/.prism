"""UI contract tests for "Clean up /conductor page to support the new
animated SDLC tasks" (task fde3c08e).

The PRISM SPA has NO JS test runner, so UI-FIRST acceptance criteria are
pinned by asserting the ACTUAL web source (TSX) — the same pattern as
tests/unit/test_animate_conductor_tasks_ui.py and test_explore_narrative_ui.py.

These assert the REAL seams the cleanup must land on the LIVE components:
the swimlane lane/tile spacing actually opens up now that each tile embeds
the ~2x-height animated SdlcProgress bar; the TaskTile information hierarchy
gains an explicit progress-section label so it reads at a glance; gate-reason
receipt text becomes click-to-expand (progressive disclosure) instead of a
tooltip-only / inline wall; the current-step fill tweens SMOOTHLY between the
5s polls via a spring (not a hard jump) AND that tween is suppressed under
reduced-motion; and the version is patch-bumped.

ALL of these FAIL against the current source (lanes are py-3, the tile grid
is minmax(220px) gap-2, the progress section has no label, gate_reason lives
only in the button `title` tooltip, SdlcProgress hard-sets segFill with no
spring, and PRISM_VERSION is 6.3.0). They go green only when the cleanup is
wired into ConductorPage.tsx + SdlcProgress.tsx.
"""

from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).resolve()
_SRC = _HERE.parent.parent.parent / "prism_service" / "web" / "src"
_CONDUCTOR = _SRC / "pages" / "ConductorPage.tsx"
_PROGRESS = _SRC / "components" / "conductor" / "SdlcProgress.tsx"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Lane / tile spacing opens up for the embedded animated progress bar
# ---------------------------------------------------------------------------

def test_conductor_uses_flat_tile_list_not_swimlanes():
    src = _read(_CONDUCTOR)
    # SUPERSEDED by the v6.3.19 redesign: the 9 literal SDLC swimlanes were
    # dropped for a flat tile list (one tile per managed task). The lane
    # two-column grid (grid-cols-[14rem_1fr]) and the per-step lane iteration
    # must be gone; managed tasks render through a single auto-fill tile grid.
    assert "grid-cols-[14rem_1fr]" not in src, \
        "the literal swimlane lane grid must be gone (flat tile list now)"
    assert "auto-fill,minmax(" in src, \
        "managed tasks must render through the flat auto-fill tile grid"
    assert "managed.map(" in src, \
        "the flat tile list must map over managed tasks directly (no per-lane grouping)"


def test_tile_autofill_grid_widens_and_gaps_for_progress_bar():
    src = _read(_CONDUCTOR)
    # 220px tiles are too narrow once the ring hero + labeled SDLC timeline sit
    # inside; the branch's showcase grid uses a container-relative "up-to-4-per-
    # row, min 280px" track — minmax(max(280px,calc((100%-3rem)/4)),1fr) — so it
    # fills to 4 across on a wide board and drops to 3/2/1 as width shrinks.
    assert "minmax(220px" not in src, \
        "tile auto-fill min must widen past 220px for the progress bar"
    import re
    # The floor is what this AC is really about: a tile has to be wide enough
    # to seat the ring hero, the LABELED 10-step timeline and the burn graph.
    # It used to pin the exact 4-across expression
    # minmax(max(280px,calc((100%-3rem)/4)),1fr), which made the literal track
    # the contract instead of the legibility it exists to protect — so widening
    # the tiles (owner 2026-07-22: "wide screen please, not portrait") failed a
    # test whose own reasoning asks for exactly that. Pin the floor, not the
    # arithmetic.
    m = re.search(r"auto-fill,minmax\((?:max|min)\([^)]*?(\d+)px", src)
    assert m, \
        "tile grid must use a container-relative auto-fill minmax(...) track " \
        "with an explicit px floor"
    assert int(m.group(1)) >= 280, \
        "tile auto-fill floor must be >=280px to seat the ring hero + labeled timeline"
    # The tile grid gap was gap-2; open it so tiles don't feel cluttered.
    assert "minmax(" in src
    grid_line = next(ln for ln in src.splitlines() if "auto-fill,minmax(" in ln)
    assert "gap-2" not in grid_line, \
        "the tile auto-fill grid must open past gap-2"
    assert "gap-3" in grid_line or "gap-4" in grid_line, \
        "the tile auto-fill grid must use gap-3/gap-4"


# ---------------------------------------------------------------------------
# TaskTile information hierarchy: the SDLC progress block reads as its own
# labelled section, not an unmarked strip competing with the meta lines.
# ---------------------------------------------------------------------------

def test_tile_renders_animated_stepper_and_phase_label():
    src = _read(_CONDUCTOR)
    # RE-POINTED by v6.8.18 (task 696cacf5): the abstract SdlcDots dot stepper
    # is REPLACED by the LabeledTimeline (a node per real WORKFLOW_STEPS_ORDERED
    # step with a visible stepLabel caption) — AC-4 of the production ring+
    # timeline tile. The at-a-glance progress invariant this test guards is now
    # owned by <LabeledTimeline/>; the current-phase label (top-right) is
    # unchanged. Scope to the TaskTile function (the LabeledTimeline definition
    # also lives later in the file, but the JSX usage is in the tile).
    tile = src[src.index("function TaskTile"):]
    assert "<LabeledTimeline" in tile, \
        "the TaskTile must render the labeled phase timeline (<LabeledTimeline/>)"
    # RE-POINTED for the showcase tile: the current phase is surfaced honestly
    # via the curStep.label handoff line ("ready -> <worker> on deck for <phase>")
    # and the honest-activity {actLabel} pill (top-right), not the absent
    # stepChipClass(stepId)/{phaseLabel} from the prior design.
    assert "curStep.label" in tile and "{actLabel}" in tile, \
        "the TaskTile must surface the current phase legibly — the curStep.label " \
        "handoff line and the {actLabel} activity pill (top-right)"


# ---------------------------------------------------------------------------
# Progressive disclosure: gate-reason receipt text becomes click-to-expand,
# not a tooltip-only string / inline wall.
# ---------------------------------------------------------------------------

def test_gate_reason_is_progressive_disclosure_not_tooltip_only():
    src = _read(_CONDUCTOR)
    # Today gate_reason rides only inside the button `title` tooltip string.
    # Surface it as a real click-to-expand disclosure in the tile.
    assert "gate_reason" in src, "the tile must read task.gate_reason"
    # The TaskTile function (today) has no local expand state and no
    # <details> element — gate_reason only rides in the button `title`
    # tooltip. Require a genuine in-tile click-to-expand disclosure.
    assert ("<details" in src or "<summary" in src or "setExpanded" in src
            or "showReason" in src or "setShowReason" in src), \
        "gate_reason detail must be a click-to-expand disclosure (details/" \
        "summary or a local expand state in the tile), not tooltip-only"


# ---------------------------------------------------------------------------
# Current-step fill tweens SMOOTHLY between the 5s polls (spring), and the
# tween is suppressed under reduced-motion.
# ---------------------------------------------------------------------------

def test_segment_fill_tweens_smoothly_via_spring():
    src = _read(_PROGRESS)
    # The current-step fill must ease between poll snapshots instead of hard
    # jumping; pin a motion spring/tween on the segment fill.
    assert "useSpring" in src, \
        "the current-step fill must tween via useSpring between 5s polls"


def test_segment_tween_is_suppressed_under_reduced_motion():
    src = _read(_PROGRESS)
    # The spring/tween that drives the segment fill must collapse when the
    # caller threads reduced=true (reduced-motion honored end-to-end).
    assert "useSpring" in src
    spring_idx = src.index("useSpring")
    window = src[spring_idx: spring_idx + 240]
    assert "reduced" in window, \
        "the segment-fill spring must reference `reduced` so the tween is " \
        "suppressed under prefers-reduced-motion"


# ---------------------------------------------------------------------------
# Reduced-motion stays threaded page -> tile -> SdlcProgress (regression guard)
# ---------------------------------------------------------------------------

def test_reduced_motion_stays_threaded_end_to_end():
    src = _read(_CONDUCTOR)
    assert "useReducedMotion" in src, "page must read useReducedMotion()"
    assert "reduced={reduced}" in src, \
        "reduced must stay threaded page -> TaskTile -> SdlcProgress"


# ---------------------------------------------------------------------------
# Version patch-bump — the /conductor cleanup landed in 6.3.1; the live-token
# fix consolidated onto the same branch as 6.3.2. Assert a >= 6.3.1 floor so
# the bump is verified without re-breaking on every subsequent patch.
# ---------------------------------------------------------------------------

def test_prism_version_patch_bumped_from_6_3_0():
    from prism_service.__version__ import PRISM_VERSION
    parts = tuple(int(x) for x in PRISM_VERSION.split("."))
    assert parts >= (6, 3, 1), \
        f"PRISM_VERSION must be >= 6.3.1 for the /conductor cleanup; got {PRISM_VERSION}"
