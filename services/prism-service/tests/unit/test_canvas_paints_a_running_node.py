"""A behaviour-layer node that is occupied but has NO WorkflowCore run
behind it must paint as unmistakably RUNNING, never as an idle box.

7.13.181/296 shipped isOccupiedLit + a 2.5px accent border and a static
14px glow for such a node, but two screenshots of a drilled layer (`loop` /
`text-challenge`) with `loop` occupied still looked IDENTICAL to a human:
same gray card, same dot, and the body kept the STALE "N runs · last Xh
ago" token-trend line -- run_count/last_run_at are completed-run history
and by design never change mid-execution. This slice replaces that stale
line with `RUNNING · <elapsed>` and gives the glow a gentle sin()-driven
pulse (never a sawtooth -- see verdictPaint's own stop_if) that
prefers-reduced-motion turns off.

The PRISM SPA has NO JS test runner, so this is pinned by reading the real
TS source. Comments are stripped and blocks are cut by brace depth, the
same discipline the sibling occupiedLit/verdict suites already use -- a
match inside a comment or docstring must never satisfy an assertion.
"""
from __future__ import annotations

import re
from pathlib import Path

_WEB = Path(__file__).resolve().parent.parent.parent / "prism_service/web/src"
_GRAPH = _WEB / "live/workflowGraph.ts"


def _code(src: str) -> str:
    """Source with /* */ spans and // line comments removed ('://' kept)."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(re.sub(r"(?<!:)//.*$", "", ln) for ln in src.splitlines())


def _block(src: str, anchor: str) -> str:
    """From `anchor` to the brace that closes its body, by depth."""
    start = src.index(anchor)
    depth = 0
    for i in range(src.index("{", start), len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    raise AssertionError(f"unbalanced braces after {anchor!r}")


def _consts(block: str) -> dict[str, str]:
    """Every `const NAME = RHS;` in the block, RHS whitespace-collapsed."""
    return {m.group(1): " ".join(m.group(2).split())
            for m in re.finditer(r"\bconst\s+(\w+)\s*=\s*([^;]+);", block)}


def _raw() -> str:
    return _GRAPH.read_text(encoding="utf-8")


def _graph() -> str:
    return _code(_raw())


def _fn(code: str, name: str) -> str:
    assert f"function {name}(" in code, f"no function {name} in the source"
    return _block(code, f"function {name}(")


def test_wf_node_carries_a_running_since_field():
    """A RUNNING node needs a started-at to compute its elapsed clock from."""
    node_type = _block(_code(_raw()), "export type WfNode = ")
    assert re.search(r"runningSince\s*\?:\s*number\s*\|\s*null", node_type), (
        "WfNode has no optional runningSince field: %r" % node_type)


def test_behaviour_running_is_occupied_with_no_active_progress():
    """The exact case this slice fixes: isOccupiedLit true, no
    ActiveNodeProgress -- a declarative behaviour layer has no WorkflowCore
    run to drive `active`, so this is how the canvas tells "occupied but
    idle-looking" apart from a genuinely live top-level FSM step."""
    node = _fn(_graph(), "drawNode")
    consts = _consts(node)
    assert consts.get("behaviourRunning") == "occupiedLit && !active", (
        "drawNode does not name the behaviour-running case: %r" % consts)


def test_the_corner_label_reads_running_under_that_branch():
    """The corner state word (PASSED/REFUSED/RUN Xs/...) must say RUNNING
    for a behaviour-running node, gated on the SAME const -- never falling
    through to the idle next-step arrow."""
    node = _fn(_graph(), "drawNode")
    assert re.search(r'behaviourRunning\s*\?\s*"RUNNING"', node), (
        "no corner RUNNING label gated on behaviourRunning")


def test_the_body_line_reads_running_and_elapsed_not_the_stale_trend():
    """The body's `summary` line (run_count/last_run_at) never changes
    mid-execution -- it is completed-run history. A behaviour-running node
    must show RUNNING · <elapsed> there instead, gated on behaviourRunning,
    and it must win over the plain `n.summary` fallback in the fillText
    call that actually draws the line."""
    node = _fn(_graph(), "drawNode")
    consts = _consts(node)
    elapsed = consts.get("runningElapsed", "")
    assert "behaviourRunning" in elapsed, (
        "runningElapsed is not gated on behaviourRunning: %r" % elapsed)
    assert re.search(r"RUNNING\s*·", elapsed), (
        "the body line never reads RUNNING with the middle-dot separator: "
        "%r" % elapsed)
    assert "runningSince" in elapsed, (
        "the elapsed reading is not derived from n.runningSince: %r" % elapsed)
    draw = re.search(r"ctx\.fillText\(\s*clip\([^)]*runningElapsed[^)]*\)",
                      node)
    assert draw, "the body line's own fillText never reads runningElapsed"


def test_no_alarm_words_on_a_running_node():
    """'idle'/'stalled' are reserved alarm words in this product (owner
    2026-07-21) -- a RUNNING node must never render either as a label."""
    code = _graph()
    for forbidden in ('"IDLE"', "'IDLE'", '"STALLED"', "'STALLED'",
                       "`IDLE`", "`STALLED`"):
        assert forbidden not in code, f"drawNode renders the alarm word {forbidden!r}"


def test_the_glow_pulses_with_sin_and_respects_reduced_motion():
    """A pulsing glow reads as "alive"; a sawtooth reads as measured
    progress with nothing behind it (the exact 7.13.174/175 mistake
    verdictPaint's own stop_if forbids repeating). prefers-reduced-motion
    must disable the pulse -- the steady occupiedLit glow set earlier in
    the function still stands, so the node never goes dark."""
    node = _fn(_graph(), "drawNode")
    pulse_block = _block(node, "if (behaviourRunning && !reducedMotion)")
    assert "Math.sin(" in pulse_block, (
        "the pulse is not sin()-driven: %r" % pulse_block)
    assert "%" not in pulse_block, (
        "a modulo in the pulse would be a sawtooth, not a smooth breath: "
        "%r" % pulse_block)
    assert "shadowBlur" in pulse_block and "shadowColor" in pulse_block


def test_drawnode_signature_and_the_call_site_thread_now_and_reduced_motion():
    """The pulse and the elapsed clock both need the rAF clock and the OS
    reduced-motion preference; drawWorkflows already has both in scope and
    must hand them down rather than drawNode inventing its own timer."""
    code = _graph()
    sig = re.search(r"function drawNode\(([^)]*)\)", code)
    assert sig, "drawNode signature not found"
    params = sig.group(1)
    assert re.search(r"\bnow\s*=\s*0\b", params), (
        "drawNode has no now parameter: %r" % params)
    assert re.search(r"\breducedMotion\s*=\s*false\b", params), (
        "drawNode has no reducedMotion parameter: %r" % params)
    frame = _block(code, "export function drawWorkflows(")
    call = re.search(r"drawNode\(([^;]*)\);", frame, re.S)
    assert call, "drawWorkflows never calls drawNode"
    args = call.group(1)
    assert re.search(r",\s*now\s*,", args), (
        "the rAF clock is not passed to drawNode: %r" % args)
    assert re.search(r"g\.isReducedMotion", args), (
        "reduced motion is not passed to drawNode: %r" % args)


def test_reduced_motion_getter_reads_the_private_flag_setreducedmotion_owns():
    """isReducedMotion must expose the SAME flag setReducedMotion sets --
    a second, independently-tracked flag would drift from the real OS
    preference the moment one path forgot to update it."""
    code = _graph()
    getter = _block(code, "get isReducedMotion(): boolean")
    assert "this.reducedMotion" in getter
