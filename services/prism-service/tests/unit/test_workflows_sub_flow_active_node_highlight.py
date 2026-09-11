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

The PRISM SPA has NO JS test runner, so this is pinned by reading the real
TS source. Every check runs on COMMENT-STRIPPED code, and blocks are cut by
brace depth, never a fixed character window.
"""
from __future__ import annotations

import re
from pathlib import Path

_GRAPH = (Path(__file__).resolve().parent.parent.parent
          / "prism_service/web/src/live/workflowGraph.ts")


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


def _draw_node() -> str:
    return _block(_code(_GRAPH.read_text(encoding="utf-8")), "function drawNode(")


def _occupied_flags(consts: dict[str, str]) -> list[str]:
    """Flags that REQUIRE the badge's own occupancy (`n.count > 0`) as a
    conjunct. An `||` would let a node with no occupancy light up too, which
    is the misfire of lighting every node of the sub-flow."""
    return [name for name, rhs in consts.items()
            if re.search(r"\bn\.count\s*>\s*0\b", rhs) and "||" not in rhs]


def test_occupied_node_is_not_dimmed_in_run_mode():
    node = _draw_node()
    consts = _consts(node)
    # The badge is drawn from this occupancy; the lit card must use the same.
    assert re.search(r"if\s*\(\s*n\.count\s*>\s*0\s*&&\s*!active\s*\)\s*"
                     r"drawOccupancy\(", node), "the occupancy badge guard moved"
    flags = _occupied_flags(consts)
    assert flags, (
        "drawNode names no occupied-node flag built on `n.count > 0` -- the "
        "node that carries the badge cannot be told apart from an idle one")
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
    widths = [" ".join(m.group(1).split()) for m in
              re.finditer(r"ctx\.lineWidth\s*=\s*([^;]+);", node)]
    assert any(re.search(rf"\b{f}\b[^?:]*\?\s*1\.5", w)
               for w in widths for f in flags), (
        "the occupied node border keeps the idle 1px width: %r" % widths)


def test_the_run_still_foregrounds_a_layer_its_path_names():
    """The top-level Conductor canvas carries occupancy on 7 of its 10
    steps (other tasks). Lighting every occupied step there would undo run
    mode (task ce471e06). So the exemption applies only on a layer that the
    run's path names no node of -- decided once per frame in drawWorkflows
    and handed to drawNode."""
    code = _code(_GRAPH.read_text(encoding="utf-8"))
    frame = _block(code, "export function drawWorkflows(")
    layer = [name for name, rhs in _consts(frame).items()
             if re.search(r"g\.nodes\.some\(", rhs) and "traversedPath" in rhs]
    assert layer, "drawWorkflows never asks if the path names this layer"
    call = frame[frame.index("drawNode("):]
    assert re.search(rf"\b{layer[0]}\b", call[:call.index(";")]), (
        "the layer answer is computed but never handed to drawNode")
    node = _draw_node()
    params = re.findall(r"(\w+)\s*(?::|=)", node[:node.index("{")])
    flags = _occupied_flags(_consts(node))
    assert any(re.search(rf"!\s*{p}\b", _consts(node)[f])
               for f in flags for p in params if p not in ("active", "n")), (
        "the occupied-node flag ignores whether the path names this layer")
