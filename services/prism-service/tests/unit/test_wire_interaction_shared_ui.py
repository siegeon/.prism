"""Pinned UI contract for the SHARED wire-interaction layer (task
be7a5d2d): the editing grammar the /workflows canvas grew on task
f506ece4 -- selection with the full-orange stroke, wire-body segment
dragging with automatic anchor mint/retire, double-click waypoints,
post-route simplifyPath, endpoint port re-docking, per-project
persistence -- is promoted into ONE canvas-agnostic module that BOTH
canvases consume, rather than forked into a second live-board copy.

The ticket's three stop_if clauses are what these tests exist to catch:
  1. editor logic existing in two copies after the change,
  2. graphState.ts being rewritten rather than integrated,
  3. persistence keys colliding across surfaces.

Same no-JS-runner convention as test_live_wire_side_ports_ui.py and
test_workflows_section_ui.py: pins the ACTUAL ts/tsx source via real
exported names and comment-stripped, brace-matched function bodies --
never a bare substring a doc comment could satisfy, and never a fixed
character window a comment above the code could push the real guard out
of.
"""

from __future__ import annotations

import re
from pathlib import Path

_HERE = Path(__file__).resolve()
_SRC = _HERE.parent.parent.parent / "prism_service" / "web" / "src"
_LIVE = _SRC / "live"
_PAGES = _SRC / "pages"

# The promoted module. Named for what it DOES, not for the canvas it grew
# on: a module called workflowWires.ts imported by the live board's
# graphState.ts is a name that invites the very drift this task exists to
# undo.
_SHARED = _LIVE / "wireEditing.ts"

# The editing primitives. Wherever these are DEFINED is the interaction
# layer; if that set of definitions ever spans two files, the drift is
# back.
_EDITOR_PRIMITIVES = (
    "simplifyPath", "grabSegment", "moveSegment", "insertWaypoint",
    "nearestOnPolyline", "routeLegs",
)


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _strip_comments(src: str) -> str:
    """Removes /* */ blocks and // line comments so a doc comment
    mentioning a symbol can never satisfy an assertion about the symbol's
    real code -- a comment has satisfied this kind of assertion before
    (see the wires.ts/cards.ts history noted in
    test_live_wire_side_ports_ui.py)."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    lines = []
    for line in src.split("\n"):
        idx = line.find("//")
        lines.append(line if idx == -1 else line[:idx])
    return "\n".join(lines)


def _body(src: str, signature: str) -> str:
    """Everything between `signature`'s opening `{` and its matching
    closing `}`, counted by brace depth over the COMMENT-STRIPPED source."""
    stripped = _strip_comments(src)
    idx = stripped.find(signature)
    assert idx != -1, f"{signature!r} not found in source"
    brace_start = stripped.find("{", idx)
    assert brace_start != -1, f"no body opened after {signature!r}"
    depth = 0
    for i in range(brace_start, len(stripped)):
        if stripped[i] == "{":
            depth += 1
        elif stripped[i] == "}":
            depth -= 1
            if depth == 0:
                return stripped[brace_start:i + 1]
    raise AssertionError(f"unbalanced braces after {signature!r}")


def _shared_src() -> str:
    """The promoted module's source, with a NAMED failure when it does not
    exist yet -- a bare FileNotFoundError traceback tells the next reader
    nothing about which contract is unmet."""
    assert _SHARED.exists(), (
        "live/wireEditing.ts does not exist -- the interaction layer is "
        "still surface-branded, so the live board cannot consume it "
        "without forking a copy")
    return _read(_SHARED)


def _defines(src: str, name: str) -> bool:
    """True if `src` DEFINES `name` (function declaration, class method or
    arrow-function const) rather than merely calling or importing it."""
    stripped = _strip_comments(src)
    patterns = (
        rf"function\s+{name}\s*\(",
        rf"^\s*(?:private\s+|public\s+|protected\s+)?{name}\s*\([^)]*\)\s*(?::[^{{;]+)?\{{",
        rf"(?:const|let)\s+{name}\s*=\s*(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>",
    )
    return any(re.search(p, stripped, re.MULTILINE) for p in patterns)


# ---------------------------------------------------------------------------
# AC-1 / stop_if #1: ONE module, imported by BOTH canvases, with zero
# editor logic duplicated into either consumer.
# ---------------------------------------------------------------------------

def test_wire_editing_is_one_shared_module():
    assert _SHARED.exists(), (
        "live/wireEditing.ts does not exist -- the interaction layer is "
        "still surface-branded, so the live board cannot consume it "
        "without forking a copy")
    for consumer in ("graphState.ts", "workflowGraph.ts"):
        src = _strip_comments(_read(_LIVE / consumer))
        assert re.search(r'from\s+"\./wireEditing"', src), (
            f"live/{consumer} must import the shared interaction layer "
            "from ./wireEditing -- both canvases consume ONE module")


def test_no_consumer_redefines_an_editor_primitive():
    """stop_if #1 stated as an executable check: the editing primitives
    are DEFINED in exactly one file across live/*.ts."""
    for name in _EDITOR_PRIMITIVES:
        homes = [p.name for p in sorted(_LIVE.glob("*.ts")) if _defines(_read(p), name)]
        assert homes == ["wireEditing.ts"], (
            f"{name} must be defined ONLY in live/wireEditing.ts, found in "
            f"{homes or 'no file at all'} -- a second definition is the "
            "two-copies drift this task exists to undo")


def test_the_shared_module_owns_no_router_and_no_port_math():
    """FR-6 / AC-1, carried forward from
    test_workflows_section_ui.py::test_waypoint_paths_still_route_through_the_one_orthogonal_router
    and re-pointed at the promoted module: routeOrthogonal stays the one
    router and portPoint stays defined exactly once, so the shared layer
    must IMPORT both, never re-derive them."""
    src = _shared_src()
    stripped = _strip_comments(src)
    assert re.search(r'routeOrthogonal[^;]*from\s+"\./wires"', stripped, re.DOTALL), (
        "live/wireEditing.ts must import routeOrthogonal from ./wires")
    assert re.search(r"routeOrthogonal\s*\(", stripped), (
        "the leg builder never calls routeOrthogonal -- it is constructing "
        "its own polyline, which is a second router")
    for owned_elsewhere in ("routeOrthogonal", "portPoint", "portFromWorld",
                            "autoPort", "drawWire"):
        assert not _defines(src, owned_elsewhere), (
            f"live/wireEditing.ts re-defines {owned_elsewhere} instead of "
            "importing it from live/wires")


# ---------------------------------------------------------------------------
# AC-2 / FR-4: ONE orange. Body, endpoint dots and bend handles take the
# same PALETTE.selection token through the shared renderer, on both
# canvases -- orange only at the handles is the half-signal the owner
# already rejected once (ticket 53cc9bcc).
# ---------------------------------------------------------------------------

def test_the_selected_wire_paint_lives_in_the_shared_renderer():
    src = _shared_src()
    assert re.search(r"export function drawEditableWire\s*\(", _strip_comments(src)), (
        "wireEditing.ts must export drawEditableWire -- the one function "
        "that knows what a selected wire looks like")
    body = _body(src, "export function drawEditableWire")
    assert "PALETTE.selection" in body, (
        "the selection hue must come from the PALETTE token, never a "
        "second orange literal")
    assert "drawWire(" in body and "drawPort(" in body, (
        "drawEditableWire must paint the body through the shared drawWire "
        "and one port dot per endpoint through the shared drawPort")


def test_both_canvases_paint_wires_through_that_one_renderer():
    for consumer in ("draw.ts", "workflowGraph.ts"):
        src = _strip_comments(_read(_LIVE / consumer))
        assert "drawEditableWire(" in src, (
            f"live/{consumer} must paint its wires through drawEditableWire "
            "-- two painters is how the two boards drifted to two oranges")
        assert "PALETTE.selection" not in src, (
            f"live/{consumer} must NOT carry its own selection colour; the "
            "shared renderer owns it")


# ---------------------------------------------------------------------------
# AC-3 / AC-4: the live board gains the identical gesture set, and the
# hit-test ordering that disambiguates it. The ordering assertion in
# test_live_wire_side_ports_ui.py (action-strip < port < node) stays true
# and is EXTENDED here rather than replaced.
# ---------------------------------------------------------------------------

def test_live_page_gains_the_waypoint_and_segment_drag_modes():
    src = _strip_comments(_read(_PAGES / "LivePage.tsx"))
    m = re.search(r"type DragMode\s*=\s*([^;]+);", src)
    assert m, "LivePage must still declare a DragMode union"
    modes = m.group(1)
    for mode in ('"waypoint"', '"segment"'):
        assert mode in modes, (
            f"DragMode must gain {mode} -- without it the live board cannot "
            "bend a wire or drag its body")


def test_live_page_hit_order_runs_handles_then_nodes_then_wire_body():
    src = _strip_comments(_read(_PAGES / "LivePage.tsx"))
    down = src[src.index("const onPointerDown"):src.index("const onPointerMove")]
    order = [
        ("actionStripHitTest(", "the selected card's action strip"),
        ("waypointAtWorld(", "a placed bend handle"),
        ("portAtWorld(", "an endpoint port dot"),
        ("beginSegmentDrag(", "a body segment of the selected wire"),
        ("nodeAtWorld(", "the card body"),
        ("wireAtWorld(", "any wire, which selects it"),
    ]
    seen = []
    for token, what in order:
        idx = down.find(token)
        assert idx != -1, f"onPointerDown must hit-test {what} ({token})"
        seen.append((idx, token))
    assert seen == sorted(seen), (
        "onPointerDown order must be action-strip, waypoint, port, segment, "
        f"node, wire -- handles before the things they sit on top of. Got: "
        f"{[t for _, t in seen]}")


def test_live_page_binds_the_double_click_bend_gesture():
    src = _strip_comments(_read(_PAGES / "LivePage.tsx"))
    assert "onDoubleClick" in src, (
        "LivePage must bind onDoubleClick -- double-click is the explicit "
        "add/remove-a-bend gesture on the workflows canvas and must be the "
        "same gesture here")
    canvas = src[src.index("<canvas"):]
    canvas = canvas[:canvas.index("/>")]
    assert "onDoubleClick" in canvas, (
        "the handler must actually be bound on the <canvas> element")


# ---------------------------------------------------------------------------
# AC-7 / likely_misfire: simplifyPath must NOT flatten the live board's
# deliberate obstacle-avoidance routing. SIMPLIFY_EPS is 12 and
# LANE_STEP is 10, so an unconditional simplify pass snaps a one-lane
# avoidance jog flat -- on a board where every other card is an obstacle.
# ---------------------------------------------------------------------------

def test_the_simplify_pass_is_gated_not_unconditional():
    src = _shared_src()
    body = _body(src, "route(")
    assert "simplifyPath(" in body, (
        "the shared route() must still run the joined polyline through "
        "simplifyPath, or a bent wire draws the raw staircase")
    assert "simplifyUnbentRoutes" in body, (
        "route() calls simplifyPath unconditionally -- an unbent live wire "
        "would lose its obstacle-avoidance jog (SIMPLIFY_EPS 12 > "
        "LANE_STEP 10). The pass must be gated on the wire carrying bends "
        "or on the surface opting in")


def test_each_surface_declares_its_own_simplify_policy():
    live = _strip_comments(_read(_LIVE / "graphState.ts"))
    workflows = _strip_comments(_read(_LIVE / "workflowGraph.ts"))
    assert re.search(r"simplifyUnbentRoutes\s*:\s*false", live), (
        "the live board must opt OUT of simplifying unbent routes -- this "
        "is the named misfire, and it is what keeps an untouched live wire "
        "rendering exactly as it does today")
    assert re.search(r"simplifyUnbentRoutes\s*:\s*true", workflows), (
        "the workflows canvas must keep simplifying every route, which is "
        "its behaviour today")


# ---------------------------------------------------------------------------
# AC-6 / stop_if #3: persistence is per SURFACE and per PROJECT. The
# surface name is part of the key by construction, so a live edit can
# never move a workflows wire.
# ---------------------------------------------------------------------------

def test_wire_edit_keys_are_per_surface_and_per_project():
    live = _strip_comments(_read(_PAGES / "LivePage.tsx"))
    workflows = _strip_comments(_read(_PAGES / "WorkflowsPage.tsx"))
    for pattern, page_name, src in (
        (r"`prism\.live\.ports\.\$\{project\}`", "LivePage", live),
        (r"`prism\.live\.waypoints\.\$\{project\}`", "LivePage", live),
        (r"`prism\.workflows\.ports\.\$\{project\}`", "WorkflowsPage", workflows),
        (r"`prism\.workflows\.waypoints\.\$\{project\}`", "WorkflowsPage", workflows),
    ):
        assert re.search(pattern, src), (
            f"{page_name} must own the key {pattern} -- wire edits persist "
            "per surface AND per project")
    assert "prism.workflows." not in live, (
        "LivePage must never touch a prism.workflows.* key (stop_if #3: "
        "persistence keys colliding across surfaces)")
    assert "prism.live." not in workflows, (
        "WorkflowsPage must never touch a prism.live.* key (stop_if #3)")


def test_live_page_hydrates_persists_and_resets_wire_waypoints():
    src = _strip_comments(_read(_PAGES / "LivePage.tsx"))
    assert "waypointsKey" in src, "LivePage needs a waypoints storage key helper"
    reset = src[src.index("const handleResetLayout"):src.index("const onPointerDown")]
    assert "waypointsKey" in reset, (
        "'reset layout' must drop the persisted waypoints too -- a reset "
        "that left wires bent would only half-work (AC-8)")


# ---------------------------------------------------------------------------
# stop_if #2: graphState.ts is INTEGRATED, not rewritten. Every public
# port name it exposes today survives, and wireEndpointsFor stays the one
# geometry source draw.ts and packet spawning both resolve through.
# ---------------------------------------------------------------------------

def test_graph_state_is_integrated_not_rewritten():
    src = _strip_comments(_read(_LIVE / "graphState.ts"))
    for member in (
        "portOverrides", "setPortOverride", "setPortFromWorld",
        "hydratePortOverrides", "serializePortOverrides", "portAtWorld",
        "draggingPortId", "wireEndpointsFor",
    ):
        assert member in src, (
            f"GraphState must still expose {member} -- this task integrates "
            "the shared layer into graphState, it does not rewrite it")
    draw = _strip_comments(_read(_LIVE / "draw.ts"))
    assert "state.wireEndpointsFor(" in draw, (
        "draw.ts must still resolve wire geometry through "
        "GraphState.wireEndpointsFor, the single router entry point")
    assert "routeOrthogonal(" not in draw, (
        "draw.ts must never call routeOrthogonal directly -- that duplicate "
        "call site is what let the drawn wire and the packet path diverge")


def test_live_wire_edits_persist_through_the_shared_editor():
    """AC-6 on the state side: the live board serializes and rehydrates
    waypoints the same way it already does ports, so a bend survives a
    reload."""
    src = _strip_comments(_read(_LIVE / "graphState.ts"))
    for member in ("serializeWireWaypoints", "hydrateWireWaypoints"):
        assert member in src, (
            f"GraphState must expose {member} so LivePage can persist and "
            "restore bends per project")
