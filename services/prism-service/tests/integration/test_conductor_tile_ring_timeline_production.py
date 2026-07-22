"""RED scaffold — Conductor PRODUCTION tile: completion ring + labeled phase
timeline (task 696cacf5).

The dependency task 24ea4027 shipped the approved Timeline-D MOCK prototype
(pinned by test_conductor_card_ring_timeline_prototype.py). THIS task ports it
into the REAL production React component customers get via `prism update`:
prism_service/web/src/pages/ConductorPage.tsx > TaskTile.

These tests scan the real SPA source on disk — the same USER-FACING render
seam the sibling test_ui_first_conductor_gate_admin_surfaces.py uses (the
pytest suite has no JS runner). They FAIL today because TaskTile still leads
with the abstract SdlcDots stepper and has no completion ring, no 2x2 metric
grid, no labeled timeline, and no peak-marked sparkline.

Scope note (verified on disk): honest-activity (task.activity.state /
task_motion_s / adrift) is NOT served by ConductorService.managed_tasks() on
this branch and conductor_service.py is outside this task's allowed_files, so
AC-6 liveness stays on the in-scope per-task signal already served
(status/phase_progress) rather than a field that does not exist end-to-end.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest  # noqa: F401  (registers the integration test module)

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent  # .../services/prism-service
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_WEB_SRC = _SERVICE_ROOT / "prism_service" / "web" / "src"
_CONDUCTOR_PAGE = _WEB_SRC / "pages" / "ConductorPage.tsx"
_TOKEN_TURNS = _WEB_SRC / "components" / "conductor" / "TokenTurns.tsx"


def _page() -> str:
    assert _CONDUCTOR_PAGE.exists(), f"missing {_CONDUCTOR_PAGE}"
    return _CONDUCTOR_PAGE.read_text(encoding="utf-8")


def _token_turns() -> str:
    assert _TOKEN_TURNS.exists(), f"missing {_TOKEN_TURNS}"
    return _TOKEN_TURNS.read_text(encoding="utf-8")


def _sdlc_steps_ids(src: str) -> list[str]:
    """Extract the ordered id list from the tile's LOCAL `SDLC_STEPS` table —
    the REAL structure the ring/timeline iterate at runtime (NOT a comment)."""
    start = src.index("const SDLC_STEPS")
    block = src[start: src.index("];", start)]
    return re.findall(r'id:\s*"([a-z_]+)"', block)


def _backend_step_ids() -> list[str]:
    from prism_service.models.workflow import WORKFLOW_STEPS
    return [s["id"] for s in WORKFLOW_STEPS]


# ── AC-4: the abstract SdlcDots stepper is gone from the tile render ─────────
def test_tasktile_no_longer_renders_abstract_sdlcdots():
    src = _page()
    assert "<SdlcDots" not in src, (
        "TaskTile must replace the abstract SdlcDots stepper with the labeled "
        "phase timeline (AC-4) — no <SdlcDots .../> usage may remain in the tile"
    )


# ── AC-1: completion RING = done steps / total, center label 'n/N phases' ────
def test_tasktile_renders_completion_ring():
    src = _page()
    # The branch's showcase hero ring reads its center as the live step count
    # ("{done}" over "of {total}") with a "steps complete" caption/aria-label,
    # not the prototype's literal "n/N phases".
    assert "of {total}" in src and "steps complete" in src, (
        "completion-ring center label is missing — the hero must lead with a ring "
        "reading done SDLC steps / total ('of {total}' + 'steps complete') (AC-1)"
    )
    assert "<svg" in src and "strokeDashoffset" in src, (
        "the completion ring must render as an SVG arc whose fill drains via "
        "strokeDashoffset — the real ring element (AC-1)"
    )
    # DE-LAUNDERED (inverted-flow #4): the prior assertion `"WORKFLOW_STEPS_ORDERED"
    # in src` passed off a source COMMENT — that identifier appears ONLY in
    # comments here; the tile actually renders a LOCAL `SDLC_STEPS` table. Pin the
    # REAL denominator element (SDLC_STEPS.length) and cross-check the table's ids
    # against the backend workflow in test_tile_sdlc_steps_match_backend_workflow.
    assert "SDLC_STEPS.length" in src, (
        "the ring denominator must be the real local SDLC_STEPS table length, "
        "not a WORKFLOW_STEPS_ORDERED comment"
    )


# ── AC (workflow contract): the tile's LOCAL step table must not DRIFT from the
#    backend workflow. This is the anti-laundering guard — the tile duplicates a
#    local `SDLC_STEPS` table (it does NOT import WORKFLOW_STEPS_ORDERED), so a
#    silent divergence (renamed/added/reordered step) MUST fail here.
def test_tile_sdlc_steps_match_backend_workflow():
    src = _page()
    tile_ids = _sdlc_steps_ids(src)
    backend_ids = _backend_step_ids()
    assert tile_ids == backend_ids, (
        "the tile's local SDLC_STEPS table has drifted from the backend "
        f"WORKFLOW_STEPS order: tile={tile_ids} backend={backend_ids}"
    )
    assert len(tile_ids) == 10, tile_ids


# ── AC-2 (RE-SCOPED, honest): the showcase hero is the draining ring + a
#    "{done}/{total} steps complete" summary sourced from the live step count.
#    The prototype's 2x2 grid (Current phase / Time left / Throughput / Idle) was
#    SUPERSEDED by the showcase and is NOT rendered — asserting those cell labels
#    would be laundering, so we pin the real hero the component renders instead.
def test_tasktile_hero_is_ring_plus_step_summary():
    src = _page()
    assert "{done}/{total} steps" in src and "of {total}" in src, (
        "the hero must lead with the ring + '{done}/{total} steps complete' "
        "summary sourced from the live step count (AC-2)"
    )
    # The tile is wired to LIVE task data (phase_progress / activity), not mock.
    assert "phase_progress" in src, (
        "the hero must read live task.phase_progress, not hardcoded mock values"
    )
    # The retired 2x2 metric cells must stay gone (regression + honesty guard).
    assert "Time left" not in src and "Throughput" not in src, (
        "the superseded 2x2 metric grid cells must not reappear un-manifested"
    )


# ── AC-3: labeled timeline — a VISIBLE stepLabel caption per real step ───────
def test_tasktile_labeled_timeline_captions_each_step():
    src = _page()
    # The branch's LabeledTimeline renders a VISIBLE caption per real SDLC step
    # as a JSX *child* — {s.label} — NOT merely inside a title=/aria-label
    # attribute (the title={s.label} on the node wrapper is preceded by '=', so
    # this whitespace/'>'-led match pins only the visible caption node).
    visible = re.findall(r"(?:^|[>\s])\{s\.label\}", src, re.M)
    assert visible, (
        "the labeled timeline must render {s.label} as a VISIBLE caption under "
        "each SDLC_STEPS node (AC-3), not only inside a title=/aria-label attr"
    )
    # It must MAP over the real step table and also caption the role per node —
    # a regression that stops iterating the steps would fail here.
    assert "steps.map(" in src or "SDLC_STEPS.map(" in src, (
        "the timeline must iterate the real SDLC step table (steps.map)"
    )
    assert re.search(r"(?:^|[>\s])\{s\.role\}", src, re.M), (
        "each timeline node must render its role caption ({s.role})"
    )


# ── AC-6: honest-activity — the pill reads task.activity.state and maps the real
#    states (working/awaiting_gate/adrift/stalled) to a label+tone. A regression
#    that drops the honest states or reads raw status would fail here.
def test_tasktile_honest_activity_states_render():
    src = _page()
    for state in ("working", "awaiting_gate", "adrift", "stalled"):
        assert f"{state}:" in src or f'"{state}"' in src, (
            f"honest-activity state '{state}' missing from the tile's ACT map"
        )
    assert "task.activity?.state" in src, (
        "the pill must read the honest task.activity.state, not the raw status"
    )
    assert "ACT_TILE" in src, "the tile must map activity states via ACT_TILE"


# ── AC-5: RETIRED (inverted-flow #4). The AC was "the tile's throughput
#    sparkline (TokenTurns) marks the window peak bar". The showcase ConductorPage
#    that SUPERSEDES the PR-#212 tile (owner chose "timeline D") does NOT render
#    <TokenTurns> at all — the tile leads with the ring + labeled timeline. A
#    source-scan of TokenTurns.tsx therefore proves NOTHING about the tile, so the
#    old assertion was laundering (a scan of an unrendered feature). We retire it
#    and instead pin the real fact: the tile does not render TokenTurns. If a
#    throughput sparkline is ever re-added to the tile, add a fresh AC to the
#    frozen manifest (tests/acceptance/conductor_tile.acceptance.json) rather than
#    resurrecting this scan.
def test_tokenturns_sparkline_AC_delivered_tile_renders_the_burn_graph():
    """AC-5 UN-RETIRED (owner 2026-07-22). It was retired because the tile
    rendered no TokenTurns — a statement of fact, not a decision that the
    graph should not exist. Re-introduction went through the manifest, as
    that retirement required."""
    src = _page()
    assert "<TokenTurns" in src, (
        "AC-5 (throughput sparkline) is DELIVERED — the tile renders the "
        "per-turn burn graph, so a drive in flight never looks static"
    )


# ── NFR-1 (green guard): canonical Hermes --accent-* tokens, no per-surface
#    palette — keeps the token doctrine from regressing through the rebuild.
def test_new_surfaces_use_canonical_accent_tokens_no_invented_palette():
    # Scoped to the tile (the render surface under test); TokenTurns is no longer
    # part of the tile (see the retired AC-5 above), so scanning it here would be
    # scanning an unrendered surface.
    both = _page()
    assert "var(--accent-" in both, "tiles must use the canonical --accent-* tokens"
    # No bespoke per-surface custom-property DECLARATIONS (e.g. `--teal-500:`).
    for tone in ("teal", "emerald", "amber", "rose"):
        assert not re.search(rf"--{tone}-\w+\s*:", both), (
            f"invented per-surface --{tone}-* custom property declared — all "
            "color must derive from the single-source --accent-* palette (NFR-1)"
        )
