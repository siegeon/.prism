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


def test_step_meta_track_width_is_fixed_not_data_dependent():
    """Follow-up bug (still visible after the sum-based pct fix, task
    c5b70c27): StepMeta's OUTER wrapper div sized the bar's track with
    `flex-1` + `max-w-[420px]` — a flex-grown width capped, not fixed. That
    made the track's actual rendered width depend on how much horizontal
    space was left over after each row's own label text, so rows with
    longer labels rendered a visibly NARROWER bar even though the fill pct
    math was already correct. The fix is a FIXED pixel width (`w-[Npx]` with
    `flex-none`) so every row's track renders at the same width regardless
    of sibling label length.
    """
    src = _read()

    step_meta_idx = src.index("function StepMeta")
    next_fn_idx = src.index("\nfunction ", step_meta_idx + 1)
    step_meta_src = src[step_meta_idx:next_fn_idx]

    import re

    # Extract the OUTER wrapper's className specifically — the first
    # `<div className="...">` in StepMeta's returned JSX (the row-level
    # track container), not the inner bar/caption column.
    outer_match = re.search(r'<div className="([^"]*)">', step_meta_src)
    assert outer_match, "could not locate StepMeta's outer wrapper div"
    outer_class = outer_match.group(1)
    outer_tokens = outer_class.split()

    # The wrapper must declare a fixed pixel width combined with flex-none —
    # not a flex-grown/capped sizing mechanism.
    # Owner 2026-08-27 ("this bar is not taking up the length of the area
    # STILL ... it should be like 40%-60%"): the width is a fixed SHARE of
    # the row (w-[N%], 40..60) or a fixed pixel width -- either is
    # label-independent, which is what this test guards; a 220px stub at the
    # right edge of a 900px row was the bug the owner saw.
    assert any(re.fullmatch(r"w-\[(\d+px|[4-6]\d%)\]", t) for t in outer_tokens), \
        f"StepMeta's outer wrapper must use a fixed width (w-[Npx]) or a fixed row share (w-[40-60%]); got: {outer_class!r}"
    assert "flex-none" in outer_tokens, \
        f"StepMeta's outer wrapper must be flex-none so its width is not flex-grown; got: {outer_class!r}"

    # The old data-dependent sizing mechanism must be gone from the wrapper
    # entirely — a row-length-dependent width is exactly the bug this test
    # guards against.
    assert "flex-1" not in outer_tokens, \
        f"the outer wrapper must not use flex-1 (flex-grown) sizing — width must be fixed, not data-dependent; got: {outer_class!r}"
    assert not any(t.startswith("max-w-") for t in outer_tokens), \
        f"the old max-w-[...] CAP (not a fixed width) must not remain on StepMeta's outer wrapper; got: {outer_class!r}"


def test_gate_wait_time_is_excluded_from_the_bar_scale():
    """Second follow-up (task c5b70c27): sumDur/sumTokens previously summed
    EVERY step including GATE steps. A gate's duration is human approval wait
    time, not agent work — one gate that sat pending 85h49m dwarfed every
    real agent step (30s-3min) so completely that every real step's bar was
    crushed to the 2% floor while the gate's bar alone read ~100%. The fix
    excludes gate steps from the accumulation loop entirely.
    """
    src = _read()

    sum_block_idx = src.index("let sumTokens")
    # Grab through the closing brace of the accumulation loop.
    loop_end_idx = src.index("}", src.index("for (const s of steps)", sum_block_idx))
    sum_block = src[sum_block_idx:loop_end_idx + 1]

    assert "for (const s of steps)" in sum_block, \
        "sumTokens/sumDur must be accumulated by iterating `steps`"
    assert 'if (s.type === "gate") continue' in sum_block, \
        "the accumulation loop must skip gate steps (s.type === \"gate\") before adding to sumTokens/sumDur"

    # The skip must appear BEFORE either accumulation line, so a gate step
    # never contributes to either sum (order matters — a skip placed after
    # the += lines would be dead code).
    skip_idx = sum_block.index('if (s.type === "gate") continue')
    tokens_add_idx = sum_block.index("sumTokens +=")
    dur_add_idx = sum_block.index("sumDur +=")
    assert skip_idx < tokens_add_idx and skip_idx < dur_add_idx, \
        "the gate-skip must run before sumTokens/sumDur are incremented, or gate durations still leak into the sum"


def test_gate_row_never_renders_a_proportional_bar():
    """A resolved (non-pending) gate row must render its duration/tokens as
    plain caption text ONLY — never through the proportional-bar-rendering
    path used for real agent-step bars. A gate's wait time is not
    commensurable with agent work, so a `pct`-based fill for it is
    misleading no matter what it's scaled against.
    """
    src = _read()

    # GateRow's resolved-gate branch (the `else` of `gi.state === "pending"`)
    # must pass isGate into StepMeta so the bar-rendering branch is skipped.
    # GateRow is the last function declared in the file, so there is no
    # following "\nfunction " to bound it — read to end of file instead.
    gate_row_idx = src.index("function GateRow")
    gate_row_src = src[gate_row_idx:]

    assert "<StepMeta durMs={durMs} tokens={tokens} sumTokens={sumTokens} sumDur={sumDur} hasTurns={(turns?.length ?? 0) > 0} open={open} isGate />" in gate_row_src, \
        "GateRow's resolved-gate call site must pass isGate to StepMeta so no proportional bar renders for a gate"

    # StepMeta itself: the bar `<div className="h-2 rounded-full` track must
    # be conditioned on NOT isGate, so no code path can render a bar for a
    # gate regardless of its val/pct.
    step_meta_idx = src.index("function StepMeta")
    step_meta_next_fn_idx = src.index("\nfunction ", step_meta_idx + 1)
    step_meta_src = src[step_meta_idx:step_meta_next_fn_idx]

    assert "isGate" in step_meta_src, \
        "StepMeta must accept an isGate prop"
    bar_div_idx = step_meta_src.index('<div className="h-2 rounded-full')
    # The condition guarding that div must appear on the line(s) just before
    # it and must reference !isGate.
    guard_window = step_meta_src[max(0, bar_div_idx - 200):bar_div_idx]
    assert "!isGate" in guard_window, \
        "the bar track div (`h-2 rounded-full`) must be conditioned on !isGate so a gate never renders a proportional fill"
