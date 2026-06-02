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

def test_swimlane_rows_get_more_vertical_breathing_room():
    src = _read(_CONDUCTOR)
    # The lane row is currently cramped at py-3; with the ~2x progress bar in
    # each tile the lanes must open up. Pin a larger vertical lane rhythm.
    assert "py-3\n" not in src and "py-3\"" not in src, \
        "swimlane rows must no longer use the cramped py-3 vertical padding"
    assert ("py-4" in src or "py-5" in src), \
        "swimlane rows must adopt a roomier vertical padding (py-4/py-5)"


def test_tile_autofill_grid_widens_and_gaps_for_progress_bar():
    src = _read(_CONDUCTOR)
    # 220px tiles are too narrow once the multi-segment SDLC bar + caption sit
    # inside; the v6.3.9 two-column redesign (meta + SDLC on the left, the live
    # per-turn burn graph on the right) needs even more room, so the auto-fill
    # min widened to >=360px. Keep the inter-tile gap open.
    assert "minmax(220px" not in src, \
        "tile auto-fill min must widen past 220px for the progress bar"
    import re
    m = re.search(r"auto-fill,minmax\((\d+)px", src)
    assert m, "tile grid must use an auto-fill minmax(<n>px ...) track"
    assert int(m.group(1)) >= 360, \
        "tile auto-fill min must widen to >=360px to seat the two-column card + burn graph"
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

def test_tile_progress_section_has_an_explicit_label():
    src = _read(_CONDUCTOR)
    # Scope to the TaskTile function so the page-level "SDLC swimlanes"
    # SectionLabel can't satisfy this. The tile progress block is currently
    # just a top-bordered <div> with no rendered label, so it competes with
    # the owner/meta lines. Render an at-a-glance label as a JSX text node.
    tile = src[src.index("function TaskTile"):]
    assert (">SDLC<" in tile or ">SDLC " in tile or ">SDLC\n" in tile
            or "SDLC progress<" in tile or "progress</" in tile.lower()), \
        "the TaskTile progress section must RENDER an explicit 'SDLC'/" \
        "'progress' label (JSX text node, not just a comment) so the " \
        "information hierarchy reads at a glance"


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
