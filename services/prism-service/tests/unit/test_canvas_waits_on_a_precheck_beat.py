"""THE VISIBLE LIE (task b490fabc, fourth pass, owner: "you left something
broken, get the subagent working on visible truths"): the canvas painted a
node RUNNING for 30 minutes off a seat's own pre-check beat
(last_tool="resume_actuator_dispatch") that fired before the seat ever
found the task's claim was held by a dead process. get_workflows now emits
a per-lit-step `live` record (task_id/driver/tool/node/age_s/since/
dispatching) and drawNode must paint WAITING, never RUNNING, when a live
record is present but not dispatching.

The PRISM SPA has NO JS test runner, so this is pinned by reading the real
TS/Python source, the same discipline test_canvas_paints_a_running_node.py
already uses -- comments are stripped and blocks are cut by brace depth, so
a match inside a comment or docstring never satisfies an assertion.
"""
from __future__ import annotations

import re
from pathlib import Path

_WEB = Path(__file__).resolve().parent.parent.parent / "prism_service/web/src"
_GRAPH = _WEB / "live/workflowGraph.ts"
_API = Path(__file__).resolve().parent.parent.parent / "prism_service/api/workflows.py"


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


def _graph() -> str:
    return _code(_GRAPH.read_text(encoding="utf-8"))


def _fn(code: str, name: str) -> str:
    assert f"function {name}(" in code, f"no function {name} in the source"
    return _block(code, f"function {name}(")


def test_wf_node_carries_a_live_field():
    node_type = _block(_graph(), "export type WfNode = ")
    assert re.search(r"\blive\s*\?\s*:", node_type), (
        "WfNode has no optional live field: %r" % node_type)


def test_a_present_non_dispatching_live_record_paints_waiting_not_running():
    """A lit node whose n.live.dispatching is false must read WAITING at
    the corner -- never the RUNNING word, and the `waiting` gate must
    check n.live.dispatching, not just occupiedLit."""
    node = _fn(_graph(), "drawNode")
    consts = _consts(node)
    waiting = consts.get("waiting", "")
    assert "behaviourRunning" in waiting, (
        "waiting is not gated on behaviourRunning: %r" % waiting)
    assert "n.live" in waiting and "dispatching" in waiting, (
        "waiting does not read n.live.dispatching: %r" % waiting)
    assert re.search(r'waiting\s*\?\s*"WAITING"', node), (
        "no corner WAITING label gated on waiting")


def test_running_appends_driver_and_tool_when_dispatching():
    """When a genuine dispatch is open (n.live.since present), the body
    line must append WHO (driver) and WHAT (tool) is actually running --
    not just an elapsed clock -- so the truth reaches the card."""
    node = _fn(_graph(), "drawNode")
    consts = _consts(node)
    elapsed = consts.get("runningElapsed", "")
    assert "n.live.driver" in elapsed, (
        "runningElapsed never appends n.live.driver: %r" % elapsed)
    assert "n.live.tool" in elapsed, (
        "runningElapsed never appends n.live.tool: %r" % elapsed)
    assert "WAITING" in elapsed, (
        "runningElapsed has no WAITING branch: %r" % elapsed)


def test_no_alarm_words_stay_absent_on_the_waiting_treatment():
    """'idle'/'stalled' are reserved alarm words in this product -- WAITING
    must never be dressed up as either."""
    code = _graph()
    for forbidden in ('"IDLE"', "'IDLE'", '"STALLED"', "'STALLED'",
                       "`IDLE`", "`STALLED`"):
        assert forbidden not in code, f"drawNode renders the alarm word {forbidden!r}"


def test_waiting_never_pulses():
    """The pulse block (behaviourRunning && !reducedMotion) must skip
    itself entirely when waiting -- a seat's pre-check beat never reads as
    a heartbeat that is alive right now."""
    node = _fn(_graph(), "drawNode")
    pulse_outer = _block(node, "if (behaviourRunning && !reducedMotion)")
    assert re.search(r"if\s*\(\s*!waiting\s*\)", pulse_outer), (
        "the pulse block is not gated on !waiting: %r" % pulse_outer)


def test_open_dispatch_tools_named_in_the_api():
    """The two tools that mean a dispatch is genuinely open, named exactly
    where get_workflows reads them."""
    src = _API.read_text(encoding="utf-8")
    m = re.search(r"_OPEN_DISPATCH_TOOLS\s*:\s*frozenset\[str\]\s*=\s*frozenset\(\s*\{([^}]*)\}", src)
    assert m, "_OPEN_DISPATCH_TOOLS not found in workflows.py"
    body = m.group(1)
    assert "dispatch_guard_live" in body, body
    assert "claude_cli.invoke" in body, body
