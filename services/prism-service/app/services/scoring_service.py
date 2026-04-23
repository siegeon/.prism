"""Scoring service — Layer-A quantitative composite score for task outcomes.

Pure functions only (no I/O). The daemon in LL-04 walks git + test state
and hands pre-computed signals here; this module turns those signals
into a single quality score that Brain.best_prompt can consume.

Parent task: 37932f3f · LL-02.
"""

from __future__ import annotations

from typing import Any, Optional


# Penalty coefficients. Kept as module constants so LL-05's CUPED layer
# can reason about expected score distributions without reimplementing
# the math.
_GATE_RETRY_PENALTY = 0.10         # per retry, capped at 5 retries
_GATE_RETRY_CAP = 5
_TESTS_RED_PENALTY = 0.30          # tests red on merge: big hit
_CHURN_PENALTY_PER_FILE = 0.05     # per file re-edited within 14d, capped at 10
_CHURN_CAP = 10
_FOLLOWUP_PENALTY_PER_TASK = 0.15  # per follow-up fix task within 14d, capped at 3
_FOLLOWUP_CAP = 3
_REVERT_FLOOR = 0.1                # hard negative — revert proves it didn't work


def composite_score(components: dict[str, Any]) -> Optional[float]:
    """Return a Layer-A quality score in [0, 1] or ``None`` if unscorable.

    Expected keys in ``components``:
      * merged_to_main (bool) — binary gate. False returns ``None``; an
        unmerged task has no outcome yet.
      * reverted_within_14d (bool) — hard negative. A revert is strong
        evidence the code didn't survive contact with the codebase.
      * gate_retry_count (int) — linear penalty per retry, capped.
      * tests_green_on_merge (bool) — red tests on merge knock a chunk
        off the score.
      * files_re_edited_within_14d (int) — churn proxy. Each file
        re-edited reduces the score linearly, capped.
      * followup_fix_tasks_within_14d (int) — the strongest "it didn't
        stick" signal. Each follow-up drops the score linearly, capped.

    Pure: no DB access, no clock, no side effects.
    """
    if not components.get("merged_to_main"):
        return None

    # Revert is a hard negative that dominates any other positive signal.
    # A reverted PR with perfect retries+tests still scores below
    # ``_REVERT_FLOOR`` — that's the whole point.
    if components.get("reverted_within_14d"):
        return _REVERT_FLOOR

    score = 1.0

    retries = min(
        int(components.get("gate_retry_count") or 0), _GATE_RETRY_CAP
    )
    score -= _GATE_RETRY_PENALTY * retries

    if not components.get("tests_green_on_merge", True):
        score -= _TESTS_RED_PENALTY

    churn = min(
        int(components.get("files_re_edited_within_14d") or 0), _CHURN_CAP
    )
    score -= _CHURN_PENALTY_PER_FILE * churn

    followups = min(
        int(components.get("followup_fix_tasks_within_14d") or 0),
        _FOLLOWUP_CAP,
    )
    score -= _FOLLOWUP_PENALTY_PER_TASK * followups

    return max(0.0, min(1.0, score))
