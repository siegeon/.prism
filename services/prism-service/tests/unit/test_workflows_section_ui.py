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
        r'const\s+WorkflowsPage\s*=\s*lazy\(\(\)\s*=>\s*import\("@/pages/WorkflowsPage"\)\);',
        app,
    ), "WorkflowsPage must be a lazy route chunk, like every other page"


def test_the_page_is_a_real_project_scoped_page():
    """Wrapped in the shared <Page> chrome and scoped to the selected
    project — a per-project section that ignores the selector is a lie."""
    page = _read("pages", "WorkflowsPage.tsx")
    assert re.search(r'export\s+default\s+function\s+WorkflowsPage', page), (
        "WorkflowsPage.tsx must default-export the page component")
    assert "<Page>" in page, "the page must render inside the shared <Page> chrome"
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
    # module deeper (live/workflowWires.ts owns the waypoint leg builder,
    # and workflowGraph calls it), so the canvas SURFACE is now two files.
    # The invariant is unchanged — the live primitives are reused, not
    # forked — so it is checked against the surface rather than one file.
    surface = graph + _read("live", "workflowWires.ts")
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
    graphState.ts already is."""
    return _read("live", "workflowWires.ts")


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
        "in live/workflowWires.ts, not an approximation of the endpoints")
    assert re.search(r'\bselected\b', graph) or re.search(r'\bselected\b', editor), (
        "nothing tracks which wire is selected")


def test_a_selected_wire_reads_as_selected():
    """Selection the owner can SEE — the live stroke, so a selected wire is
    unmistakable against the dim idle ones."""
    graph = _read("live", "workflowGraph.ts")
    assert re.search(r'drawWire\([^)]*(isSel|selected)', graph) or re.search(
        r'(isSel|selected)[^;\n]*drawWire', graph), (
        "drawWorkflows must draw the SELECTED wire with the live stroke — "
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
            f"live/workflowWires.ts has no {fn} — a waypoint the owner "
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
        "live/workflowWires.ts must import routeOrthogonal from ./wires")
    assert re.search(r'routeOrthogonal\s*\(', editor), (
        "the waypoint path builder never calls routeOrthogonal — it is "
        "constructing its own polyline")
    for owned_elsewhere in ("routeOrthogonal", "portPoint", "portFromWorld",
                            "autoPort", "drawWire"):
        assert not re.search(
            rf'(export\s+)?function\s+{owned_elsewhere}\s*\(', editor), (
            f"live/workflowWires.ts re-defines {owned_elsewhere} instead of "
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
