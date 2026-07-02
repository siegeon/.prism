"""RED — per-phase metrics rollup for every SDLC step (task bd1c2289).

The SDLC is only partly observable: history rows carry a timestamp per
advance_task/gate_decide (per-step DURATION) and the fc08da8d telemetry
attributes per-exchange tokens to a task_id (per-step TOKENS), but nothing
ROLLS THEM UP per phase. `compute_phase_metrics` (a pure module-level helper in
conductor_service, ON TOP of the fc08da8d plumbing — it does not touch
phase_progress/burn) folds the history segments + task-attributed pi_runs into
one entry per VISITED workflow step plus a task total.

Pins, RED-first (these FAIL before compute_phase_metrics lands):

  * AC-1 — per-step DURATION = next transition ts − this step's entry ts; the
    current/last step is measured to `now`; steps come back in WORKFLOW_STEPS
    order with their entered_at.
  * AC-2 — tokens bucket into the step the task was on at each pi_runs row's ts
    (or the row's explicit `step` when present); out-of-window rows are ignored;
    tokens_in/out + turns + model aggregate per step.
  * AC-3 — gate steps carry gate_outcome (passed on approve, failed on reject);
    sparse history returns empty steps + a total without raising.
  * AC-4 — the task total reports wall_duration_s, total_tokens and tok/s.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SERVICE_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_T0 = datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc)


def _iso(sec: float) -> str:
    return (_T0 + timedelta(seconds=sec)).isoformat()


def _epoch(sec: float) -> float:
    return (_T0 + timedelta(seconds=sec)).timestamp()


def _adv(to: str, at: float, extra: str = "") -> dict:
    det = f"from=x; to={to}" + (f"; {extra}" if extra else "")
    return {"action": "advance_task", "details": det, "timestamp": _iso(at),
            "actor": "conductor"}


def _gate(step: str, action: str, at: float) -> dict:
    return {"action": "gate_decide",
            "details": f"gate={step}; action={action}; reason=r",
            "timestamp": _iso(at), "actor": "conductor"}


def _walk_history() -> list[dict]:
    # created -> review(0) -> draft(10) -> story_gate(30, pending) ->
    # approve(35) -> verify_plan(40); "now" is 100s in.
    return [
        {"action": "created", "details": "title='x'", "timestamp": _iso(0)},
        _adv("review_previous_notes", 0),
        _adv("draft_story", 10),
        _adv("story_gate", 30, "gate=pending"),
        _gate("story_gate", "approve", 35),
        _adv("verify_plan", 40),
    ]


# ── AC-1: per-step durations + order + entered_at ──────────────────────

def test_per_step_durations_and_order():
    from prism_service.services.conductor_service import compute_phase_metrics

    res = compute_phase_metrics(
        _walk_history(), [], now=_epoch(100), task_status="in_progress")
    steps = res["steps"]
    by = {s["step"]: s for s in steps}

    # Canonical WORKFLOW_STEPS order (review < draft < story_gate < verify_plan).
    assert [s["step"] for s in steps] == [
        "review_previous_notes", "draft_story", "story_gate", "verify_plan"]

    assert by["review_previous_notes"]["duration_s"] == 10.0
    assert by["draft_story"]["duration_s"] == 20.0
    assert by["story_gate"]["duration_s"] == 10.0
    # The current/last step is measured to `now` (40 -> 100 == 60).
    assert by["verify_plan"]["duration_s"] == 60.0

    # Each entry records when it was entered.
    assert by["draft_story"]["entered_at"] == _iso(10)


# ── AC-2: token bucketing by step window + explicit step ───────────────

def test_tokens_bucket_by_step_window_and_explicit_step():
    from prism_service.services.conductor_service import compute_phase_metrics

    pi_rows = [
        # In the draft_story window [10, 30) -> draft_story.
        {"ts": _epoch(15), "input_tokens": 40, "output_tokens": 100},
        # In the verify_plan window [40, now) -> verify_plan; carries a model.
        {"ts": _epoch(45), "input_tokens": 10, "output_tokens": 20,
         "model": "qwen3:0.6b"},
        # Explicit step wins over the ts (ts would fall in draft_story).
        {"ts": _epoch(15), "step": "story_gate", "input_tokens": 5,
         "output_tokens": 7},
        # Out of window (before the first advance) -> ignored entirely.
        {"ts": _epoch(-50), "input_tokens": 999, "output_tokens": 999},
    ]
    res = compute_phase_metrics(
        _walk_history(), pi_rows, now=_epoch(100), task_status="in_progress")
    by = {s["step"]: s for s in res["steps"]}

    assert by["draft_story"]["tokens_in"] == 40
    assert by["draft_story"]["tokens_out"] == 100
    assert by["draft_story"]["turns"] == 1
    assert by["verify_plan"]["tokens_in"] == 10
    assert by["verify_plan"]["tokens_out"] == 20
    assert by["verify_plan"]["model"] == "qwen3:0.6b"
    assert by["story_gate"]["tokens_out"] == 7

    # The out-of-window 999/999 row folded nowhere.
    total = res["total"]
    assert total["total_tokens"] == 40 + 100 + 10 + 20 + 5 + 7 == 182


# ── AC-4: task total (wall duration + tok/s) ───────────────────────────

def test_task_total_wall_and_tok_s():
    from prism_service.services.conductor_service import compute_phase_metrics

    pi_rows = [{"ts": _epoch(15), "input_tokens": 0, "output_tokens": 100}]
    res = compute_phase_metrics(
        _walk_history(), pi_rows, now=_epoch(100), task_status="in_progress")
    total = res["total"]
    assert total["wall_duration_s"] == 100.0
    assert total["total_tokens"] == 100
    # tok/s = total_tokens / wall_duration_s.
    assert abs(total["tok_s"] - 1.0) < 1e-6


# ── AC-3: gate outcome (approve/reject) + non-gate has none ────────────

def test_gate_outcome_passed_and_failed():
    from prism_service.services.conductor_service import compute_phase_metrics

    hist = [
        _adv("write_failing_tests", 0),
        _adv("red_gate", 10, "gate=pending"),
        _gate("red_gate", "reject", 12),
    ]
    res = compute_phase_metrics(
        hist, [], now=_epoch(20), task_status="in_progress")
    by = {s["step"]: s for s in res["steps"]}
    assert by["red_gate"]["gate_outcome"] == "failed"
    # A non-gate agent step never carries a gate outcome.
    assert by["write_failing_tests"]["gate_outcome"] in (None, "none")

    # story_gate approve -> passed (from the standard walk).
    res2 = compute_phase_metrics(
        _walk_history(), [], now=_epoch(100), task_status="in_progress")
    by2 = {s["step"]: s for s in res2["steps"]}
    assert by2["story_gate"]["gate_outcome"] == "passed"


# ── AC-3: robustness — sparse / empty history never raises ─────────────

def test_sparse_history_returns_empty_steps_without_raising():
    from prism_service.services.conductor_service import compute_phase_metrics

    only_created = [{"action": "created", "details": "title='x'",
                     "timestamp": _iso(0)}]
    res = compute_phase_metrics(
        only_created, [], now=_epoch(50), task_status="in_progress")
    assert res["steps"] == []
    assert "wall_duration_s" in res["total"]

    # Wholly empty history is still safe.
    res2 = compute_phase_metrics([], [], now=_epoch(0))
    assert res2["steps"] == []
    assert res2["total"]["total_tokens"] == 0
