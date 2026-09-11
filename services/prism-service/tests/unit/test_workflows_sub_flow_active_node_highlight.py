"""The occupied node of a drilled sub-workflow must render LIT in run mode
(task 67a98810).

Owner report on 7.13.296: open /workflows?task=<id> and drill into
Conductor > Steward > "Draft story (Observe-Reason-Validate)". The node
`loop` carries the orange occupancy badge and the teal wire into it is lit,
but the card draws DIMMED exactly like the idle node `text-challenge`.

Cause: run mode dims every node whose id is not in the run's traversedPath.
That path holds CONDUCTOR step ids (review_previous_notes, draft_story),
so on a sub-flow (ids loop, text-challenge) no node is ever on it and the
whole layer dims -- the occupied node included.

Scope addition, owner on the TOP-LEVEL canvas (same task): "if this is the
active step with an agent on it, put the agent icon and the outline for it
rather then the one with a sub agent please." draft_story held the task and
the badge but drew with no outline; the bright outline sat on the PASSED
review_previous_notes, and the agent marker rode the Steward bot wire.

The PRISM SPA has NO JS test runner, so this is pinned by reading the real
TS source. Every check runs on COMMENT-STRIPPED code, and blocks are cut by
brace depth, never a fixed character window.
"""
from __future__ import annotations

import re
from pathlib import Path

_WEB = Path(__file__).resolve().parent.parent.parent / "prism_service/web/src"
_GRAPH = _WEB / "live/workflowGraph.ts"
_PAGE = _WEB / "pages/WorkflowsPage.tsx"


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


def _guard_of(code: str, call: str) -> str:
    """The condition of the `if (...)` that directly governs `call`: the
    nearest one before it, with no closing brace between its `)` and the
    call (so an if-block that already closed never counts)."""
    at = code.index(call)
    start = code.rindex("if (", 0, at) + 3
    depth = 0
    for i in range(start, at):
        depth += {"(": 1, ")": -1}.get(code[i], 0)
        if depth == 0:
            assert "}" not in code[i:at], f"{call!r} is outside its if-block"
            return code[start + 1:i]
    raise AssertionError(f"no closed if-guard before {call!r}")


def test_occupied_node_is_not_dimmed_in_run_mode():
    code = _graph()
    node = _fn(code, "drawNode")
    consts = _consts(node)
    # The badge is drawn from this occupancy; the lit card must use the same.
    assert re.search(r"if\s*\(\s*n\.count\s*>\s*0\s*&&\s*!active\s*\)\s*"
                     r"drawOccupancy\(", node), "the occupancy badge guard moved"
    # ONE decision names the lit node, and a node with no occupancy can
    # never pass it (the misfire: every node of the sub-flow lit).
    lit = _fn(code, "isOccupiedLit")
    assert re.search(r"if\s*\([^;{]*n\.count\s*<=\s*0[^;{]*\)\s*return\s+false",
                     lit), "isOccupiedLit does not require n.count > 0: %r" % lit
    assert re.search(r"\brunMode\b", lit), "the lit rule is not scoped to run mode"
    flags = ["occupiedLit"]
    run_dim = consts.get("runDim", "")
    # Idle nodes of the layer must STAY dimmed in run mode.
    assert "runMode" in run_dim and re.search(
        r"traversedPath\.includes\(\s*n\.id\s*\)", run_dim), (
        "runDim no longer dims the nodes the run did not walk: %r" % run_dim)
    assert any(re.search(rf"!\s*{f}\b", run_dim) for f in flags), (
        "runDim still dims the occupied node: a sub-flow node id (loop) is "
        "never in the conductor-step traversedPath, so the node with the "
        "badge draws at 0.45 alpha like an idle one: %r" % run_dim)
    verdict = consts.get("verdictLook", "")
    assert any(re.search(rf"!\s*{f}\b", verdict) for f in flags), (
        "a stale not_reached/unknown verdict can still dim the occupied "
        "node through the verdict dim path: %r" % verdict)
    # Not dimmed is not enough: it takes the SAME accent the active node of
    # the top-level canvas draws (activeStroke border at the heavier width).
    strokes = [" ".join(m.group(1).split()) for m in
               re.finditer(r"ctx\.strokeStyle\s*=\s*([^;]+);", node)]
    card = [s for s in strokes if "activeStroke" in s]
    assert card, "the card border no longer draws activeStroke at all"
    assert any(re.search(rf"\b{f}\b[^?:]*\?\s*activeStroke", s)
               for s in card for f in flags), (
        "the occupied node does not take the active accent border: %r" % card)


def test_a_passed_step_never_outshines_the_live_one():
    """Owner screenshot: the bright outline sat on the PASSED
    review_previous_notes while the step the agent stood on had none. The
    live node's border must be HEAVIER than a verdict border, and glow."""
    node = _fn(_graph(), "drawNode")
    widths = [" ".join(m.group(1).split()) for m in
              re.finditer(r"ctx\.lineWidth\s*=\s*([^;]+);", node)]
    card = [w for w in widths if "verdictLook" in w and "occupiedLit" in w]
    assert card, "the card border width ignores the lit node: %r" % widths
    live = re.search(r"\boccupiedLit\s*\?\s*([\d.]+)", card[0])
    passed = re.search(r"\bverdictLook\s*\?\s*([\d.]+)", card[0])
    assert live and passed and float(live.group(1)) > float(passed.group(1)), (
        "a PASSED border is as heavy as the live one: %r" % card[0])
    glow = re.search(r"ctx\.shadowBlur\s*=\s*[1-9]", node)
    assert glow, "the live node has no glow"
    assert "occupiedLit" in _guard_of(node, glow.group(0)), (
        "the glow is not scoped to the lit node")


def test_the_run_still_foregrounds_a_layer_its_path_names():
    """The top-level Conductor canvas carries occupancy on 7 of its 10
    steps (other tasks). Lighting every occupied step there would undo run
    mode (task ce471e06). So the exemption applies only on a layer that the
    run's path names no node of -- decided once per frame in drawWorkflows
    -- plus the run's OWN step (the next test)."""
    code = _graph()
    frame = _block(code, "export function drawWorkflows(")
    layer = [name for name, rhs in _consts(frame).items()
             if re.search(r"g\.nodes\.some\(", rhs) and "traversedPath" in rhs]
    assert layer, "drawWorkflows never asks if the path names this layer"
    assert re.search(rf"isOccupiedLit\([^;]*\b{layer[0]}\b", frame), (
        "the layer answer is computed but never used to pick the lit node")
    lit = _fn(code, "isOccupiedLit")
    assert re.search(r"return\s*!\s*\w+\s*\|\|\s*n\.id\s*===", lit), (
        "on a layer the path names, every occupied step would light up: %r" % lit)


def test_the_runs_own_step_is_lit_on_the_top_level_canvas():
    """draft_story held the task (badge 1) and drew with no outline. The
    run's own step is where its task stands NOW -- task.workflow_step, with
    the last traversed stop as the fallback -- and it lights when occupied."""
    lit = _fn(_graph(), "isOccupiedLit")
    tip = [name for name, rhs in _consts(lit).items()
           if "currentStep" in rhs
           and re.search(r"traversedPath\[[^\]]*length\s*-\s*1\s*\]", rhs)]
    assert tip, "isOccupiedLit never resolves the run's own step: %r" % lit
    assert re.search(rf"return\s*!\s*\w+\s*\|\|\s*n\.id\s*===\s*{tip[0]}\b", lit), (
        "the run's own step is not the node that lights: %r" % lit)
    page = _code(_PAGE.read_text(encoding="utf-8"))
    assert re.search(r"currentStep\s*:[^;,}]*workflow_step", page), (
        "WorkflowsPage never hands the run's task.workflow_step to RunView")


_TAKEN = set("▣◇▼▲●◆▶■")  # glyphFor + the canvas's own node glyphs


def test_the_lit_step_carries_an_agent_glyph_not_only_its_persona():
    """SUPERSEDES the 7.13.299 version of this test, which parked the
    ambient occupancy PACKET on the node and hid it on the bot wire. That
    packet is not an agent symbol, and its motion stays as it was. The
    owner saw no inference symbol anywhere; the ▼ on a card is the Steward
    PERSONA. The step where an agent works now carries its own glyph,
    defined in palette.ts beside glyphFor, sharing no silhouette."""
    palette = _code((_WEB / "live/palette.ts").read_text(encoding="utf-8"))
    m = re.search(r'export const (\w+)\s*=\s*"(.)"\s*;', palette)
    assert m, "palette.ts exports no agent-at-work glyph beside glyphFor"
    name, glyph = m.group(1), m.group(2)
    assert glyph not in _TAKEN, f"{glyph!r} shares a silhouette in use"
    code = _graph()
    node = _fn(code, "drawNode")
    call = re.search(rf"ctx\.fillText\(\s*{name}\b", node)
    assert call, "drawNode never draws the agent glyph"
    assert "occupiedLit" in _guard_of(node, call.group(0)), (
        "the agent glyph is not scoped to the lit step")
    assert "ctx.fillText(n.glyph" in node, "the persona glyph is gone elsewhere"
    frame = _block(code, "export function drawWorkflows(")
    assert re.search(r"drawPackets\(\s*ctx\s*,\s*g\.packets\s*,", frame), (
        "the ambient occupancy packets are filtered -- leave them alone")
    lit_set = [k for k, v in _consts(frame).items() if "isOccupiedLit(" in v]
    assert lit_set, "drawWorkflows never collects the lit nodes"
    assert re.search(rf"drawNode\([^;]*\b{lit_set[0]}\.has\(\s*n\.id\s*\)", frame), (
        "drawNode is not told which node is lit")
