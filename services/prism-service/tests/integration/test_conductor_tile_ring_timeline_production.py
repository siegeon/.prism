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
    assert "phases" in src, (
        "completion-ring center label ('n/N phases') is missing — the hero must "
        "lead with a ring reading done SDLC steps / total (AC-1)"
    )
    assert ("<svg" in src) or ("conic-gradient" in src), (
        "the completion ring must render as an SVG arc (or conic-gradient) — the "
        "tile has no ring element today (AC-1)"
    )
    # Denominator = the REAL step order, not the mock's 8 friendly names.
    assert "WORKFLOW_STEPS_ORDERED" in src


# ── AC-2: 2x2 metric grid, values sourced from live phase_progress ───────────
def test_tasktile_renders_2x2_metric_grid_from_phase_progress():
    src = _page()
    for label in ("Current phase", "Time left", "Throughput", "Idle"):
        assert label in src, f"metric-grid cell '{label}' missing from hero (AC-2)"
    # Values must be wired to LIVE phase_progress, not hardcoded/mock (misfire).
    assert "phase_progress" in src, (
        "metric grid must read task.phase_progress, not hardcoded mock values"
    )


# ── AC-3: labeled timeline — a VISIBLE stepLabel caption per real step ───────
def test_tasktile_labeled_timeline_captions_each_step():
    src = _page()
    # A stepLabel rendered as a JSX *child* (visible node caption), NOT merely
    # inside a title=/aria-label attribute the way SdlcDots does today.
    visible = re.findall(r"(?:^|[>\s])\{stepLabel\(", src, re.M)
    assert visible, (
        "the labeled timeline must render stepLabel() as a VISIBLE caption "
        "under each WORKFLOW_STEPS_ORDERED node (AC-3) — today stepLabel only "
        "appears inside title=/aria-label attributes on SdlcDots"
    )


# ── AC-5: throughput sparkline MARKS the peak bar (not just a numeric readout)
def test_tokenturns_marks_the_peak_bar():
    src = _token_turns()
    assert re.search(r"isPeak|peakIdx|peakIndex", src), (
        "TokenTurns must visually MARK which bar is the window peak (AC-5) — "
        "today it computes a scalar `peak` and only prints a numeric readout, "
        "with no per-bar peak marker"
    )


# ── NFR-1 (green guard): canonical Hermes --accent-* tokens, no per-surface
#    palette — keeps the token doctrine from regressing through the rebuild.
def test_new_surfaces_use_canonical_accent_tokens_no_invented_palette():
    both = _page() + "\n" + _token_turns()
    assert "var(--accent-" in both, "tiles must use the canonical --accent-* tokens"
    # No bespoke per-surface custom-property DECLARATIONS (e.g. `--teal-500:`).
    for tone in ("teal", "emerald", "amber", "rose"):
        assert not re.search(rf"--{tone}-\w+\s*:", both), (
            f"invented per-surface --{tone}-* custom property declared — all "
            "color must derive from the single-source --accent-* palette (NFR-1)"
        )
