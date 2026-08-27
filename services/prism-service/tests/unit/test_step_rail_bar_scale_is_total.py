"""UI contract test for "StepRail bars scale to the run total, not the
single heaviest stage" (task c5b70c27-73fe-416e-b71c-48cbaa319206).

The PRISM SPA has NO JS test runner, so UI behavior is pinned by asserting
the ACTUAL web source (TSX) — the same pattern as
tests/unit/test_conductor_page_animated_cleanup_ui.py.

BUG: StepRail.tsx scaled each step's duration/token bar against the SINGLE
HEAVIEST stage in the run (`maxTokens`/`maxDur`), which made bar lengths
visually inconsistent and hard to compare (owner: "the bar lengths are
different lengths and hard to look at").

FIX: scale each bar against the SUM of every stage's duration/tokens across
the run, so a bar's width is that stage's proportional SHARE of the whole
run — a 100% bar means "this step took ALL of the elapsed time/tokens so
far" — and every bar is directly comparable to every other bar.
"""

from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).resolve()
_SRC = _HERE.parent.parent.parent / "prism_service" / "web" / "src"
_STEP_RAIL = _SRC / "components" / "conductor" / "StepRail.tsx"


def _read() -> str:
    return _STEP_RAIL.read_text(encoding="utf-8")


def test_bars_scale_to_sum_not_max():
    src = _read()

    # No leftover single-stage-max variables anywhere in the file — the old
    # `maxTokens`/`maxDur` denominators must be gone entirely, not just
    # renamed in some call sites and left dangling in others.
    assert "maxTokens" not in src, \
        "maxTokens (single-heaviest-stage denominator) must not remain in StepRail.tsx"
    assert "maxDur" not in src, \
        "maxDur (single-heaviest-stage denominator) must not remain in StepRail.tsx"

    # The replacement denominators are SUMS across every step in the run.
    assert "sumTokens" in src and "sumDur" in src, \
        "StepRail must compute sumTokens/sumDur (sum, not max) as the bar-scale denominators"

    # The sum computation must actually accumulate (+=) across `steps`, not
    # just track the largest single value. Anchor on the declaration inside
    # StepRail (not StepMeta's earlier prop-type usage of the same name).
    sum_block_idx = src.index("let sumTokens")
    sum_block = src[sum_block_idx: sum_block_idx + 400]
    assert "sumTokens +=" in sum_block, \
        "sumTokens must be accumulated with += across every step (a running total), not a max"
    assert "sumDur +=" in sum_block, \
        "sumDur must be accumulated with += across every step (a running total), not a max"

    # StepMeta — the component that actually renders the bar — must divide by
    # the sum for BOTH the token branch and the duration branch (the
    # documented likely_misfire: fixing only duration and leaving tokens on
    # the old max-based scale).
    step_meta_idx = src.index("function StepMeta")
    # Grab the StepMeta function body up to the next top-level function.
    next_fn_idx = src.index("\nfunction ", step_meta_idx + 1)
    step_meta_src = src[step_meta_idx:next_fn_idx]

    assert "sumTokens" in step_meta_src and "sumDur" in step_meta_src, \
        "StepMeta must receive and use sumTokens/sumDur as its scale props"
    assert "maxTokens" not in step_meta_src and "maxDur" not in step_meta_src, \
        "StepMeta must not reference any leftover max-based scale prop"

    # The pct computation itself: `max` (the local denominator name inside
    # StepMeta) must be assigned from sumTokens/sumDur, and the division must
    # still be val / max — i.e. the actual arithmetic denominator is now the
    # run total, not any single stage's value.
    assert "const useTokens = sumTokens > 0" in step_meta_src, \
        "StepMeta must gate token-vs-duration mode on sumTokens (the total), not a max"
    assert "const max = useTokens ? sumTokens : sumDur" in step_meta_src, \
        "StepMeta's scale denominator must be sumTokens (tokens) or sumDur (duration) — both sums"
    assert "const pct = max > 0 ? Math.max(2, Math.round((val / max) * 100)) : 0" in step_meta_src, \
        "the pct computation must divide by the sum-based `max` (2% visibility floor preserved)"

    # Both call sites that pass scale props into StepMeta/GateRow must use
    # the new sum-based prop names (regression guard: no orphaned max-based
    # arg at either call site).
    assert src.count("sumTokens={sumTokens} sumDur={sumDur}") >= 2, \
        "both StepMeta/GateRow call sites must pass the sum-based sumTokens/sumDur props"
