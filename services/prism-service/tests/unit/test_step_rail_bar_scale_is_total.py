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


def test_step_meta_track_fills_remaining_row_space_with_no_gap():
    """SUPERSEDES test_step_meta_track_width_is_fixed_not_data_dependent
    (same name kept in history, not in this file — the fixed-width/fixed-%
    contract it pinned is retired here, not just relaxed).

    Two prior fixes both pinned a WIDTH capped well short of the real
    available space (first w-[220px], then w-[50%] of the row) and
    right-aligned it with ml-auto. Both left a large UNUSED gap between the
    step label and where the bar started, because the bar's own width was
    never allowed to reach the space right after the label. Owner, 4th
    report: "why is there STILL so much dark space, are you not using the
    remote browser to see all of the dead space on each of those lines?"

    The fix: StepMeta's outer wrapper GROWS (flex-1) to consume every pixel
    left over after the row's label/badges, so there is no gap at all
    between the label and the bar. A short label leaves more room, so its
    bar is wider than a long label's bar -- that is correct, not a
    regression; label-independent width was the wrong goal, no dead space
    is the right one.
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

    # The wrapper must GROW to fill whatever space is left after the label —
    # no width cap, no flex-none, no fixed pixel/percent value.
    assert "flex-1" in outer_tokens, \
        f"StepMeta's outer wrapper must be flex-1 so it grows to fill the space left after the label, closing the dead gap; got: {outer_class!r}"
    assert "flex-none" not in outer_tokens, \
        f"flex-none would cap the wrapper back to a fixed size and reopen the dead-space bug; got: {outer_class!r}"
    assert not any(re.fullmatch(r"w-\[(\d+px|\d+%)\]", t) for t in outer_tokens), \
        f"a fixed w-[...] width (px or %) caps the bar short of the real available space; got: {outer_class!r}"
    assert not any(t.startswith("max-w-") for t in outer_tokens), \
        f"a max-w-[...] cap reopens the dead-space bug just like a fixed width would; got: {outer_class!r}"


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


def test_step_meta_is_a_direct_row_child_for_agent_steps():
    """Third follow-up (task c5b70c27, owner: "this is the third time we
    have tried to fix it and you keep saying its working, but we can see
    its not"). Root cause: for a real agent-work step, StepMeta was rendered
    NESTED inside an extra `<div className="ml-auto flex items-center gap-2
    min-w-0">` wrapper. That wrapper has no width of its own — it shrinks to
    fit its content. A CSS percentage width on a DESCENDANT of an
    auto-sized flex item resolves to 0 per spec, so StepMeta's own
    `w-[50%]` silently fell back to its `min-w-[220px]` floor every time —
    the exact same ~220px as the PREVIOUS (also-failed) fixed-width
    attempt, despite the class correctly saying 50%. Gate rows never had
    this wrapper and were never affected, which is why testing missed it
    twice. The fix: StepMeta renders as a DIRECT child of the row (which
    has a real, definite width) for the non-current-step case; only the
    CURRENT step's status badges keep the inner wrapper.
    """
    src = _read()

    # Anchor on the row-rendering block inside StepRail's items.map loop
    # (not GateRow, which never had this bug).
    row_block_idx = src.index("const stepTurns = byStep[s.id] ?? [];")
    row_block_end_idx = src.index("{rowOpen && hasTurns && <TurnList", row_block_idx)
    row_block = src[row_block_idx:row_block_end_idx]

    step_meta_call = '<StepMeta durMs={durByStep[s.id]} tokens={stepTokens?.[s.id] ?? 0} sumTokens={sumTokens} sumDur={sumDur} hasTurns={hasTurns} open={rowOpen} />'
    assert step_meta_call in row_block, \
        "could not find the per-row StepMeta call — did its props change?"

    wrapper_open = '<div className="ml-auto flex items-center gap-2 min-w-0">'
    wrapper_open_idx = row_block.index(wrapper_open)
    step_meta_idx = row_block.index(step_meta_call)

    # The wrapper must CLOSE (its matching `</div>`, i.e. the ternary's `) : (`
    # boundary) before StepMeta appears — StepMeta must not be textually
    # contained inside the wrapper's own JSX subtree.
    close_marker = ") : ("
    close_idx = row_block.index(close_marker, wrapper_open_idx)
    assert close_idx < step_meta_idx, \
        ("StepMeta must render OUTSIDE (after) the ml-auto/min-w-0 wrapper closes, "
         "as a direct child of the row — nesting it inside that wrapper reintroduces "
         "the collapsed-percentage-width bug")
