"""UI contract — the Phase-metrics card on the task detail page (task bd1c2289).

The PRISM SPA has no JS test runner, so the UI-FIRST acceptance criterion is
pinned by asserting the ACTUAL web source (TSX) — the same pattern as
tests/unit/test_task_tree_ui.py and the sibling *_ui.py contracts.

These assert the real seams and ALL FAIL today (TaskDetailPage renders no
Phase-metrics card; it reads phase_progress, never phase_metrics):

  * AC-5 — TaskDetailPage merges the API's phase_metrics block onto the task and
    renders a PhaseMetricsCard: one row per visited step with a duration bar,
    duration, tokens (in/out), tok/s, and a gate-outcome pill for gate steps,
    plus a header total (wall duration, total tokens, tok/s). It reuses the
    shared stepLabel + gate chip helpers and Hermes accent tokens (no invented
    palette).
"""

from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).resolve()
_SRC = _HERE.parent.parent.parent / "prism_service" / "web" / "src"
_DETAIL = _SRC / "pages" / "TaskDetailPage.tsx"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_detail_page_reads_phase_metrics_from_api():
    src = _read(_DETAIL)
    # The detail page must consume the new top-level phase_metrics block
    # (merged onto the task, like phase_progress) and read it back.
    assert "phase_metrics" in src, \
        "TaskDetailPage must read the API's phase_metrics block"
    assert "task.phase_metrics" in src, \
        "the card must read task.phase_metrics"


def test_detail_page_renders_phase_metrics_card():
    src = _read(_DETAIL)
    assert "PhaseMetricsCard" in src, \
        "a PhaseMetricsCard component must render the per-phase breakdown"
    assert "Phase metrics" in src, "the card needs a 'Phase metrics' heading"
    # One row per step, labelled with the shared workflow-step label helper.
    assert "stepLabel" in src, "reuse the shared stepLabel helper"


def test_phase_metrics_card_shows_per_step_effort_fields():
    src = _read(_DETAIL)
    # Per-step: duration + tokens (in/out) + tok/s + a gate-outcome pill.
    assert "duration_s" in src, "each row shows its duration"
    assert "tokens_out" in src and "tokens_in" in src, \
        "each row shows tokens in/out"
    assert "tok/s" in src, "each row shows a per-step tok/s rate"
    assert "gate_outcome" in src, \
        "gate steps render a passed/failed outcome pill"


def test_phase_metrics_card_shows_task_total_and_is_hermes_themed():
    src = _read(_DETAIL)
    # Header total pulled from the API total block.
    assert "wall_duration_s" in src, "the header shows the task wall duration"
    assert "total_tokens" in src, "the header shows the task total tokens"
    # Hermes accent tokens only — no invented palette.
    assert "var(--accent-" in src, "status/gate tones must use Hermes tokens"
