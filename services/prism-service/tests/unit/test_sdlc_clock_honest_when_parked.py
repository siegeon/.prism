"""Pin: the SDLC step clock tells the truth when the task is parked
(task 19c03bbc).

Two defects, read directly off SdlcProgress.tsx (services/prism-service/
prism_service/web/src/components/conductor/SdlcProgress.tsx):

1. fmtClock has no hour rollover (`m = Math.floor(s / 60)`, unbounded), so a
   9h40m park literally renders "579:43" - the owner's reported screenshot.
2. `counting` (and therefore the clock + the '~Nh left' ETA span) is purely
   `state === "working" || "adrift" || "driving"` - a raw activity.state
   ping, with no reference to phase.tokens_since_step. A session can emit
   heartbeat activity with zero tokens landed and the clock keeps counting
   beside a '0 tok' readout.

The PRISM SPA has NO JS test runner, so these ACs are pinned by asserting
the ACTUAL TSX source - the established convention (test_conductor_page_
animated_cleanup_ui.py:4-6). Per this task's stop_if, assertions here use
brace/RHS-matching helpers (never a fixed character window), so a comment
sitting near a match can never satisfy an assertion meant for the real
guard - see _find_matching_brace / _const_rhs / _function_body /
_brace_block_starting below.
"""

from __future__ import annotations

import re
from pathlib import Path

_HERE = Path(__file__).resolve()
_SRC = _HERE.parent.parent.parent / "prism_service" / "web" / "src"
_PROGRESS = _SRC / "components" / "conductor" / "SdlcProgress.tsx"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Brace/RHS-matching helpers - parse the enclosing structure, never slice a
# fixed character window (a window can silently include/exclude a comment;
# see this task's stop_if and the repo's own "comment satisfied the
# assertion three times" lesson).
# ---------------------------------------------------------------------------

def _find_matching_brace(src: str, open_idx: int) -> int:
    """`src[open_idx]` must be '{'. Return the index of its matching '}'."""
    assert src[open_idx] == "{", src[open_idx:open_idx + 20]
    depth = 0
    for i in range(open_idx, len(src)):
        c = src[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
    raise AssertionError("unbalanced braces from index %d" % open_idx)


def _function_body(src: str, signature: str) -> str:
    """Full `<signature>(...) { ... }` text, matched by brace depth."""
    start = src.index(signature)
    open_idx = src.index("{", start)
    close_idx = _find_matching_brace(src, open_idx)
    return src[start:close_idx + 1]


def _const_rhs(src: str, name: str) -> str:
    """RHS of `const <name> = ...;`, scanned to the statement-terminating
    ';' at bracket depth 0 (never a fixed window - the RHS can be any
    length, and a naive `src.index(";", i)` would stop early on a nested
    string/ternary containing ';')."""
    marker = f"const {name} ="
    start = src.index(marker) + len(marker)
    depth = 0
    i = start
    while i < len(src):
        c = src[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == ";" and depth == 0:
            return src[start:i].strip()
        i += 1
    raise AssertionError(f"no terminating ';' found for const {name}")


def _brace_block_starting(src: str, marker: str) -> str:
    """Full `{<marker> ...}` JSX expression whose opening brace is
    immediately followed by `marker`, matched by brace depth."""
    idx = src.index("{" + marker)
    close_idx = _find_matching_brace(src, idx)
    return src[idx:close_idx + 1]


def _arrow_body(src: str, call_prefix: str) -> str:
    """Body of an arrow callback passed to a call, e.g.
    `useAnimationFrame((t) => { ... })` - call_prefix must end in '{'."""
    idx = src.index(call_prefix)
    open_idx = idx + len(call_prefix) - 1
    assert src[open_idx] == "{", call_prefix
    close_idx = _find_matching_brace(src, open_idx)
    return src[open_idx:close_idx + 1]


# ---------------------------------------------------------------------------
# AC-1 / AC-2 - fmtClock rolls over into an hour unit; stays the ONLY mm:ss
# clock formatter in the file; fmtEta's separate Nh/Nm ETA format is
# untouched, not duplicated.
# ---------------------------------------------------------------------------

def test_fmt_clock_rolls_over_into_hour_unit_past_3600s():
    src = _read(_PROGRESS)
    body = _function_body(src, "function fmtClock")
    assert "3600" in body, (
        "fmtClock must compute an hour component via a 3600s divisor - "
        "today it is `m = Math.floor(s / 60)` with no rollover, so a "
        '9h40m park renders "579:43" (the owner\'s reported screenshot)'
    )
    assert re.search(r"%\s*3600", body), (
        "the minutes component must be derived from `s % 3600` so it "
        "stays within 0-59 once an hour digit is present - no rendering "
        "path may print a minutes field > 99"
    )
    assert body.count("padStart(2") >= 2, (
        "both the minutes and seconds fields must be zero-padded to 2 "
        "digits in the h:mm:ss branch"
    )


def test_fmt_clock_is_the_only_mmss_formatter_fmt_eta_untouched():
    src = _read(_PROGRESS)
    clock_fns = re.findall(r"function (fmt\w*[Cc]lock\w*)\(", src)
    assert clock_fns == ["fmtClock"], (
        f"there must be exactly one minutes:seconds clock formatter "
        f"(fmtClock); found {clock_fns}"
    )
    eta_body = _function_body(src, "function fmtEta")
    assert "3600" in eta_body and "toFixed(1)" in eta_body, (
        "fmtEta's own separate Nh/Nm ETA format must stay intact and "
        "distinct from fmtClock's h:mm:ss format"
    )


# ---------------------------------------------------------------------------
# AC-3 / AC-5 / AC-7 - `counting` becomes token-aware via a named grace
# constant, while keeping the original live disjunction.
# ---------------------------------------------------------------------------

def test_counting_gate_is_token_aware():
    src = _read(_PROGRESS)
    rhs = _const_rhs(src, "counting")
    assert "tokens" in rhs, (
        "`counting` must reference `tokens` (phase.tokens_since_step) - "
        "a session can emit heartbeat activity.state with zero tokens "
        "landed, which must not read as genuine execution time"
    )


def test_count_grace_constant_is_module_level_and_referenced_not_a_literal():
    src = _read(_PROGRESS)
    assert re.search(r"^const COUNT_GRACE_S\s*=\s*90\s*;", src, re.M), (
        "a module-level `COUNT_GRACE_S = 90` constant must gate the "
        "count-without-tokens-yet window (matches this file's own "
        "documented 90s session-quiet window)"
    )
    const_idx = src.index("const COUNT_GRACE_S")
    component_idx = src.index("export default function SdlcProgress")
    assert const_idx < component_idx, (
        "COUNT_GRACE_S must be declared at module level, above the "
        "component function"
    )
    rhs = _const_rhs(src, "counting")
    assert "COUNT_GRACE_S" in rhs, (
        "`counting` must reference COUNT_GRACE_S, never a bare literal 90"
    )


def test_counting_keeps_the_original_live_disjunction_joined_by_and():
    src = _read(_PROGRESS)
    rhs = _const_rhs(src, "counting")
    assert "live" in rhs, (
        "`counting` must still be gated by the original working/adrift/"
        "driving disjunction (`live`) - genuine execution must keep "
        "counting, this is additive, not a replacement"
    )
    assert "&&" in rhs, (
        "the live disjunction and the new token clause must be joined "
        "by && (both must hold), not one replacing the other"
    )


# ---------------------------------------------------------------------------
# AC-4 - the rAF early-return guard stays exactly `counting`; the new
# token-gate logic lives ONLY in the `counting` definition, never
# duplicated into the rAF block.
# ---------------------------------------------------------------------------

def test_raf_early_return_guard_is_exactly_counting_no_duplicated_logic():
    src = _read(_PROGRESS)
    body = _arrow_body(src, "useAnimationFrame((t) => {")
    assert "if (!counting) {" in body, (
        "the rAF early-return guard must remain exactly `if (!counting)`"
    )
    assert "tokens_since_step" not in body and "COUNT_GRACE_S" not in body, (
        "token-awareness belongs solely in the `counting` definition - "
        "it must not be duplicated as new logic inside the rAF block"
    )


# ---------------------------------------------------------------------------
# AC-6 - the ETA span gates on `counting`, not the old bare `live`.
# ---------------------------------------------------------------------------

def test_eta_span_gates_on_counting_not_bare_live():
    src = _read(_PROGRESS)
    assert re.search(r"\{\s*counting\s*&&\s*\(phase\?\.eta_s", src), (
        "the ETA span ('~Nh left') must gate on `counting`, not the old "
        "bare `live`, so it never disagrees with the clock about whether "
        "real execution time is elapsing"
    )
    assert not re.search(r"\{\s*live\s*&&\s*\(phase\?\.eta_s", src), (
        "the ETA span must no longer gate on the bare `live` flag"
    )


# ---------------------------------------------------------------------------
# AC-8 - when not counting, the clock renders an explicit, non-animating
# 'waiting' label sourced from phase.in_step_s (never idle/stalled); the
# false HIDDEN-when-parked comment is corrected.
# ---------------------------------------------------------------------------

def test_clock_renders_waiting_label_from_in_step_s_when_not_counting():
    src = _read(_PROGRESS)
    block = _brace_block_starting(src, "counting")
    low = block.lower()
    assert "waiting" in low, (
        "the non-counting branch must render an explicit 'waiting' label "
        "instead of rendering nothing"
    )
    assert "idle" not in low and "stalled" not in low, (
        "the waiting label must not read as an idle/stalled alarm word - "
        "those mean 'I must intervene' (owner 2026-07-21) and this is "
        "the normal condition of a parked step"
    )
    assert "in_step_s" in block, (
        "the waiting label's time must be sourced from phase.in_step_s "
        "(server truth), not a client clock"
    )
    assert "fmtClock(liveInStep)" in block, (
        "the counting branch must still render the live counting clock"
    )


def test_hidden_when_parked_comment_is_corrected():
    src = _read(_PROGRESS)
    assert "clock is HIDDEN" not in src, (
        "the load-bearing comment claiming the execution clock is HIDDEN "
        "when parked is false once it renders a waiting label - correct "
        "the comment to describe the actual behavior"
    )


# ---------------------------------------------------------------------------
# AC-9 (task 95474ec7, owner live): "waiting" contradicts a "working" state
# label. An epic-rollup can be state=working (real subtree motion, see
# ConductorService._subtree_active) while THIS task's own step never streams
# a token, so `counting` stays false and the OLD unconditional "waiting"
# label read as a direct contradiction next to the state pill's "working" /
# "in progress" text. Additive: every other not-counting state keeps
# "waiting" (test_clock_renders_waiting_label_from_in_step_s_when_not_counting
# above still passes unmodified).
# ---------------------------------------------------------------------------

def test_working_state_says_elapsed_not_waiting_when_not_counting():
    src = _read(_PROGRESS)
    block = _brace_block_starting(src, "counting")
    assert 'state === "working" ? "elapsed" : "waiting"' in block, (
        "the non-counting clock label must say 'elapsed' (not the "
        'contradictory "waiting") specifically when state === "working" -- '
        "an epic-rollup can be genuinely working via subtree motion while "
        "its own step streams no tokens"
    )


def test_non_working_states_still_get_the_original_waiting_label():
    """The fix must be additive, not a replacement -- a genuinely parked
    task (state e.g. "adrift", "awaiting_gate", or the bare status fallback)
    keeps the original honest "waiting" wording."""
    src = _read(_PROGRESS)
    block = _brace_block_starting(src, "counting")
    assert '"waiting"' in block, (
        "the ternary's non-working branch must still literally read "
        '"waiting" for every other not-counting state'
    )
