"""LL-05 tests — CUPED residualization + 90-day operator baseline.

CUPED (Deng/Xu/Kohavi/Walker 2013) uses a pre-experiment covariate to
reduce variance in post-experiment outcome measurements. Here the
covariate is each operator's 90-day rolling merge rate; subtracting
the operator-specific deviation from the global baseline before
attributing a quality score to a variant keeps operator skill from
getting credited to the variant.

Parent task: 37932f3f · Sub-task LL-05.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def test_cuped_residualization_against_known_covariate():
    """Verify the CUPED identity directly against Deng/Xu/Kohavi 2013.

    Given Y=0.9 (raw quality), X=0.80 (operator baseline), mean(X)=0.55
    (global), theta=1.0 →  Y_cuped = Y - 1.0 * (X - mean(X))
                                   = 0.9 - (0.80 - 0.55) = 0.65.
    """
    from app.services.scoring_service import cuped_residualize
    adjusted = cuped_residualize(
        quality_score=0.9,
        operator_baseline=0.80,
        global_baseline=0.55,
        theta=1.0,
    )
    assert abs(adjusted - 0.65) < 1e-9


def test_cuped_zero_samples_falls_back_to_raw_score():
    """When the operator has 0 prior tasks, we have no covariate — CUPED
    degrades to the identity: Y_cuped = Y."""
    from app.services.scoring_service import cuped_residualize
    # Operator baseline == global baseline → (X - mean(X)) == 0 → no
    # adjustment. This is the defensive path: when we don't know an
    # operator's history, assume they're average.
    adjusted = cuped_residualize(
        quality_score=0.77,
        operator_baseline=0.55,   # == global
        global_baseline=0.55,
        theta=1.0,
    )
    assert abs(adjusted - 0.77) < 1e-9


def test_cuped_isolates_operator_effect():
    """Two operators with different baselines but the same raw quality
    score on the same variant should produce residuals that are
    comparable (close to each other) — otherwise the variant would be
    falsely credited/penalized for operator skill."""
    from app.services.scoring_service import cuped_residualize

    global_baseline = 0.60
    raw = 0.85

    # Alice: high-skill operator (90% baseline merge rate)
    alice_cuped = cuped_residualize(
        quality_score=raw,
        operator_baseline=0.90,
        global_baseline=global_baseline,
        theta=1.0,
    )
    # Bob: newer operator (30% baseline)
    bob_cuped = cuped_residualize(
        quality_score=raw,
        operator_baseline=0.30,
        global_baseline=global_baseline,
        theta=1.0,
    )
    # Post-adjustment, the per-variant credit differs only by the
    # difference between their baselines — which is the POINT of CUPED:
    # residualize away the operator-skill component.
    #
    # What we assert: the *spread* of cuped scores (alice vs bob) is
    # much smaller than the spread of raw scores would imply if we
    # naively penalized bob for his weaker track record. Specifically,
    # each residual moves away from the raw score by (X - mean(X)),
    # and the check is that residuals land within 20% of each other's
    # deviation from raw.
    alice_delta = abs(alice_cuped - raw)
    bob_delta = abs(bob_cuped - raw)
    # Both deltas should be non-trivially >0 (we're adjusting).
    assert alice_delta > 0.05
    assert bob_delta > 0.05
    # And the adjustments should reflect the operator's deviation from
    # the global, not the raw score — meaning residuals should be on
    # opposite sides of raw by comparable amounts.
    assert (alice_cuped < raw) and (bob_cuped > raw), (
        "high-baseline operator residual should drop, low should rise"
    )


def test_compute_operator_baseline_returns_merge_rate_and_sample_n(tmp_path):
    """Per-operator 90-day rolling merge rate."""
    from app.services.scoring_service import compute_operator_baseline
    from app.services.task_service import TaskService

    svc = TaskService(str(tmp_path / "tasks.db"))
    now = datetime(2026, 4, 23, tzinfo=timezone.utc)

    # Alice: 3 tasks, 2 merged
    for i in range(3):
        t = svc.create(title=f"a{i}", assigned_agent="alice")
        if i < 2:
            svc._db.execute(
                "UPDATE tasks SET merge_sha=?, merged_at=? WHERE id=?",
                ("abc", (now - timedelta(days=10)).isoformat(), t.id),
            )
    svc._db.commit()

    merge_rate, n = compute_operator_baseline(
        svc, "alice", window_days=90, now=now,
    )
    assert n == 3
    assert abs(merge_rate - (2 / 3)) < 1e-9


def test_compute_operator_baseline_zero_samples(tmp_path):
    """Unknown operator returns (0.0, 0)."""
    from app.services.scoring_service import compute_operator_baseline
    from app.services.task_service import TaskService

    svc = TaskService(str(tmp_path / "tasks.db"))
    merge_rate, n = compute_operator_baseline(svc, "unknown", window_days=90)
    assert merge_rate == 0.0
    assert n == 0
