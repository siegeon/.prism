"""Task f506ece4: a per-project "Workflows" section on the live canvas.

In PRISM a workflow IS a bot — an FSM that agentically interacts with the
conductor's FSM. Both already exist (models/workflow.py WORKFLOW_STEPS,
services/context_builder.py ROLE_CARDS), so this section is a VIEW
assembled from existing entities: no new tables, no parallel FSM model.

Convention (test_conductor_page_animated_cleanup_ui.py): the PRISM SPA has
no JS test runner, so UI ACs are pinned by asserting the ACTUAL TSX source.
Every assertion strips comments first and parses the enclosing structure
rather than a fixed character window — an explanatory comment above an
element must never satisfy an assertion (lesson: e139295d).

The simplification rider is pinned here too. lib/workflowChips.ts used to
carry WORKFLOW_STEPS_ORDERED, a hand-maintained duplicate of the backend
WORKFLOW_STEPS (plus a dead `WorkflowStep` string-union listing the same
ids a second time). GET /api/workflows is now the single source of that
ordering, so the duplicate must be GONE — not merely unused, and not kept
as a static fallback, which is just the duplication with a longer fuse.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_WEB = _SERVICE_ROOT / "prism_service" / "web" / "src"


def _strip_comments(src: str) -> str:
    # Line comments FIRST: some line comments contain a literal "/*" (path
    # wildcards like "/settings/*"), which a block-comment pass run first
    # would misread as a real opener and eat the rest of the file.
    src = re.sub(r"//[^\n]*", "", src)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    return src


def _read(*parts: str) -> str:
    path = _WEB.joinpath(*parts)
    assert path.exists(), f"expected {path} to exist"
    return _strip_comments(path.read_text(encoding="utf-8"))


def _function_body(src: str, signature: str) -> str:
    """Everything between `signature`'s opening `{` and its matching
    closing `}`, counted by brace depth — never a fixed character window,
    which a comment above the code has been shown to push the real guard
    out of. `_read` has already stripped comments. Added by task be7a5d2d,
    when several assertions here moved from a whole-file substring match
    onto a specific function in the shared wireEditing.ts."""
    idx = src.find(signature)
    assert idx != -1, f"{signature!r} not found in source"
    brace_start = src.find("{", idx)
    assert brace_start != -1, f"no body opened after {signature!r}"
    depth = 0
    for i in range(brace_start, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[brace_start:i + 1]
    raise AssertionError(f"unbalanced braces after {signature!r}")


def _backend_step_ids() -> list[str]:
    from prism_service.models.workflow import WORKFLOW_STEPS

    return [s["id"] for s in WORKFLOW_STEPS]


def _activity_items() -> str:
    """The Activity section's items array — the same extraction
    test_inbox_hidden_ui.py uses, so both suites agree on what "in the nav"
    means."""
    src = _read("components", "Sidebar.tsx")
    m = re.search(r'label:\s*"Activity".*?items:\s*\[(.*?)\n\s*\],', src, re.DOTALL)
    assert m, "could not locate the Activity section's items array in Sidebar.tsx"
    return m.group(1)


# --------------------------------------------------------------------------
# Reachability: a section nobody can navigate to does not exist.
# --------------------------------------------------------------------------

def test_a_person_can_reach_workflows_from_the_nav():
    """A RENDERED nav item inside the Activity section — things-in-motion,
    next to Conductor and Live, not a comment promising one."""
    items_src = _activity_items()
    item = re.search(
        r'\{[^{}]*to:\s*"/workflows"[^{}]*label:\s*"Workflows"[^{}]*icon:\s*(\w+)[^{}]*\}',
        items_src,
    )
    assert item, (
        "no {to: \"/workflows\", label: \"Workflows\", icon: ...} item in the "
        f"Sidebar Activity items array: {items_src!r}")
    icon = item.group(1)
    assert icon != "Workflow", (
        "the lucide `Workflow` icon already belongs to Conductor — two nav "
        "rows with the same glyph are unreadable")
    sidebar = _read("components", "Sidebar.tsx")
    assert re.search(rf'\b{icon}\b[^;]*?from\s+"lucide-react"', sidebar, re.DOTALL), (
        f"the Workflows nav icon {icon} is not imported from lucide-react")


def test_the_nav_item_does_not_disturb_its_siblings():
    """Adding a section must not reorder the Activity rows that were
    already there (test_inbox_hidden_ui.py pins the same invariant)."""
    items_src = _activity_items()
    positions = [
        m.start()
        for m in re.finditer(r'to:\s*"(/tasks|/conductor|/live|/retrievals)"', items_src)
    ]
    assert len(positions) == 4, f"expected all 4 prior siblings: {items_src!r}"
    assert positions == sorted(positions), "Activity siblings must stay in order"


def test_the_route_mounts_the_workflows_page():
    """/workflows resolves to a real page, lazily like every other route."""
    app = _read("App.tsx")
    route = re.search(
        r'<Route\s+path="/workflows"\s+element=\{([^}]*)\}\s*/>', app)
    assert route, 'no <Route path="/workflows" .../> element found in App.tsx'
    assert "WorkflowsPage" in route.group(1), (
        f"the /workflows route must mount WorkflowsPage: {route.group(1)!r}")
    assert re.search(
        r'const\s+WorkflowsPage\s*=\s*lazyRoute\("workflows",\s*\(\)\s*=>\s*import\("@/pages/WorkflowsPage"\)\);',
        app,
    ), "WorkflowsPage must be a self-healing lazy route chunk"
    assert "window.location.reload()" in app


def test_the_page_is_a_real_project_scoped_page():
    """Full-bleed in the shared route chrome and scoped to the selected
    project — a per-project section that ignores the selector is a lie."""
    page = _read("pages", "WorkflowsPage.tsx")
    assert re.search(r'export\s+default\s+function\s+WorkflowsPage', page), (
        "WorkflowsPage.tsx must default-export the page component")
    assert "<Page>" not in page, (
        "the graph owns the complete route content area; Page padding would "
        "reintroduce the gutter around the canvas")
    assert re.search(r'<div className="relative flex h-full[^\"]*w-full', page)
    assert "useProject()" in page, "the page must read the selected project"
    # SUPERSEDED (this slice): this used to require the raw
    # `/api/workflows?project=${encodeURIComponent(project)}` literal in the
    # page. The endpoint ended up with TWO consumers on different clocks —
    # this page's per-project occupancy poll and the conductor rail's
    # cached one-shot — so the URL is owned once by lib/useWorkflowDef and
    # both call through it. The invariant is unchanged (the page's read is
    # scoped to the selected project); only WHERE the scoping is spelled
    # moved, and the literal is still pinned, on its owner, in
    # test_the_step_ordering_is_sourced_from_the_api below.
    assert re.search(r'fetchWorkflowDef\(project\)', page), (
        "the page must scope its fetch to the selected project via the "
        "shared fetcher")
    assert "<canvas" in page, "the Workflows section is a canvas surface"


# --------------------------------------------------------------------------
# Reuse: the section rides the EXISTING live canvas primitives.
# --------------------------------------------------------------------------

def test_the_canvas_reuses_the_live_wire_and_packet_primitives():
    """Maximum reuse, no new entities: routing, wire drawing and in-transit
    packets come from live/wires + live/packets, never a second copy that
    can drift from the /live board's visual grammar."""
    graph = _read("live", "workflowGraph.ts")

    assert re.search(r'from\s+"\./wires"', graph), (
        "live/workflowGraph.ts must import the shared wire primitives")
    assert re.search(r'from\s+"\./packets"', graph), (
        "live/workflowGraph.ts must import the shared packet primitives")
    # SUPERSEDED (increment 2): these symbols used to be pinned to
    # workflowGraph.ts alone. The wire-editing slice moved routing one
    # module deeper (live/wireEditing.ts owns the waypoint leg builder,
    # and workflowGraph calls it), so the canvas SURFACE is now two files.
    # RE-ANCHORED again by task be7a5d2d: that module is no longer named
    # for this canvas, because /live consumes it too.
    # The invariant is unchanged — the live primitives are reused, not
    # forked — so it is checked against the surface rather than one file.
    surface = graph + _read("live", "wireEditing.ts")
    for symbol in ("routeOrthogonal", "drawWire", "spawnPacket",
                   "stepPackets", "drawPackets"):
        assert symbol in surface, (
            f"{symbol} is not used by the workflows canvas — the section is "
            "supposed to REUSE the live board's grammar, not re-invent it")


def test_the_workflows_canvas_is_not_a_fork_of_the_live_primitives():
    """A copied implementation satisfies an import check by accident; this
    catches the actual failure mode — a second definition of the same
    routine living beside the shared one."""
    graph = _read("live", "workflowGraph.ts")
    page = _read("pages", "WorkflowsPage.tsx")
    for owned_elsewhere in ("routeOrthogonal", "drawWire", "spawnPacket",
                            "stepPackets", "drawPackets", "portPoint",
                            "autoPort"):
        for name, src in (("workflowGraph.ts", graph), ("WorkflowsPage.tsx", page)):
            assert not re.search(
                rf'(export\s+)?function\s+{owned_elsewhere}\s*\(', src), (
                f"{name} re-defines {owned_elsewhere} instead of importing it "
                "from live/wires or live/packets")


def test_the_page_drives_the_shared_canvas_module():
    """The page owns the DOM canvas and the rAF loop (LivePage's shape);
    all geometry/drawing lives in the live/ module."""
    page = _read("pages", "WorkflowsPage.tsx")
    assert re.search(r'from\s+"@/live/workflowGraph"', page), (
        "WorkflowsPage must drive the shared live/workflowGraph module")
    assert "requestAnimationFrame" in page, "the canvas needs a rAF loop"
    assert "ResizeObserver" in page, "the canvas must resize with its container"


def test_positions_are_remembered_per_project():
    """Dragged nodes survive a reload, scoped per project — the same
    convention /live uses (prism.live.positions.<project>)."""
    page = _read("pages", "WorkflowsPage.tsx")
    key = re.search(r'`prism\.workflows\.positions\.\$\{project\}`', page)
    assert key, (
        "WorkflowsPage must persist node positions under a per-project "
        "localStorage key (prism.workflows.positions.<project>)")
    assert "localStorage" in page


# --------------------------------------------------------------------------
# Simplification rider: ONE source of the step ordering.
# --------------------------------------------------------------------------

def test_workflow_chips_no_longer_hardcodes_the_step_list():
    """The duplicate of the backend FSM is GONE from lib/workflowChips.ts —
    every step id literal with it. Keeping the array "just as a fallback"
    is the same duplication with a longer fuse: it drifts silently, and the
    only symptom is a rail that disagrees with the conductor."""
    chips = _read("lib", "workflowChips.ts")

    assert "WORKFLOW_STEPS_ORDERED" not in chips, (
        "lib/workflowChips.ts still declares WORKFLOW_STEPS_ORDERED — the "
        "ordered step list now comes from GET /api/workflows")
    leaked = [sid for sid in _backend_step_ids() if f'"{sid}"' in chips]
    assert not leaked, (
        f"lib/workflowChips.ts still hardcodes backend step ids {leaked} — "
        "a second copy of the FSM that nothing keeps in sync")


def test_the_step_ordering_is_sourced_from_the_api():
    """One hook owns the fetch, so there is exactly one place the ordering
    can come from."""
    hook = _read("lib", "useWorkflowDef.ts")
    assert re.search(
        r'/api/workflows\?project=\$\{encodeURIComponent\(project\)\}', hook), (
        "lib/useWorkflowDef.ts must fetch the definition from "
        "GET /api/workflows, scoped to the requested project")
    assert re.search(r'export\s+function\s+useWorkflowSteps\s*\(', hook), (
        "useWorkflowSteps() is the hook the conductor rail consumes")
    assert re.search(r'export\s+function\s+fetchWorkflowDef\s*\(', hook), (
        "one exported fetcher owns the endpoint — the rail and the canvas "
        "must not each spell the URL")


def test_the_conductor_rail_consumes_the_hook():
    """StepRail and SdlcProgress render the API-sourced ordering. Their
    downstream contracts (a step's `type`, `steps.length`, `curIdx`) are
    unchanged — only where the list COMES FROM moves."""
    for rel in (("components", "conductor", "StepRail.tsx"),
                ("components", "conductor", "SdlcProgress.tsx")):
        src = _read(*rel)
        name = rel[-1]
        assert "WORKFLOW_STEPS_ORDERED" not in src, (
            f"{name} still reads the retired hardcoded list")
        assert re.search(r'const\s+steps\s*=\s*useWorkflowSteps\(\)', src), (
            f"{name} must take its ordered steps from useWorkflowSteps()")
        assert re.search(r'useWorkflowSteps[^;]*from\s+"@/lib/useWorkflowDef"',
                         src, re.DOTALL), (
            f"{name} must import the hook from lib/useWorkflowDef")


def test_the_rail_still_gets_intake_and_a_persona_per_step():
    """The rail's list is the FSM plus the synthetic leading `intake` row
    (the pre-conductor state, which is NOT a backend step). Both the intake
    entry and each step's persona must survive the move to the API, or
    StepRail loses its "who owns this row" column and SdlcProgress loses
    its gate-vs-agent caption guard."""
    hook = _read("lib", "useWorkflowDef.ts")
    assert '"intake"' in hook, (
        "the synthetic intake row must still lead the rail")
    assert "persona" in hook and "type" in hook, (
        "the rail's step shape is {id, persona, type} — consumers key off "
        "both persona (owner column) and type (gate caption guard)")


# --------------------------------------------------------------------------
# Increment 2 (owner at green_gate review): "The wires need to do a better
# job — I should be able to click on them like draw.io / mxGraph so that I
# can move this and help ensure their path is the way I want it to be."
#
# Direct manipulation of a wire: select it, re-dock either endpoint port,
# and bend the middle with waypoints. All CLIENT state — no new entity, no
# backend change — persisted per project exactly like node positions.
# --------------------------------------------------------------------------

def _wire_editor() -> str:
    """The wire-interaction module. Split out of workflowGraph.ts to keep
    that file from becoming the very god-module the /live board's
    graphState.ts already is.

    RE-ANCHORED by task be7a5d2d: renamed workflowWires.ts ->
    wireEditing.ts when the /live board became the second consumer. A
    module named for one canvas but imported by both is the name that
    invites the drift back; the contract below is otherwise unchanged."""
    return _read("live", "wireEditing.ts")


def test_clicking_a_wire_selects_it():
    """A wire is a thing you can point at. Without a polyline hit-test the
    owner has nothing to grab, which is the whole complaint."""
    graph = _read("live", "workflowGraph.ts")
    editor = _wire_editor()

    assert re.search(r'\bwireAtWorld\s*\(', graph), (
        "workflowGraph.ts exposes no wireAtWorld hit-test — a wire cannot "
        "be clicked")
    assert re.search(r'export\s+function\s+nearestOnPolyline\s*\(', editor), (
        "the polyline hit-test must be a real distance-to-segment helper "
        "in live/wireEditing.ts, not an approximation of the endpoints")
    assert re.search(r'\bselected\b', graph) or re.search(r'\bselected\b', editor), (
        "nothing tracks which wire is selected")


def test_a_selected_wire_reads_as_selected():
    """Selection the owner can SEE — the live stroke, so a selected wire is
    unmistakable against the dim idle ones."""
    graph = _read("live", "workflowGraph.ts")
    # RE-ANCHORED by task be7a5d2d: drawWorkflows no longer calls drawWire
    # itself. It hands the selection state to the shared drawEditableWire,
    # which /live's draw.ts also paints through — one renderer, so the two
    # canvases cannot drift into two selections. The invariant is
    # unchanged: a selected wire must be drawn as selected.
    assert re.search(r'drawEditableWire\(', graph), (
        "drawWorkflows must paint through the shared editable-wire renderer")
    assert re.search(r'selected[,:]', _strip_comments(graph)), (
        "drawWorkflows must pass the SELECTED state through to it — "
        "a selection nobody can see is not a selection")


def test_endpoint_ports_can_be_re_docked_and_survive_a_reload():
    """Drag either end of a wire to another side of its node. Mirrors the
    live board's scheme (graphState.ts portOverrides keyed `<wire>:from` /
    `<wire>:to`) rather than inventing a second one."""
    graph = _read("live", "workflowGraph.ts")
    editor = _wire_editor()
    page = _read("pages", "WorkflowsPage.tsx")

    assert re.search(r'\bportAtWorld\s*\(', graph), (
        "no portAtWorld hit-test — the endpoint dots are not grabbable")
    assert "portFromWorld" in editor, (
        "re-docking must resolve the new side through live/wires.ts's "
        "portFromWorld, never a hand-rolled side/offset calculation")
    assert re.search(r'`prism\.workflows\.ports\.\$\{project\}`', page), (
        "port placements must persist per project under "
        "prism.workflows.ports.<project>")


def test_waypoints_can_be_inserted_moved_and_removed():
    """The genuinely new affordance: bend a wire's middle. Insert, move and
    remove must all exist or the owner can create a bend and never undo it."""
    editor = _wire_editor()
    graph = _read("live", "workflowGraph.ts")
    page = _read("pages", "WorkflowsPage.tsx")

    for fn in ("insertWaypoint", "moveWaypoint", "removeWaypoint"):
        assert re.search(rf'\b{fn}\s*\(', editor), (
            f"live/wireEditing.ts has no {fn} — a waypoint the owner "
            "cannot undo is a trap, not an affordance")
    assert re.search(r'\bwaypointAtWorld\s*\(', graph), (
        "no waypointAtWorld hit-test — a placed waypoint cannot be grabbed")
    assert re.search(r'onDoubleClick', page), (
        "double-click is the insert/remove gesture; the canvas binds no "
        "double-click handler")
    assert re.search(r'`prism\.workflows\.waypoints\.\$\{project\}`', page), (
        "waypoints must persist per project under "
        "prism.workflows.waypoints.<project>")


def test_waypoint_paths_still_route_through_the_one_orthogonal_router():
    """A bent wire is still ORTHOGONAL. Every hop — node to waypoint,
    waypoint to waypoint, waypoint to node — goes through the SAME
    routeOrthogonal the straight wires use. A second hand-rolled path
    builder is exactly how the two canvases would drift into two
    grammars."""
    editor = _wire_editor()

    assert re.search(r'routeOrthogonal[^;]*from\s+"\./wires"', editor, re.DOTALL), (
        "live/wireEditing.ts must import routeOrthogonal from ./wires")
    assert re.search(r'routeOrthogonal\s*\(', editor), (
        "the waypoint path builder never calls routeOrthogonal — it is "
        "constructing its own polyline")
    for owned_elsewhere in ("routeOrthogonal", "portPoint", "portFromWorld",
                            "autoPort", "drawWire"):
        assert not re.search(
            rf'(export\s+)?function\s+{owned_elsewhere}\s*\(', editor), (
            f"live/wireEditing.ts re-defines {owned_elsewhere} instead of "
            "importing it from live/wires")


def test_deselect_and_reset_clear_the_manual_wire_state():
    """Escape / clicking empty space lets go, and 'reset layout' drops
    ports and waypoints along with positions — otherwise the escape hatch
    only half-works and a wire stays bent with no way back."""
    page = _read("pages", "WorkflowsPage.tsx")
    editor = _wire_editor()

    assert re.search(r'(Escape|"Escape")', page), (
        "Escape must deselect the active wire")
    for key in ("prism\\.workflows\\.positions", "prism\\.workflows\\.ports",
                "prism\\.workflows\\.waypoints"):
        assert re.search(rf'removeItem\(\s*{key}|{key}', page), (
            f"reset layout must also forget {key}.<project>")
    assert re.search(r'\bclear\w*\s*\(', editor), (
        "the wire editor exposes no clear — reset layout has nothing to "
        "call to drop port/waypoint overrides")


# --------------------------------------------------------------------------
# Increment 3 (owner rejected the first cut at green_gate, ticket 53cc9bcc,
# screenshot of the Steward -> story_gate wire): "it's not working yet, the
# lines are all kinda messed up, and if the wires are active please make the
# whole thing orange."
# --------------------------------------------------------------------------

def test_routed_paths_are_simplified_instead_of_staircasing():
    """Routing per hop and concatenating is what mints staircases: every
    hop contributes its own stub-and-jog, so a couple of bends can stack a
    dozen 3px steps inside a 150px span. The fix is POST-PROCESSING the
    joined polyline — merge collinear runs, snap sub-threshold jogs onto
    one rail — never a second router."""
    editor = _wire_editor()
    graph = _read("live", "workflowGraph.ts")

    assert re.search(r'export\s+function\s+simplifyPath\s*\(', editor), (
        "live/wireEditing.ts exposes no simplifyPath — a chained path "
        "keeps every micro-jog the per-hop router emitted")
    # RE-ANCHORED by task be7a5d2d: route composition moved into the
    # shared WireInteraction, so the simplify call now lives there rather
    # than in workflowGraph. The invariant is unchanged AND strengthened:
    # the pass must still run on the joined polyline, and it is now gated
    # so it can never flatten the /live board's obstacle avoidance.
    interaction = _function_body(editor, "route(")
    assert re.search(r'simplifyPath\s*\(', interaction), (
        "WireInteraction.route must run the joined polyline through "
        "simplifyPath, or the canvas draws the raw staircase")
    assert "joinLegs(" in interaction, (
        "simplify must see the WHOLE path at once — per-hop cleanup is "
        "what leaves the staircase behind")
    assert re.search(r'wireEdits\.route\(', _strip_comments(graph)), (
        "workflowGraph.route must delegate to that one implementation")
    assert not re.search(r'(export\s+)?function\s+routeOrthogonal\s*\(', editor), (
        "simplification must stay POST-processing on routeOrthogonal's "
        "output — re-implementing the router is the thing this whole "
        "section is not allowed to do")


def test_a_dragged_bend_is_settled_before_it_is_saved():
    """Dragging must not mint micro-staircases, and a SAVED path must
    already be clean — persisting raw drag coordinates would reload the
    exact staircase the renderer just simplified away."""
    editor = _wire_editor()
    page = _read("pages", "WorkflowsPage.tsx")
    graph = _read("live", "workflowGraph.ts")

    assert re.search(r'simplifyWaypoints\s*\(', editor), (
        "the editor cannot settle stored bends onto clean rails")
    assert re.search(r'settleWire\s*\(', graph), (
        "workflowGraph exposes no settleWire for the page to call")
    settle = [m.start() for m in re.finditer(r'settleWire\s*\(', page)]
    persist = [m.start() for m in re.finditer(r'persistWires\s*\(\)', page)]
    assert settle and persist, (
        "the page must settle the wire AND persist it after an edit")
    assert min(settle) < max(persist), (
        "settleWire must run BEFORE the path is persisted, or the saved "
        "copy is the unsimplified one")


def test_an_active_wire_is_orange_end_to_end():
    """Owner: "if the wires are active please make the whole thing
    orange." Body, endpoint ports and bend handles all take the SAME
    selection token — a wire that is orange only at its handles is exactly
    the half-signal that got rejected."""
    # RE-ANCHORED by task be7a5d2d: the selected-wire paint moved into
    # wireEditing.ts's drawEditableWire, and this assertion moved with it
    # — strengthened, because the orange must now be identical on BOTH
    # canvases, which is the whole point of that ticket. The owner's rule
    # is untouched: body, ports and handles take ONE selection token.
    body = _function_body(_wire_editor(), "export function drawEditableWire")
    assert re.search(r'drawWire\([^)]*selColor', body), (
        "the selected wire's BODY still takes its color from wireColor — "
        "the stroke must be the selection hue too, not just the handles")
    assert re.search(r'selColor\s*=\s*w\.selected\s*\?\s*PALETTE\.selection', body), (
        "the one selection color must come from the PALETTE token the "
        "handles already use, never a second orange literal")
    assert re.search(r'portColor\s*=\s*selColor', body), (
        "the endpoint ports must reuse that same selection color")
    for consumer in ("workflowGraph.ts", "draw.ts"):
        src = _strip_comments(_read("live", consumer))
        assert "drawEditableWire(" in src, (
            f"live/{consumer} must reach the one orange through the shared "
            "renderer")
        assert "PALETTE.selection" not in src, (
            f"live/{consumer} must not carry a second selection colour")


def test_the_orange_stroke_goes_through_the_shared_wire_renderer():
    """Whole-stroke recoloring must be an OPTIONAL argument to the shared
    drawWire, not a local re-implementation of stroking a polyline — that
    is how the two canvases stay one grammar. The live board's own pins
    (width rule, no globalAlpha in the body) must survive untouched."""
    wires = _read("live", "wires.ts")

    assert re.search(r'export function drawWire\([^)]*color\?:\s*string', wires), (
        "drawWire takes no optional color override — the workflows canvas "
        "would have to fork it to draw a selected wire")
    assert "color ?? wireColor(" in wires, (
        "the override must FALL BACK to wireColor, so every existing "
        "caller keeps its exact current color")
    assert "ctx.lineWidth = live ? 3 : 2;" in wires, (
        "the live board's width rule must survive the added parameter")


# --------------------------------------------------------------------------
# Increment 4 (owner: "this is a great step thank you", plus refinements):
# drag the wire BODY, mint/retire anchors automatically, and stop wasting a
# whole band on a page title that duplicates the global header.
# --------------------------------------------------------------------------

def test_the_wire_body_itself_can_be_dragged():
    """Owner: "I can't drag the wire at all." Draw.io's core gesture is
    grabbing a SEGMENT and sliding it perpendicular to its own axis — a
    horizontal run moves vertically, a vertical run moves horizontally."""
    editor = _wire_editor()
    graph = _read("live", "workflowGraph.ts")
    page = _read("pages", "WorkflowsPage.tsx")

    assert re.search(r'\bgrabSegment\s*\(', editor), (
        "live/wireEditing.ts cannot grab a segment — there is nothing "
        "for a body drag to move")
    assert re.search(r'\bmoveSegment\s*\(', editor), (
        "a grabbed segment cannot be slid")
    assert re.search(r'\bsegmentAtWorld\s*\(', graph), (
        "workflowGraph exposes no segmentAtWorld — the page cannot tell "
        "WHICH run of the wire is under the cursor")
    assert re.search(r'"segment"', page), (
        "the page has no segment drag mode")


def test_dragging_a_segment_mints_its_anchors_automatically():
    """Owner: stop making them "micromanage the need for the double-click
    anchors". A body drag materializes the anchors the new shape needs by
    itself; double-click stays as the explicit manual path."""
    editor = _wire_editor()
    page = _read("pages", "WorkflowsPage.tsx")

    assert "grabSegment" in editor, "grabSegment does not exist yet"
    grab = editor.split("grabSegment", 1)[1][:1200]
    assert "waypoints.set" in grab, (
        "grabSegment must WRITE the materialized bends — otherwise the "
        "drag has no anchors to move and the owner is back to "
        "double-clicking first")
    assert re.search(r'onDoubleClick', page), (
        "double-click must REMAIN the explicit add/remove path")


def test_a_segment_drag_is_settled_like_every_other_edit():
    """Anchors a drag makes redundant must retire themselves — the same
    simplify/settle pass, so a body drag can't leave junk anchors behind
    that only a manual double-click could clear."""
    page = _read("pages", "WorkflowsPage.tsx")
    graph = _read("live", "workflowGraph.ts")

    # RE-ANCHORED by task be7a5d2d: same move as
    # test_routed_paths_are_simplified_instead_of_staircasing — the
    # simplify pass lives in the shared WireInteraction.route now, and
    # workflowGraph delegates to it.
    assert re.search(r'simplifyPath\s*\(', _function_body(_wire_editor(), "route(")), (
        "the drawn path must stay simplified while a segment is dragged")
    assert re.search(r'wireEdits\.route\(', _strip_comments(graph)), (
        "workflowGraph must resolve that path through the shared layer")
    settle = [m.start() for m in re.finditer(r'settleWire\s*\(', page)]
    persist = [m.start() for m in re.finditer(r'persistWires\s*\(\)', page)]
    assert settle and persist and min(settle) < max(persist), (
        "a segment drag must settle before it persists, like the other "
        "wire edits")


def test_the_section_title_lives_in_the_global_header():
    """The page-level title row duplicated the global header one band
    below it. The header now names the active section, and it derives that
    name from the SAME nav items the sidebar renders — PageHeader's own
    TITLES map had already drifted (it said "Tasks" while the nav said
    "Work")."""
    header = _read("components", "PageHeader.tsx")
    sidebar = _read("components", "Sidebar.tsx")

    assert re.search(r'export\s+function\s+sectionTitleFor\s*\(', sidebar), (
        "Sidebar must export the one section-name resolver, derived from "
        "its own items array")
    assert re.search(r'sectionTitleFor[^;]*from\s+"@/components/Sidebar"',
                     header, re.DOTALL), (
        "PageHeader must consume that resolver, not keep a second map")
    assert not re.search(r'const\s+TITLES\s*:', header), (
        "PageHeader still declares its own TITLES map — that is the "
        "duplicate source that drifted in the first place")


def test_the_workflows_page_no_longer_repeats_its_own_title():
    """The reclaimed band goes to the canvas."""
    page = _read("pages", "WorkflowsPage.tsx")

    assert "<h1" not in page, (
        "WorkflowsPage still renders its own <h1> — that is the wasted "
        "band the owner called out, now that the global header names the "
        "section")
    assert "<canvas" in page, "the canvas must survive the title removal"


def test_workflow_directory_selects_the_definition_drawn_on_the_right():
    page = _read("pages", "WorkflowsPage.tsx")

    assert 'aria-label="Workflow directory"' in page
    assert 'aria-label="Available workflows"' in page
    assert "Collapse workflow directory" in page
    assert "Expand workflow directory" in page
    # The directory nests a bot's own FSM/Behavior entries under their
    # parent (progressive disclosure) -- top level is every catalog entry
    # WITHOUT a parent_id, filtered before mapping.
    assert re.search(r'workflows\.filter\(\(workflow\) => !workflow\.parent_id\)\.map\(\(workflow\)', page)
    assert re.search(r'onClick=\{\(\) => selectWorkflow\(workflow\)\}', page)
    assert re.search(
        r'graphRef\.current\.setDef\(workflowForGraph\(workflow\)\)', page
    ), (
        "selection must replace the graph definition, not only style a row")


def test_conductor_validation_node_opens_build_and_test_workflow():
    page = _read("pages", "WorkflowsPage.tsx")
    data = _read("lib", "useWorkflowDef.ts")
    api = (_SERVICE_ROOT / "prism_service" / "api" / "workflows.py").read_text(
        encoding="utf-8",
    )

    assert "linked_workflow_id?: string | null" in data
    assert '"validation" if step["id"] == "verify_green_state"' in api
    assert "const linkedStep = selectedWorkflow?.steps.find(" in page
    assert "const linkedWorkflowId = linkedStep?.linked_workflow_id" in page
    assert "workflow.id === linkedWorkflowId" in page
    assert "selectWorkflow(linkedWorkflow, [...workflowPath, {" in page
    assert 'linked_workflow_id: step.linked_workflow_id ?? "validation"' in page
    graph = _read("live", "workflowGraph.ts")
    assert 'linkedWorkflowLabel ? "⌄" : "↗"' in graph
    assert '`${linkedWorkflowLabel} workflow`' in graph
    assert "label: linkedWorkflowLabel ?? title(s.id)" in graph
    assert "setWorkflowPath(path)" in page
    assert "returnToWorkflowOrigin" in page
    assert 'aria-label="Workflow breadcrumb"' in page
    assert "workflowPath.map((entry, index)" in page
    assert "linked_workflow_step_count: linked.steps.length" in page


def test_workflow_directory_uses_sidebar_menu_grammar_and_graph_is_full_bleed():
    page = _read("pages", "WorkflowsPage.tsx")

    assert "DIRECTORY_DEFAULT_PX = 240" in page, (
        "open directory must initially align to Sidebar width")
    for token in ("--nav-bg", "--nav-line", "--nav-text",
                  "--nav-active-bg", "--nav-active-text", "--nav-hover"):
        assert token in page, f"workflow menu must use Sidebar token {token}"
    assert 'className="py-3" aria-label="Available workflows"' in page
    assert "rounded-md border" not in page, (
        "the graph split must not be presented as a padded card")
    assert "<Page>" not in page and "</Page>" not in page, (
        "the shared Page adds a 32px gutter; workflows is a full-bleed surface")
    assert 'relative flex h-full min-h-[420px] w-full overflow-hidden' in page


def test_workflow_directory_divider_is_resizable_and_accessible():
    page = _read("pages", "WorkflowsPage.tsx")

    assert 'role="separator"' in page
    assert 'aria-label="Resize workflow directory"' in page
    assert 'aria-orientation="vertical"' in page
    assert "cursor-col-resize" in page
    assert "setPointerCapture" in page and "releasePointerCapture" in page
    assert "onPointerMove={onDirectoryResizeMove}" in page
    assert "onKeyDown={onDirectoryResizeKey}" in page
    assert 'ev.key === "ArrowLeft"' in page
    assert 'ev.key === "ArrowRight"' in page
    assert "DIRECTORY_MIN_PX = 180" in page
    assert "DIRECTORY_MAX_PX = 480" in page
    assert "prism.workflows.directory.width" in page


def test_workflow_graph_is_an_operational_state_machine_with_drill_in():
    page = _read("pages", "WorkflowsPage.tsx")
    graph = _read("live", "workflowGraph.ts")

    assert 'id: "__start__"' in graph and 'id: "__complete__"' in graph
    assert 'linkedWorkflowLabel' in graph and 'i + 1 < steps.length' in graph
    assert "setSelectedNodeId(node.id)" in page
    assert "setStateDetailsOrigin" in page
    assert "onClick={onCanvasClick}" in page
    assert 'aria-label="State details"' in page
    assert 'n.actionLabel ?? "↗"' in graph
    assert 'aria-label="Close state details"' in page
    assert "Child transition" not in page and "Technical metadata" in page
    assert 'aria-label="Step behavior"' in page
    assert '<div className="text-sm">' in page
    assert 'space-y-3 py-3 text-sm' not in page
    assert 'space-y-3 px-4 py-3 text-sm' not in page
    assert '>Behavior</div>' not in page
    assert "Avg duration" in page and "Step behavior" in page
    assert 'aria-label="Step script"' in page
    assert 'import Editor from "@monaco-editor/react"' in page
    assert 'h-[max(420px,calc(100vh-470px))]' in page
    assert 'Read only' in page
    assert 'className="absolute bottom-5 left-5 right-5 top-5' not in page
    assert 'value={selectedStep.script_source}' in page
    assert '>Behavior</span>' in page
    assert '<header className="border-b border-[color:var(--border-default)] px-4 py-4">' in page
    assert '["Script", selectedStep.script_path || "Embedded"]' in page
    assert "grid max-h-64 grid-cols-2" in page
    assert "overflow-auto" in page
    assert "xl:grid-cols-3" in page
    for label in ("Input", "Success", "Output"):
        assert f'["{label}",' in page
    assert 'selectedStep?.purpose' in page
    assert 'selectedStep.command' in page
    assert '"Definition only"' in page
    assert '"Run workflow"' in page
    assert "Execute this project's typed scripted workflow" in page
    assert "startWorkflowRun(project, selectedWorkflow.id)" in page
    assert "requestWorkflowFix" in page
    assert "Ask PRISM agent to fix" in page
    assert "fetchWorkflowRun(started.instanceId)" in page
    assert "selectedScriptFrame" in page
    assert 'border-emerald-500/70' in page
    assert 'border-red-500/70' in page
    for field in ("command", "working_directory", "runner", "timeout_seconds", "depends_on"):
        assert f"selectedStep.{field}" in page
    assert "setTestStep(-1)" in page
    assert "Testing ·" in page and "Flow complete" in page
    assert 'graphWorkflow.id === "validation" ? { ...graphWorkflow, bots: [] }' in page
    assert "sendTransition(from, to)" in page
    assert "elapsedSeconds / average" in page
    assert "Math.min(0.98" in page
    assert "activeProgress" in graph
    assert "ctx.fillRect(x + 1, y + 1, w - 2, 3)" in graph
    assert "ctx.fillRect(x + 1, y + 1, fillWidth, 3)" in graph
    assert "fillWidth, h - 21" not in graph
    assert "drawTransitionLabel(ctx, wire.label" in graph
    assert "sub: gate ?" in graph and "sub: s.validation" not in graph
    assert "elapsedSeconds <= active.averageSeconds" in graph
    assert "if (n.childCount)" in graph
    assert "shortDuration(active.elapsedSeconds)" in graph
    assert "shortDuration(active.averageSeconds)" in graph
    assert "fetchActiveWorkflowRun(project, selectedWorkflow.id)" in page
    assert re.search(r'sendTransition\(source: string, target: string\)', graph)
    assert "spawnPacket(source, target, false, pts)" in graph


def test_dev_bundle_changes_reload_an_open_prism_tab():
    version = _read("lib", "version.ts")

    assert "web_build?: string" in version
    assert "function startDevBundleWatch(" in version
    assert "setInterval(poll, 2000)" in version
    assert re.search(r'r\.web_build\s*!==\s*initialBuild', version)
    assert "window.location.reload()" in version


def test_workflow_connection_interrupt_is_friendly_and_self_healing():
    page = _read("pages", "WorkflowsPage.tsx")
    header = _read("components", "PageHeader.tsx")

    assert 'new CustomEvent("prism:connection-state"' in page
    assert 'window.addEventListener("prism:connection-state"' in header
    assert '"Connection interrupted"' in header
    assert "reconnecting automatically · attempt" in header
    assert 'role={connection.interrupted ? "status"' in header
    assert "setConnectionInterrupted(false)" in page
    assert "setWorkflowRunError(null)" in page
    assert "RECONNECT_MIN_MS * 2 ** (failures - 1)" in page
    assert "window.setTimeout(load, delay)" in page
    assert "setWorkflows([])" not in page
    assert "e instanceof Error ? e.message" not in page


def test_finished_validation_reports_truth_and_exposes_step_output():
    page = _read("pages", "WorkflowsPage.tsx")

    assert 'truth.runtime?.status === "running"' in page
    assert '${selectedWorkflow?.name ?? "Workflow"} ${workflowRun.data.passed ? "passed" : "failed"}' in page
    assert "select a step for results" in page
    assert "Last run  •  Exit" in page
    assert "execution failed" in page
    assert "Recorded failure" in page
    assert "failureEvidence(selectedStepResult.output)" in page
    assert "View full output" not in page
    assert "Open step script" not in page
    assert "selectedStepResult.output" in page


def test_failed_step_exposes_copyable_run_and_step_identity():
    page = _read("pages", "WorkflowsPage.tsx")

    assert 'instance_id: workflowRun.id' in page
    assert 'step_id: selectedStep.id' in page
    assert "navigator.clipboard.writeText(JSON.stringify" in page
    assert '"Copy failure IDs"' in page
    assert "instance_id: {workflowRun?.id}" in page
    assert "step_id: {selectedStep.id}" in page


def test_state_details_zooms_from_the_clicked_node_into_the_full_graph_view():
    page = _read("pages", "WorkflowsPage.tsx")

    assert "setStateDetailsOrigin" in page
    assert 'transform: stateDetailsOpen ? "scale(1)" : "scale(0.04)"' in page
    assert "window.requestAnimationFrame(() => window.requestAnimationFrame" in page
    assert "transition-[transform,opacity]" in page
    assert "duration-[650ms]" in page
    assert "will-change-transform" in page
    assert 'className="absolute inset-x-0 bottom-10 top-0 z-30' in page
    assert 'if (event.propertyName !== "transform") return' in page
    assert 'selectedStep.execution === "scripted" && selectedStep.script_source' in page
    assert "setScriptOpen" not in page


def test_validation_discloses_async_brain_and_learning_state():
    page = _read("pages", "WorkflowsPage.tsx")

    assert "/api/staleness?project=" in page
    assert "/api/consolidation/workers?project=" in page
    assert 'worker.id === "memory_learning_pipeline"' in page
    assert "After validation · Brain" in page
    assert 'href="/consolidation"' in page
    assert "Validation emits deterministic evidence" in page


def test_workflow_run_has_a_segmented_page_level_progress_rail():
    page = _read("pages", "WorkflowsPage.tsx")
    data = _read("lib", "useWorkflowDef.ts")

    assert "RUN_RAIL_PILLS = 72" in page
    assert 'role="progressbar"' in page
    assert 'aria-label="Workflow run history"' in page
    assert "max-w-[10px]" in page
    assert "runPillTone(index)" in page
    assert "fetchWorkflowRunHistory(project, selectedWorkflow.id" in page
    assert "visibleRunHistory[pillIndex - historyOffset]" in page
    assert "const historyOffset = 0" in page
    assert '.filter((run) => ["Complete", "Terminated"].includes(run.status))' in page
    assert ".slice(0, RUN_RAIL_PILLS)" in page
    assert 'runs/history?project=' in data
    assert "bg-emerald-400" in page
    assert "bg-red-400" in page
    assert "bg-amber-300/60" in page
    assert 'h-10 border-t border-white/10 bg-[#08090b]' in page
    assert 'left-0 right-[118px] top-0' in page
    assert 'gap-[2px]' in page
    assert 'h-9 min-w-1 max-w-[10px] flex-1 rounded-none' in page
    assert 'absolute bottom-2 right-4' in page


def test_historical_run_can_be_replayed_on_the_graph_at_animation_frame_rate():
    page = _read("pages", "WorkflowsPage.tsx")
    graph = _read("live", "workflowGraph.ts")

    assert "replayHistoricalRun(run)" in page
    assert 'testModeRef.current = "replay"' in page
    assert "requestAnimationFrame(frame)" in page
    assert "elapsedMs / replayStepDurationRef.current" in page
    assert 'type ReplayEvent = NonNullable<WorkflowRun["timeline"]>[number]' in page
    assert "replayStepMs(event)" in page
    assert "replayGapMs(event" in page
    assert "replaySpanMs(event" in page
    assert "Replay ${REPLAY_SPEED}×" in page
    assert "run.data.definition?.steps" in page
    assert 'event.status !== "skipped"' in page
    assert 'result.status.replace("_", " ").toUpperCase()' in page
    assert 'workflowRun?.data.passed ? "PASSED" : "FAILED"' in page
    assert 'tone?: "active" | "success" | "failure" | "warning"' in graph
    assert 'active.tone === "failure"' in graph
    assert "graphRef.current.sendTransition(from, to)" in page
    assert "Ran {new Date(selectedHistoryRun.createTime).toLocaleString()}" in page
    assert "leaveHistoricalReplay" in page
    assert "active.label ??" in graph
    assert 'aria-label="Historical workflow overlay"' in page
    assert 'event.propertyName !== "bottom"' in page
    assert '"calc(100% - 32px)" : "0px"' in page
    assert "duration-[1500ms]" in page
    assert page.count('className="h-px flex-1 bg-white"') == 2
    assert 'style={{ bottom: historyOverlayOpen ? "100%" : "32px" }}' in page
    assert 'bg-[#111722]/95 transition-[bottom]' in page
    assert "beginHistoricalReplay(run)" in page
    assert "window.requestAnimationFrame(()" in page
    assert '"border-red-400"' in page
    assert "historyOverlayReady" in page


def test_failed_historical_replay_stops_on_the_failed_step():
    page = _read("pages", "WorkflowsPage.tsx")

    failure_guard = page.index('if (last.status !== "passed")')
    complete_transition = page.index('graphRef.current.sendTransition(last.step, "__complete__")')
    assert failure_guard < complete_transition
    assert "setReplayStoppedAt(last)" in page
    assert "__complete__: 0" in page[failure_guard:complete_transition]
    assert "setTestStep(failedStepIndex >= 0 ? failedStepIndex : null)" in page
    assert "Replay stopped ·" in page
    assert "replayFinishedAtFailure ? 1" in page
    assert 'replayFinishedAtFailure\n          ? "failure"' in page


def test_failed_script_marks_the_responsible_editor_line():
    page = _read("pages", "WorkflowsPage.tsx")
    css = _read("index.css")

    assert "failureMarkerLine(" in page
    assert "findLastIndex" in page
    assert 'glyphMarginClassName: "workflow-script-error-glyph"' in page
    assert "glyphMarginHoverMessage" not in page
    assert "MouseTargetType.GUTTER_GLYPH_MARGIN" in page
    assert "setScriptDiagnosticOpen((open) => !open)" in page
    assert 'role="alert"' in page
    assert "revealLineInCenterIfOutsideViewport" in page
    assert "glyphMargin: true" in page
    assert '.workflow-script-error-glyph::before' in css
    assert 'content: "!"' in css


def test_active_workflow_node_does_not_duplicate_progress_with_an_occupancy_badge():
    graph = _read("live", "workflowGraph.ts")

    assert "if (n.count > 0 && !active) drawOccupancy(ctx, n)" in graph


def test_replay_step_fill_spans_execution_and_inter_step_wait_without_a_pause():
    page = _read("pages", "WorkflowsPage.tsx")

    assert "REPLAY_MIN_STEP_MS = 1500" in page
    assert "REPLAY_MAX_STEP_MS = 5000" in page
    assert "replayStepMs(event) + replayGapMs(event, next)" in page
    assert "setReplayEventIndex((index) => (index ?? 0) + 1), duration" in page
