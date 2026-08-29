"""A parked step's bar must not claim progress that is not happening.

Owner, 2026-08-29, watching a task sit at story_gate: "the story gate progress
loading animation is not very real, it just fills and fills and fills, but it
never actually finishes ... it shows us that work is being done when no work
is being done, unless there is work being done IN that task."

MEASURED on task 54585a5f parked at story_gate, 75 wall seconds apart:
    in_step_s      245.5 -> 321.9   (+76.4s while PARKED)
    task_motion_s  246.5 -> 321.9   (tracks it, so frozenInStep ~= 0)
    segment fill   0.900 -> 0.951   climbing to the 0.97 asymptote

So the execution clock beside the bar correctly read ~no execution time while
the bar read 95% full: one step, one second, two numbers contradicting each
other. The parked branch must fill from the SAME frozen figure the clock
prints, never the raw server in_step_s that grows while the ball is in
someone else's court.

The PRISM SPA has no JS test runner, so this pins the actual TSX source.
"""
from __future__ import annotations

import re
from pathlib import Path

_SRC = (Path(__file__).resolve().parent.parent.parent
        / "prism_service/web/src/components/conductor/SdlcProgress.tsx")


def _source() -> str:
    return _SRC.read_text(encoding="utf-8")


def _parked_branch(src: str) -> str:
    """The `if (!counting) { ... }` body -- parsed by brace depth, never a
    fixed character window (a comment above the call would push the real
    statement out of a window and the assertion would pass on prose)."""
    i = src.index("if (!counting) {")
    j = src.index("{", i)
    depth, k = 0, j
    while k < len(src):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[j:k + 1]
        k += 1
    raise AssertionError("unbalanced parked branch")


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", "", text)


def test_a_parked_step_fills_from_the_frozen_clock_not_raw_in_step_s():
    body = _strip_comments(_parked_branch(_source()))
    calls = re.findall(r"liveFraction\(([^)]*)\)", body)
    assert calls, f"the parked branch computes no fill at all: {body!r}"
    for args in calls:
        assert "frozenInStep" in args, (
            "the parked branch fills from the raw server in_step_s, which keeps "
            "growing while the gate waits -- measured 0.900 -> 0.951 on a task "
            f"doing no work. Got liveFraction({args})")


def test_the_clock_and_the_fill_use_the_same_frozen_figure():
    """The bug was two measures of one second disagreeing. Whatever the parked
    branch prints as the clock, the fill must be computed from it too."""
    body = _strip_comments(_parked_branch(_source()))
    assert "setLiveInStep(frozenInStep)" in body, (
        "the parked clock no longer prints frozenInStep; the fill assertion "
        "above is anchored to it, so they must move together")


def test_live_fraction_accepts_an_explicit_seconds_override():
    src = _strip_comments(_source())
    sig = re.search(r"function liveFraction\((.*?)\)\s*:\s*number", src,
                    flags=re.DOTALL)
    assert sig, "liveFraction signature not found"
    assert "secondsOverride" in sig.group(1), (
        f"liveFraction cannot be told which seconds to use: {sig.group(1)!r}")
