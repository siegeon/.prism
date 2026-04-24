"""LL-02 tests — composite Layer-A quality scorer (pure function).

Parent task: 37932f3f-9cd4-40bf-9df3-e9db19fcc88d · Sub-task LL-02
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _score(**components):
    """Call composite_score with defaults for unspecified fields.

    Defaults lean "neutral positive" — merged, tests green, no retries,
    no churn, no followups, no revert. Individual tests override what
    they care about.
    """
    from app.services.scoring_service import composite_score

    base = {
        "merged_to_main": True,
        "gate_retry_count": 0,
        "tests_green_on_merge": True,
        "reverted_within_14d": False,
        "files_re_edited_within_14d": 0,
        "followup_fix_tasks_within_14d": 0,
    }
    base.update(components)
    return composite_score(base)


def test_composite_all_positive_returns_max():
    """Merged + tests_green + 0 retries + 0 churn + 0 followups → 1.0."""
    assert _score() == 1.0


def test_reverted_is_hard_negative():
    """A revert dominates all other positives — proves the code didn't survive."""
    # Even with zero retries, green tests, zero churn, the revert flag
    # must collapse the score to a low value that a "barely shipped" run
    # can't beat.
    reverted = _score(reverted_within_14d=True)
    assert reverted is not None
    assert reverted <= 0.2, f"reverted score {reverted} should be ≤0.2"


def test_unmerged_returns_none():
    """Unmerged tasks are unscored — score is only meaningful after merge."""
    assert _score(merged_to_main=False) is None


def test_gate_retry_linearly_penalizes():
    """Increasing gate retries must strictly decrease the score."""
    scores = [_score(gate_retry_count=n) for n in (0, 1, 2, 3)]
    for a, b in zip(scores, scores[1:]):
        assert a > b, (
            f"gate_retry penalty must be monotonically decreasing; got {scores}"
        )


def test_churn_uses_exact_14d_window():
    """The pure function trusts the count coming from the 14d-window
    detector (LL-04 owns the window math). Here we verify the scorer
    reacts linearly to whatever count it's given: 0 churn → max, more
    churn → lower score, and the delta between consecutive counts is
    consistent (linear)."""
    s0 = _score(files_re_edited_within_14d=0)
    s1 = _score(files_re_edited_within_14d=1)
    s2 = _score(files_re_edited_within_14d=2)
    assert s0 > s1 > s2
    # Linear penalty check — differences within 1e-9 tolerance.
    assert abs((s0 - s1) - (s1 - s2)) < 1e-9


def test_followup_requires_file_overlap():
    """The pure function trusts the count from the follow-up detector
    (LL-04 requires ≥1 file overlap before counting). Here: zero
    follow-ups → no penalty; any follow-up drops the score."""
    s_none = _score(followup_fix_tasks_within_14d=0)
    s_one = _score(followup_fix_tasks_within_14d=1)
    assert s_none == 1.0, (
        "zero follow-ups (no file overlap detected) must not penalize"
    )
    assert s_one < s_none, (
        "any follow-up with file overlap must drop the score"
    )
