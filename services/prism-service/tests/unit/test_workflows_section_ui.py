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
    # RELOCATED (task d9f082fe follow-up, owner live, 2026-08-24): "it
    # should NEVER go white -- it should have the banner letting the
    # customer know it's updating". A blind window.location.reload() on a
    # failed chunk import could fire while the server was still
    # unreachable, and a failed reload navigation shows the BROWSER's own
    # blank error page -- nothing React-level can intercept that. App.tsx
    # now defers to lib/reconnect.ts's waitForServerThenReload(), which
    # only reloads after a real probe succeeds, showing ReconnectBanner
    # while it waits.
    assert "waitForServerThenReload()" in app
    assert 'import { waitForServerThenReload } from "@/lib/reconnect";' in app


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


def test_switching_workflows_rehydrates_the_owners_saved_layout():
    """Owner-reported bug: manually positioned nodes reset on re-navigation.
    Root cause was NOT a missing persistence layer (positions/ports/waypoints
    were already written to localStorage on drag-end) -- it was
    selectWorkflow() (fired by every directory click, drilling into a linked
    workflow, and the breadcrumb back button) calling clearOverrides() and
    never refilling the maps from localStorage afterward. The very next drag
    anywhere then called persist(), writing that now-EMPTY map back to
    localStorage and permanently destroying whatever the owner had arranged.
    The fix mirrors the initial-mount effect's own ordering: node positions
    rehydrate BEFORE setDef (so nodes don't visibly snap from default to
    saved), wire ports/waypoints rehydrate AFTER setDef (they validate
    against the freshly built wire list, which only exists post-setDef)."""
    page = _read("pages", "WorkflowsPage.tsx")

    fn_start = page.index("const selectWorkflow = useCallback(")
    clear_idx = page.index("graphRef.current.clearOverrides();", fn_start)
    hydrate_positions_idx = page.index(
        "readJson<Record<string, Point>>(positionsKey(project),", clear_idx)
    hydrate_positions_call = page.index(
        "(raw) => graphRef.current.hydrateOverrides(raw));", hydrate_positions_idx)
    setdef_idx = page.index(
        "graphRef.current.setDef(workflowForGraph(workflow));", hydrate_positions_call)
    hydrate_ports_idx = page.index(
        "readJson<Record<string, WirePort>>(portsKey(project),", setdef_idx)
    hydrate_ports_call = page.index(
        "(raw) => graphRef.current.wireEdits.hydrate(raw, undefined));", hydrate_ports_idx)
    hydrate_waypoints_idx = page.index(
        "readJson<Record<string, Point[]>>(waypointsKey(project),", hydrate_ports_call)
    page.index(
        "(raw) => graphRef.current.wireEdits.hydrate(undefined, raw));", hydrate_waypoints_idx)

    assert clear_idx < hydrate_positions_idx < setdef_idx < hydrate_ports_idx < hydrate_waypoints_idx, (
        "selectWorkflow must clear overrides, rehydrate saved node "
        "positions from localStorage BEFORE setDef, then rehydrate saved "
        "wire ports/waypoints AFTER setDef")

    # Task <workflow-deep-link>: selectWorkflow also keeps ?workflow=<id> in
    # sync (every call site — directory click, drill-in, breadcrumb back —
    # routes through here), so its dependency array grew setSearchParams.
    fn_body_end = page.index("}, [project, setSearchParams]);", setdef_idx)
    assert fn_body_end < page.index("const selectedWorkflow = workflows.find", fn_start), (
        "selectWorkflow's useCallback must depend on `project` and "
        "`setSearchParams` now that it reads project-scoped localStorage "
        "keys directly and writes the URL's ?workflow= param"
    )


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
        # SUPERSEDED (task: a task's own workflow renders its own FSM steps,
        # not always "implement"'s): useWorkflowSteps() now takes an
        # optional `workflow` argument so the rail resolves the CALLING
        # task's own workflow (e.g. "workflow" prop) instead of always the
        # top-level implement/conductor steps — see
        # test_step_rail_uses_task_own_workflow.py. The call-with-no-args
        # form this test originally pinned still works (falls back to the
        # default resolution) but is no longer the only valid call shape.
        assert re.search(r'const\s+steps\s*=\s*useWorkflowSteps\([^)]*\)', src), (
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
    # The next two assertions are superseded by task 25b2a05c:
    # verify_green_state now carries its own real node
    # (verify-green-state-loop, see test_every_step_is_a_node_on_the_card.py)
    # instead of a fallback link into the unrelated "validation" catalog
    # entry -- the backend always supplies a real linked_workflow_id now, so
    # the frontend fallback these two lines used to pin is gone.
    assert '"verify-green-state-loop" if step["id"] == "verify_green_state"' in api
    assert "const linkedStep = selectedWorkflow?.steps.find(" in page
    assert "const linkedWorkflowId = linkedStep?.linked_workflow_id" in page
    assert "workflow.id === linkedWorkflowId" in page
    assert "selectWorkflow(linkedWorkflow, [...workflowPath, {" in page
    assert "const linkedId = step.linked_workflow_id ?? null;" in page
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
    # Superseded by test_workflow_canvas_progress_uses_p95.py (owner
    # 2026-08-26): the pacing source is now p95 of real recent same-
    # step durations, with the plain mean as its fallback only.
    assert "elapsedSeconds / pacing" in page
    assert "Math.min(0.98" in page
    assert "activeProgress" in graph
    assert "ctx.fillRect(x + 1, y + 1, w - 2, 3)" in graph
    assert "ctx.fillRect(x + 1, y + 1, fillWidth, 3)" in graph
    # SUPERSEDED 2026-08-26 (owner, live, pointing at a card's own body:
    # "a bar in the body of the panel filling over time"): the header rail
    # used to be the ONLY progress paint, deliberately, so nothing ever had
    # to render beneath the card's text. The owner asked for the card body
    # itself to fill left-to-right too -- a low-opacity fill plus a
    # brighter leading edge, painted before the text so it still never sits
    # ABOVE the content, just no longer absent from the body either.
    assert "bodyY = y + 20, bodyH = h - 20" in graph
    assert "ctx.fillRect(x + 1, bodyY, fillWidth, bodyH - 1)" in graph
    assert "rgba(${rgb}, 0.16)" in graph
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
    # AC-7 (task e14680ba) made replay speed user-adjustable, so the call site
    # now threads a `speed` arg through (default REPLAY_SPEED preserved on the
    # function signatures) instead of the hardcoded constant read at the call --
    # the underlying elapsed/speed math this test guards is unchanged.
    assert "replayStepMs(event, speed)" in page
    assert "replayGapMs(event" in page
    assert "replaySpanMs(event" in page
    assert "Replay ${replaySpeed}×" in page
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
    # midWalkFailure (a retried step that failed mid-replay) shares the SAME
    # "failure" tone as the run's own terminal failure -- one vocabulary,
    # not two competing ones.
    assert 'replayFinishedAtFailure || midWalkFailure\n          ? "failure"' in page


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
    # See the comment above test_historical_run_can_be_replayed_...: AC-7
    # (task e14680ba) added a `speed` arg to both calls; same span math.
    assert "replayStepMs(event, speed) + replayGapMs(event, next, speed)" in page
    assert "setReplayEventIndex((index) => (index ?? 0) + 1), duration" in page


# ---------------------------------------------------------------------------
# The conductor rail's pill click opens the SAME animated instance view
# "Build and test"'s rail already has (owner 2026-08-21: "when i click on
# that bar it opens the INSTANCE of the workflow its describing, theres a
# whole animation and everything, please make sure the conductor view is
# using that same logic"). Before this, a conductor pill's onClick was
# `navigate('/tasks/${task.id}')` -- a plain page redirect with no overlay,
# no replay, no reused machinery at all.
# ---------------------------------------------------------------------------

def test_conductor_pill_reuses_replay_history_run_instead_of_navigating_away():
    page = _read("pages", "WorkflowsPage.tsx")

    # The click handler feeds the SAME replayHistoricalRun/beginHistoricalReplay
    # pipeline validation's rail already drives -- no second, parallel
    # "instance view" implementation for conductor.
    handler = _function_body(page, "const openConductorInstance = useCallback((task: ManagedTask) => {")
    assert "fetchConductorRunFromTask(project" in handler
    assert "replayHistoricalRun(run)" in handler
    # A synth fetch failure still lands the owner somewhere real, never a
    # dead pill.
    assert "navigate(`/tasks/${task.id}`)" in handler

    # The rail's onClick calls the new handler, not navigate directly.
    conductor_pills = page.index('const railPills: RailPill[] = isStateMachineWorkflow')
    validation_branch = page.index(": Array.from({ length: RUN_RAIL_PILLS }, (_, index) => {\n        const run = visibleRunHistory")
    conductor_branch = page[conductor_pills:validation_branch]
    assert "onClick: task ? () => openConductorInstance(task) : undefined" in conductor_branch
    assert "navigate(`/tasks/${task.id}`)" not in conductor_branch


def test_conductor_run_is_synthesized_from_the_tasks_own_history_not_a_workflowcore_run():
    data = _read("lib", "useWorkflowDef.ts")

    assert 'export function fetchConductorRunFromTask' in data
    assert '/api/tasks/${encodeURIComponent(task.id)}?project=' in data
    assert 'scope=core' in data
    # conductor_service.advance_task writes exactly this shape (details=
    # "from=X; to=Y...") -- the timeline is built off the task's OWN audit
    # history, there is no separate run-history endpoint for it.
    assert 'row.action === "advance_task"' in data
    assert '/to=([^;]+)/' in data
    # tests/build are optional -- a conductor run has neither, and nothing
    # should have to fake them to satisfy the WorkflowRun type.
    assert "tests?: WorkflowStepResult;" in data
    assert "build?: WorkflowStepResult;" in data
    assert "conductorTask?: {" in data


def test_conductor_instance_badge_shows_task_state_never_build_test_language():
    page = _read("pages", "WorkflowsPage.tsx")

    summary = _function_body(page, "function conductorRunSummary(run: WorkflowRun): string {")
    assert "run.data.conductorTask" in summary
    assert "t.title" in summary
    assert "shipped" in summary
    assert "awaiting gate" in summary
    assert "build" not in summary.lower()
    # (gateState/workflowStep legitimately contain "test" as a substring
    # artifact -- "gaTeSTate" -- so assert the absence of the actual
    # validation phrasing instead of a bare "test" substring.)
    assert "build ${" not in summary and "· test" not in summary

    # The top run badge renders the conductor summary instead of the
    # build/test sentence when the conductor workflow is selected.
    badge = page[page.index('{(workflowRun || workflowRunError) && ('):page.index('{selectedHistoryRun && (')]
    assert 'isStateMachineWorkflow && workflowRun\n                ? conductorRunSummary(workflowRun)' in badge
    assert 'isStateMachineWorkflow && workflowRun ? conductorRunTone(workflowRun)' in badge
    # The validation sentence (build X / test Y) still renders unchanged for
    # every other workflow -- this is an ADDED branch, not a rewrite.
    assert "build ${workflowRun.data.build?.status} · test ${workflowRun.data.tests?.status}" in badge


def test_a_conductor_task_still_in_flight_shows_live_progress_not_a_stale_replay():
    page = _read("pages", "WorkflowsPage.tsx")

    begin = _function_body(page, "const beginHistoricalReplay = useCallback((run: WorkflowRun) => {")
    live_branch_start = begin.index('if (run.runtime?.status === "running") {')
    replay_branch_start = begin.index("const historicalStepIds = new Set(historicalWorkflow.steps.map((step) => step.id));")
    assert live_branch_start < replay_branch_start
    live_branch = begin[live_branch_start:replay_branch_start]
    # No timeline replay for a task that hasn't finished -- just occupy its
    # current step, the same way a running scripted workflow's step gets a
    # live progress bar (activeProgress reads workflowRun.runtime for this).
    assert "testModeRef.current = null" in live_branch
    assert "replayTimelineRef.current = []" in live_branch
    assert "run.runtime?.currentStep" in live_branch


def test_conductor_run_poll_effect_never_hits_the_validation_only_run_endpoint():
    page = _read("pages", "WorkflowsPage.tsx")

    poll_effect = page[
        page.index('if (isStateMachineWorkflow) return;'):
        page.index("fetchWorkflowRun(workflowRun.id).then((next) => {")
    ]
    # GET /api/workflows/runs/:id is a WorkflowCore-instance route; a
    # conductor task id would 404 against it. The guard must precede the
    # fetch, not follow it.
    assert 'if (isStateMachineWorkflow) return;' in poll_effect
    assert poll_effect.index('if (isStateMachineWorkflow) return;') < \
        poll_effect.index('if (!workflowRun || ["Complete", "Terminated"].includes(workflowRun.status)) return;')


def test_selected_history_run_resolves_off_the_live_run_for_conductor():
    page = _read("pages", "WorkflowsPage.tsx")

    # workflowRunHistory stays validation-only (refreshRunHistory), so
    # conductor's "selected instance" must resolve off workflowRun itself,
    # never off that array (which would always be empty for it).
    block = page[page.index("const selectedHistoryRun = isStateMachineWorkflow"):page.index("const selectedHistoryFrameTone")]
    assert 'workflowRun && workflowRun.id === selectedHistoryRunId ? workflowRun : null' in block
    assert 'workflowRunHistory.find((run) => run.id === selectedHistoryRunId) ?? null' in block


def test_version_bumped_for_the_conductor_instance_view_reuse():
    ver_src = _read(_HERE.parent.parent.parent / "prism_service" / "__version__.py")
    m = re.search(r'PRISM_VERSION = "(\d+)\.(\d+)\.(\d+)"', ver_src)
    assert m, (
        "PRISM_VERSION must be plain release semver; got "
        f"{ver_src.splitlines()[:1]!r}")
    current = tuple(int(x) for x in m.groups())
    assert current > (7, 12, 41), (
        "PRISM_VERSION must be patch-bumped past 7.12.41 in the "
        "implementation commit for this user-visible /workflows change")


# ---------------------------------------------------------------------------
# The pill rail must be a GENERIC state-machine-family mechanism, never a
# literal "conductor" id check (task 3baadd19, 2026-08-24). First pass
# hardcoded `selectedWorkflowId === "conductor"` / `parent_id === "conductor"`
# -- owner, live, catching it directly: "the conductor family should not be
# hardcoded like that, these are all hierarchical, conductor is the only one
# now, but later we may have more state machine top level workflows." The
# fix derives family membership structurally: a workflow IS the family when
# something else nests under it (hasChildWorkflows), or it nests under one
# (parent_id set) -- "validation" is the one named exception (a genuinely
# scripted WorkflowCore workflow, nested for documentation only).
# ---------------------------------------------------------------------------


def test_state_machine_family_is_never_a_hardcoded_conductor_id_check():
    page = _read("pages", "WorkflowsPage.tsx")

    definition = _function_body_like(
        page, 'const hasChildWorkflows = workflows.some(',
        'const isStateMachineWorkflow = selectedWorkflowId !== "validation"',
    )
    assert 'workflow.parent_id === selectedWorkflowId' in definition, (
        "family membership for a TOP-LEVEL entry must be derived from "
        "whether anything nests under it, not a literal id")

    ism_block = page[page.index('const isStateMachineWorkflow = selectedWorkflowId !== "validation"'):
                     page.index('const conductorLivePhase')]
    assert 'hasChildWorkflows || !!selectedWorkflow?.parent_id' in ism_block
    # The literal string "conductor" must never appear as an equality
    # check anywhere in the derivation itself (comments are fine; this
    # scans the executable expression only).
    assert '=== "conductor"' not in ism_block
    assert '!== "conductor"' not in ism_block


def test_conductor_step_ids_resolves_the_parent_generically_not_via_conductor_literal():
    """The per-child step-id lookup must resolve its parent's OWN catalog
    entry (workflows.find(w => w.id === parentId)), never assume the
    parent is literally "conductor" -- a future second top-level bot must
    work identically without touching this code."""
    page = _read("pages", "WorkflowsPage.tsx")

    body = _function_body_like(
        page, "const conductorStepIds = useMemo(() => {", "}, [selectedWorkflow,",
    )
    assert 'const parentId = selectedWorkflow?.parent_id' in body
    assert 'workflows.find((workflow) => workflow.id === parentId)' in body
    # "land" stays a named, documented exception (mirrors the identical
    # hardcoded exception in api/workflows.py) -- but the GENERAL lookup
    # path above it must not hardcode "conductor" as the parent.
    assert 'workflow.id === "conductor"' not in body


def _function_body_like(source: str, start_marker: str, end_marker: str) -> str:
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return source[start:end]


# ---------------------------------------------------------------------------
# Follow-up (owner, live on this exact instance): "the animation on that
# playback appears to be made up" -- conductorTimelineFromHistory only ever
# read advance_task rows and stamped every closed segment "passed",
# silently discarding flow_report_failure (a step that failed and retried)
# and a rejected/control-plane-failed gate_decide. Verified live against
# task 93d6c6f3 (real history: 3 flow_report_failure rows at
# verify_green_state) and 1bc0b316 (2 flow_report_failure rows at
# write_failing_tests, plus a red_gate that ended gate_state=failed).
# ---------------------------------------------------------------------------

def test_conductor_failed_step_reader_recognizes_all_three_real_setback_shapes():
    data = _read("lib", "useWorkflowDef.ts")

    reader = _function_body(
        data, "function conductorFailedStepFromDetails(action: string | undefined, details: string): string | null {",
    )
    # flow_report_failure / advance_refused: conductor_service.py writes
    # `step=<id>; ...` -- the action NAME alone already says it failed, no
    # need to parse `outcome` (which varies shape: bare `fail`, or a
    # `{'ok': False, ...}` dict repr).
    assert 'action === "flow_report_failure" || action === "advance_refused"' in reader
    assert "/step=([^;]+)/" in reader
    # gate_decide: only a REAL rejection counts -- a verifier=fail row that
    # was immediately overridden (override=True) is a genuine pass, per
    # api/tasks.py _build_timeline's own documented ambiguity.
    assert 'action === "gate_decide"' in reader
    assert "/gate=(\\w+_gate)/" in reader
    assert "action=reject" in reader
    assert "control-plane=fail" in reader
    assert "verifier=fail" in reader and "override=True" in reader


def test_conductor_timeline_closes_a_failed_attempt_and_reopens_the_same_step():
    data = _read("lib", "useWorkflowDef.ts")

    builder = _function_body(
        data, "function conductorTimelineFromHistory(\n  history: ConductorHistoryRow[],\n): NonNullable<WorkflowRun[\"timeline\"]> {",
    )
    assert "conductorFailedStepFromDetails(row.action, row.details" in builder
    retry_guard = builder.index("if (failedStep && open?.step === failedStep) {")
    reopen = builder.index("open = { step: failedStep, startedAt: row.timestamp }")
    status_failed = builder.index('status: "failed"')
    # A failed attempt closes with status "failed" (the SAME vocabulary a
    # failed validation step already uses -- WorkflowStepResult["status"]),
    # never a new invented word, and a fresh dwell reopens at the SAME step
    # so the next attempt (pass or another failure) gets its own segment.
    assert retry_guard < status_failed < reopen


def test_conductor_replay_tints_a_mid_walk_retry_the_same_as_a_terminal_failure():
    page = _read("pages", "WorkflowsPage.tsx")

    walk_effect = _function_body(page, "useEffect(() => {\n    if (testModeRef.current !== \"replay\" || replayEventIndex === null) return;")
    # The per-event walker stamps the CURRENT event's own status into a ref
    # the rAF loop reads -- distinct from replayStoppedAt, which only ever
    # names the run's FINAL event.
    assert "replayEventStatusRef.current = event.status" in walk_effect
    assert "replayEventStatusRef.current = null" in walk_effect

    raf_replay = page[page.index('} else if (testModeRef.current === "replay"'):page.index("activeProgress = {\n          nodeId,")]
    assert 'replayEventStatusRef.current === "failed"' in raf_replay
    # Reuses the SAME tone/label vocabulary the terminal-failure branch
    # already renders (workflowGraph.ts's tone: "failure" -> red fill,
    # matching what a failed build/test step already looks like) -- no
    # separate "retry" visual invented for this.
    assert "const midWalkFailure = !replayFinishedAtFailure" in raf_replay
    assert 'replayFinishedAtFailure || midWalkFailure\n          ? "failure"' in raf_replay
    assert 'midWalkFailure\n          ? "FAILED"' in raf_replay


# ---------------------------------------------------------------------------
# Follow-up (owner, live on this exact instance): "This playback of the
# animation is not moving from step to step, its still just doing the
# random animations." Investigated empirically (Playwright label/screenshot
# traces at ~150-200ms resolution, before touching any code) rather than
# assumed: sendTransition/the replay walk DOES fire once per real event, in
# real order, with a genuine 1.5-5s visible dwell -- the stepping mechanism
# itself was never broken. What WAS broken: the def+occupancy poll
# (POLL_MS=10s, a `[project]`-only useEffect closure) unconditionally
# re-applied the LIVE BOARD's real occupancy over a replay's own synthetic
# single-node occupancy on every tick -- confirmed live via GET
# /api/workflows?project=prism: conductor's real occupancy is heavily
# nonzero ({green_gate: 25, plan_gate: 13, ...}) while every OTHER
# workflow's (validation included) is always {}, which is exactly why
# Build and test's own replay never showed this and conductor's did.
# ---------------------------------------------------------------------------

def test_live_occupancy_poll_skips_reapplying_def_while_an_instance_is_open():
    page = _read("pages", "WorkflowsPage.tsx")

    poll_load = _function_body(page, "const load = () => {\n      fetchWorkflowDef(project)")
    guard = poll_load.index("if (selected && !viewingInstanceRef.current) {")
    set_def = poll_load.index("graphRef.current.setDef(workflowForGraph(selected));")
    fit_call = poll_load.index('graphRef.current.fit(canvas?.clientWidth || 800, canvas?.clientHeight || 600);')
    # The guard must wrap BOTH the occupancy reapply and the camera refit --
    # re-fitting the camera while the owner is watching a replay would be
    # its own jarring interruption, same root cause.
    assert guard < set_def < fit_call


def test_viewing_instance_ref_is_a_ref_not_state_and_is_set_and_cleared():
    page = _read("pages", "WorkflowsPage.tsx")

    # Must be a ref: the poll's closure is created once per project (deps
    # are `[project]` only) and would otherwise read a stale, always-false
    # `selectedHistoryRunId`/state snapshot from whenever the effect last
    # ran, never seeing a later click's state update.
    assert "const viewingInstanceRef = useRef(false);" in page

    replay_start = _function_body(page, "const replayHistoricalRun = useCallback((run: WorkflowRun) => {")
    assert "viewingInstanceRef.current = true" in replay_start

    leave = _function_body(page, "const leaveHistoricalReplay = useCallback(() => {")
    assert "viewingInstanceRef.current = false" in leave

    # Switching to a different workflow entirely (sidebar click) must also
    # clear it, or an instance left open elsewhere would freeze the live
    # board poll forever afterward.
    select_workflow_start = page.index("const selectWorkflow = useCallback((")
    select_workflow_end = page.index("const selectedWorkflow = workflows.find(")
    assert select_workflow_start < select_workflow_end
    select_workflow = page[select_workflow_start:select_workflow_end]
    assert "viewingInstanceRef.current = false" in select_workflow


def test_replay_walk_genuinely_steps_one_real_event_at_a_time():
    page = _read("pages", "WorkflowsPage.tsx")

    # Pin the mechanics that make this a deliberate walk rather than an
    # instant jump: each event gets its own visible-duration timer before
    # advancing to the NEXT index, and REPLAY_MIN_STEP_MS floors every
    # segment (even a near-zero real duration) at a genuinely visible dwell.
    assert "REPLAY_MIN_STEP_MS = 1500" in page
    walk_effect = _function_body(page, "useEffect(() => {\n    if (testModeRef.current !== \"replay\" || replayEventIndex === null) return;")
    assert "window.setTimeout(() => setReplayEventIndex((index) => (index ?? 0) + 1), duration)" in walk_effect
    assert "graphRef.current.sendTransition(replayEventIndex === 0 ? \"__start__\" : timeline[replayEventIndex - 1].step, event.step)" in walk_effect


def test_version_bumped_for_the_retry_visibility_and_poll_stomping_fixes():
    ver_src = _read(_HERE.parent.parent.parent / "prism_service" / "__version__.py")
    m = re.search(r'PRISM_VERSION = "(\d+)\.(\d+)\.(\d+)"', ver_src)
    assert m, (
        "PRISM_VERSION must be plain release semver; got "
        f"{ver_src.splitlines()[:1]!r}")
    current = tuple(int(x) for x in m.groups())
    assert current > (7, 12, 42), (
        "PRISM_VERSION must be patch-bumped past 7.12.42 in the "
        "implementation commit for these two follow-up fixes")


# ---------------------------------------------------------------------------
# Third follow-up (owner, live on this exact instance, task a205eb7a mid-
# drive): "it is not clear what [behavior] is currently running... remember
# we have the logic to fill up the panel of the task while that workflow is
# active." Confirmed live via Playwright: fetchConductorRunFromTask's
# runtime.currentStep (derived from FRESH history) and data.conductorTask.
# workflowStep (the caller's STALE rail-poll field) could name two DIFFERENT
# steps on screen at once for a task advancing quickly -- and the only
# on-canvas "currently running" signal was a tiny ~85px node's 3px fill bar
# and a 10px "RUN Xs" corner label, nothing like the large, legible
# SdlcProgress panel TaskDetailPage/PlanView already use for this.
# ---------------------------------------------------------------------------

def test_fetch_conductor_run_prefers_the_fresh_task_over_the_callers_stale_copy():
    data = _read("lib", "useWorkflowDef.ts")

    builder = _function_body(
        data,
        "export function fetchConductorRunFromTask(project: string, task: ConductorRunTask): Promise<WorkflowRun> {\n  return api.get<{ task?: ConductorFreshTask; history: ConductorHistoryRow[] }>(",
    )
    assert "const title = fresh?.title ?? task.title;" in builder
    assert "const status = fresh?.status ?? task.status;" in builder
    assert "const workflowStep = fresh?.workflow_step ?? task.workflow_step;" in builder
    assert "const gateState = fresh?.gate_state ?? task.gate_state;" in builder
    # runtime.currentStep and conductorTask.workflowStep must derive from the
    # SAME freshly-read values -- never one fresh (history-derived) and one
    # stale (the caller's rail-poll snapshot), which is what let two
    # different step names appear on screen at once.
    assert "currentStep: last?.step ?? workflowStep ?? \"\"," in builder
    # gateReason joined this assignment in task 8fbd5cf0's refusal-legibility
    # fix (test_the_conductor_run_type_carries_gate_reason_end_to_end below)
    assert "workflowStep, gateState, gateReason, stranded," in builder


def test_workflows_page_reuses_sdlc_progress_for_a_live_conductor_instance():
    page = _read("pages", "WorkflowsPage.tsx")

    assert 'import SdlcProgress, { type Activity, type PhaseProgress } from "@/components/conductor/SdlcProgress";' in page
    # Task 8fbd5cf0 (c705e20a) SUPERSEDES the prior "step?.average_duration_seconds"
    # assertion here: that was a clock ratio (elapsed / the step's stored
    # average duration), exactly what the craft brief's stop_if forbids
    # ("any progress value derives from one shared typical duration instead
    # of that node's own measured history"). conductorLivePhase now reads
    # ONLY server-counted units (flow_run_recorder.progress_source, exposed
    # as flowRuns.progress) -- no clock, no average, ever.
    phase_memo = _function_body(
        page,
        'const conductorLivePhase = useMemo<PhaseProgress | null>(() => {',
    )
    # Generalized off the literal "conductor" id to the whole state-machine
    # family (owner: "each step here should have a playback mode... look at
    # how we did it with build and test") -- every bot-family behavior
    # canvas (green-gate-status, write-failing-tests-loop, etc.) gets the
    # SAME live fill a task's own conductor instance already got, not just
    # the top-level "conductor" bot. isStateMachineWorkflow already excludes
    # "validation" (line ~532), so that exclusion still holds here too.
    assert '!isStateMachineWorkflow || !workflowRun?.runtime || workflowRun.status !== "Runnable"' in phase_memo
    assert "const counted = flowRuns?.progress;" in phase_memo, (
        "conductorLivePhase must read the server's counted units, never a "
        "clock/average-duration ratio")
    assert "average_duration_seconds" not in phase_memo, (
        "a clock-ratio pacing source has crept back into the live phase memo")

    activity_memo = _function_body(page, "const conductorLiveActivity = useMemo<Activity | null>(() => {")
    # Reuses ACTIVITY_META's existing vocabulary (awaiting_gate/blocked/
    # working) -- the SAME states LiveBar/ConductorPage/PlanView already
    # render, never a new label invented for this one view.
    assert '"awaiting_gate"' in activity_memo
    assert '"blocked"' in activity_memo
    assert '"working"' in activity_memo

    render_block = page[page.index("{(workflowRun || workflowRunError) && ("):page.index("{selectedHistoryRun && (")]
    assert "conductorLivePhase && (" in render_block
    assert "<SdlcProgress" in render_block
    assert "phase={conductorLivePhase}" in render_block
    assert "activity={conductorLiveActivity}" in render_block


def test_sdlc_progress_reuse_never_touches_validation_or_a_finished_replay():
    page = _read("pages", "WorkflowsPage.tsx")

    # Null for anything but a LIVE (Runnable, i.e. not yet done) instance of
    # a state-machine-family workflow -- validation's own live-run badge and
    # a finished replay must render exactly as before. Superseded the old
    # literal `selectedWorkflowId !== "conductor"` guard (isStateMachineWorkflow
    # already excludes "validation", see test above) so every bot-family
    # canvas gets the same fill, not just the top-level "conductor" bot.
    phase_memo = _function_body(page, "const conductorLivePhase = useMemo<PhaseProgress | null>(() => {")
    guard = phase_memo.index('if (!isStateMachineWorkflow || !workflowRun?.runtime || workflowRun.status !== "Runnable") return null;')
    assert guard == phase_memo.index("if (")


def test_every_bot_family_canvas_auto_attaches_its_own_live_task():
    # Owner: "each step here should have a playback mode where the workflow
    # it is on is filling from left to right... look at how we did it with
    # build and test". Build and test (validation) has always auto-reattached
    # to its own in-flight run with no click (see the effect right above this
    # one); every other bot-family canvas required an explicit rail-pill
    # click to ever show a fill. This pins the generalized counterpart.
    page = _read("pages", "WorkflowsPage.tsx")
    attach = _function_body_like(
        page,
        "if (!isStateMachineWorkflow || workflowRun || searchParams.get(\"task\")) return;",
        "}, [isStateMachineWorkflow, workflowRun, searchParams, conductorRailTasks, openConductorInstance]);",
    )
    # Skips when the ?task= handler (the effect immediately above) already
    # owns opening a specific instance, so the two never race onto
    # different pills for the same canvas.
    assert 'searchParams.get("task")' in attach
    # Only a task genuinely IN FLIGHT right now counts -- a done task is
    # history, and clicking into history stays an explicit rail action
    # (mirrors validation, which never auto-replays a finished run either).
    assert 'task.status !== "done"' in attach
    # SUPERSEDED 2026-08-28 by task a928f3d5: this used to also require
    # '"pending"', treating a task parked at a gate as "in flight". It is
    # not -- it is waiting for a PERSON -- and attaching to one sets
    # viewingInstanceRef, which stops the definition poll from re-applying
    # live occupancy, freezing the whole board on a task doing nothing
    # (measured: a task awaiting review for 32 hours pinned the canvas while
    # 8 tasks drove through implement_tasks unseen). In flight now means
    # exactly activity working/driving.
    assert '"working"' in attach and '"driving"' in attach
    assert "gate_state" not in attach
    assert "openConductorInstance(live)" in attach
    # Most-recently-updated live task wins, not the oldest -- conductorRailTasks
    # is sorted ascending, so this must walk it in reverse.
    assert "[...conductorRailTasks].reverse()" in attach


def test_version_bumped_for_the_conductor_live_instance_legibility_fix():
    ver_src = _read(_HERE.parent.parent.parent / "prism_service" / "__version__.py")
    m = re.search(r'PRISM_VERSION = "(\d+)\.(\d+)\.(\d+)"', ver_src)
    assert m, (
        "PRISM_VERSION must be plain release semver; got "
        f"{ver_src.splitlines()[:1]!r}")
    current = tuple(int(x) for x in m.groups())
    assert current > (7, 12, 44), (
        "PRISM_VERSION must be patch-bumped past 7.12.44 in the "
        "implementation commit for this third follow-up fix")


# ---------------------------------------------------------------------------
# Owner-found gap: /workflows had exactly one route and selectedWorkflowId
# lived only in useState, so NO link could ever point at a specific behavior
# (e.g. "plan-gate-check") -- every link landed on whatever was last
# selected. Fixed by syncing selectedWorkflowId to ?workflow=<id> (the same
# useSearchParams convention Understand's ?concept= already uses), read once
# at mount and written back on every selectWorkflow() call so the address
# bar always names the behavior actually on screen.
# ---------------------------------------------------------------------------

def test_selected_workflow_seeds_from_the_url_query_param():
    page = _read("pages", "WorkflowsPage.tsx")
    # Re-anchored by task e14680ba (Trace tab -> own conductor flow): that slice
    # added `Link` to this import for the back-to-task control, so the exact
    # import line is no longer stable. The invariant is that useSearchParams is
    # imported from react-router-dom, whatever siblings share the line.
    rr_import = re.search(r'import \{([^}]*)\} from "react-router-dom";', page)
    assert rr_import and "useSearchParams" in rr_import.group(1), (
        "WorkflowsPage must import useSearchParams from react-router-dom")
    assert "const [searchParams, setSearchParams] = useSearchParams();" in page
    assert (
        'const [selectedWorkflowId, setSelectedWorkflowId] = useState(\n'
        '    () => searchParams.get("workflow") || "conductor",\n'
        '  );'
    ) in page, (
        "selectedWorkflowId must seed from ?workflow=<id> so a fresh page "
        "load with the param pre-selects that behavior, falling back to "
        "conductor when the param is absent")
    assert (
        'const selectedWorkflowRef = useRef(searchParams.get("workflow") || "conductor");'
    ) in page, (
        "selectedWorkflowRef (read by the catalog-load effect to pick the "
        "initially-selected entry once /api/workflows resolves) must start "
        "from the SAME url-seeded id as the state, not a hardcoded "
        '"conductor"')


def test_selecting_a_workflow_writes_the_url_so_the_link_is_shareable():
    """Every selectWorkflow() call site -- directory click, drill-in via a
    linked node, breadcrumb back -- routes through this ONE function, so
    writing the url param here (rather than at each call site) is what makes
    the address bar always match the canvas, no matter how the owner got
    there."""
    page = _read("pages", "WorkflowsPage.tsx")
    fn_start = page.index("const selectWorkflow = useCallback(")
    fn_body_end = page.index("}, [project, setSearchParams]);", fn_start)
    body = page[fn_start:fn_body_end]

    assert "selectedWorkflowRef.current = workflow.id;" in body
    assert "setSelectedWorkflowId(workflow.id);" in body
    set_params_idx = body.index("setSearchParams((prev) => {")
    assert set_params_idx > body.index("setSelectedWorkflowId(workflow.id);"), (
        "the url write should follow the state write, not race ahead of it")
    call_body = _function_body(body, "setSearchParams((prev) => {")
    assert "new URLSearchParams(prev)" in call_body, (
        "must build off the CURRENT params (preserving ?project= and any "
        "other existing query state), never a bare new URLSearchParams()")
    assert 'next.set("workflow", workflow.id);' in call_body
    assert "return next;" in call_body
    tail = body[body.index(call_body) + len(call_body):]
    assert re.match(r"\s*,\s*\{\s*replace:\s*true\s*\}\s*\)\s*;", tail), (
        "setSearchParams must be called with { replace: true } -- clicking "
        "through several behaviors is one page's view state changing, not "
        "a new back-button stop each time")
# --------------------------------------------------------------------------
# Task a205eb7a: the top-right toolbar's "Simulate flow" button (rendered
# whenever the selected workflow's steps are not all execution="scripted")
# was a mock step-through animation nobody asked for. Remove the button and
# every bit of code that only existed to drive it, without touching the
# sibling "Run workflow" (scripted) path or the historical-run/replay
# affordances, which share the same toolbar and some of the same state.
# --------------------------------------------------------------------------

def test_simulate_flow_button_is_removed_from_the_toolbar():
    """No rendered "Simulate flow" affordance, and none of the dead code
    that only existed to drive it: the startFlowSimulation handler, the
    "simulation" arm of the testModeRef union type, and the effect tail
    gated on it. A single case-insensitive sweep for "simulat" across the
    comment-stripped source is the right invariant here (not a fixed
    character window) -- every one of those symbols contains that
    substring, and the premise notes confirm (via a targeted grep this
    session) that no OTHER, unrelated code in this file does. The sibling
    "Run workflow" (scripted) button must survive untouched."""
    page = _read("pages", "WorkflowsPage.tsx")

    leaked = re.findall(r"[A-Za-z][\w\"' ]*simulat[\w\"' ]*", page, re.IGNORECASE)
    assert not leaked, (
        f"WorkflowsPage.tsx still references simulation-only code: {leaked!r} "
        "-- the Simulate flow button, startFlowSimulation, the "
        "testModeRef \"simulation\" union arm, and its guarded effect tail "
        "must all be deleted together")

    assert "onClick={runScriptedWorkflow}" in page, (
        "the sibling \"Run workflow\" button must still call "
        "runScriptedWorkflow directly, not through the removed ternary")
    assert 'disabled={startingWorkflow}' in page, (
        "the Run workflow button's disabled state must survive the removal")
    assert "Run workflow" in page and "Starting…" in page, (
        "the Run workflow button's labels must survive the removal")


# --------------------------------------------------------------------------
# Remove the "Run workflow" button when Conductor is selected. The button
# is only meaningful for scripted workflows triggered via POST /api/workflows/runs.
# Conductor is driven by PRISM tasks via conductor_work(), not manual runs.
# --------------------------------------------------------------------------

def test_run_workflow_button_is_hidden_for_conductor():
    """The "Run workflow" button must NOT render when selectedWorkflowId === "conductor".
    The "Ran {timestamp}" replay-return button must remain visible when a
    selectedHistoryRun is active, regardless of which workflow is selected."""
    page = _read("pages", "WorkflowsPage.tsx")

    # Verify the Run workflow button is gated on selectedWorkflowId !== "conductor"
    assert "selectedWorkflowId !== \"conductor\"" in page, (
        "the Run workflow button must be hidden when Conductor is selected")

    # Verify the button calls runScriptedWorkflow
    assert "onClick={runScriptedWorkflow}" in page, (
        "the Run workflow button must call runScriptedWorkflow")

    # Verify the "Ran {timestamp}" button condition is unchanged (always shows on selectedHistoryRun)
    assert "{selectedHistoryRun ? (" in page, (
        "the historical-run replay-return button must still be gated on selectedHistoryRun")
    assert "leaveHistoricalReplay" in page, (
        "the Ran button must still call leaveHistoricalReplay")


# --------------------------------------------------------------------------
# Craft bar (task 8fbd5cf0): "Respect prefers-reduced-motion: fall back to
# instant state changes, never to a broken layout." The token/packet system
# is a hand-rolled canvas-2D animation (workflowGraph.ts/packets.ts), not a
# CSS transition a browser setting suppresses for free. The page already
# reads the OS preference live via motion/react's useReducedMotion() (for
# SdlcProgress's own tweens) -- the fix relays that SAME value onto the
# graph instance the rAF loop actually draws, rather than standing up a
# second, independent matchMedia listener that could drift from the first.
# --------------------------------------------------------------------------

def test_the_page_relays_the_os_reduced_motion_preference_into_the_graph():
    """WorkflowsPage.tsx must hand the SAME `reduced` value it already reads
    (useReducedMotion(), used by SdlcProgress) onto the canvas graph
    instance, reactively -- not a parallel flag nothing reads, and not a
    second independent OS-preference read that could disagree with the
    first."""
    page = _read("pages", "WorkflowsPage.tsx")

    assert "const reduced = useReducedMotion();" in page, (
        "the page's existing OS reduced-motion read must survive untouched"
    )
    effect_body = _function_body(
        page,
        "useEffect(() => {\n    graphRef.current.setReducedMotion(!!reduced);",
    )
    assert "graphRef.current.setReducedMotion(!!reduced);" in effect_body, (
        "the graph instance the canvas actually renders must be kept in "
        "sync with the page's single `reduced` source of truth")
    assert "[reduced]" in page, (
        "the relay effect must re-run whenever the OS preference changes, "
        "not just once at mount")


def test_reduced_motion_means_no_packet_ever_travels():
    """WorkflowGraph must actually DO something with reducedMotion: step()
    (the ambient-motion driver) must bail before spawning or advancing any
    packet, and sendTransition (the deliberate, per-transition call) must
    skip spawning a travelling marker while still returning true -- the
    node's own occupied/verdict state changes the same frame regardless,
    so a transition is never silently dropped, only its marker is."""
    src = _read("live", "workflowGraph.ts")

    step_body = _function_body(src, "step(dtMs: number, now: number): void {")
    assert re.match(r"\{\s*if \(this\.reducedMotion\) return;", step_body), (
        "step() must bail out before any ambient packet spawn/advance "
        "when reducedMotion is set")

    send_body = _function_body(src, "sendTransition(source: string, target: string): boolean {")
    assert "if (!this.reducedMotion) this.packets.push(spawnPacket(" in send_body, (
        "sendTransition must gate the travelling-marker spawn on "
        "reducedMotion -- the transition itself (return true) must not "
        "be gated, only the marker")

    assert "setReducedMotion(reduced: boolean): void {" in src, (
        "the graph needs a setter the page can call on mount and on "
        "every OS preference change"
    )


# --------------------------------------------------------------------------
# Task 8fbd5cf0 stop_if: "A gate reads as deciding or waiting while its seat
# has already refused" / "A stored gate refusal reason never reaches the
# canvas." Live misfire named in the task: on fc471aed the bottom bar read
# "machine deciding" for 18 minutes after the seat had already refused, and
# the stored reason (naming the exact fix) never reached the screen. Fixed
# by threading task.gate_reason through fetchConductorRunFromTask's
# synthesized run and reading gateState in the banner's own tone, not just
# run.status (which stays "Runnable" for a refused-but-not-yet-rewound
# task -- the SAME state a still-deciding gate is in).
# --------------------------------------------------------------------------

def test_a_refused_gate_reads_differently_from_a_deciding_one_on_the_canvas():
    page = _read("pages", "WorkflowsPage.tsx")

    tone_body = _function_body(page, "function conductorRunTone(run: WorkflowRun): string {")
    assert 'run.data.conductorTask?.gateState === "failed"' in tone_body, (
        "the banner's own border/text tone must react to a refused gate "
        "even while run.status is still \"Runnable\" -- the exact state a "
        "gate that is merely still deciding is also in")
    assert '"border-red-500/60 text-red-300"' in tone_body

    summary_body = _function_body(page, "function conductorRunSummary(run: WorkflowRun): string {")
    assert 't.gateState === "failed" ? ` · REFUSED: ${t.gateReason' in summary_body, (
        "a refused gate's summary text must include the STORED reason, "
        "not just a generic \"gate failed\" label a driver can't act on")

    # The call site must actually pass gate_reason through -- adding the
    # field to the type without wiring the one real caller would leave it
    # permanently undefined.
    assert "gate_reason: task.gate_reason," in page, (
        "openConductorInstance must pass the task's real gate_reason into "
        "fetchConductorRunFromTask, or gateReason is always empty")


def test_the_conductor_run_type_carries_gate_reason_end_to_end():
    """useWorkflowDef.ts: gate_reason must survive from the caller's task
    row, through the /api/tasks?scope=core refetch (fresh wins when
    present, same fallback pattern as workflowStep/gateState), onto the
    synthesized run's conductorTask -- not dropped at any one of the three
    hops."""
    src = _read("lib", "useWorkflowDef.ts")

    assert "gate_reason?: string | null;" in src, (
        "ConductorRunTask and ConductorFreshTask must both carry gate_reason"
    )
    assert "gateReason?: string | null;" in src, (
        "the synthesized run's conductorTask type must expose it"
    )
    builder = _function_body(
        src,
        "export function fetchConductorRunFromTask(project: string, task: ConductorRunTask): Promise<WorkflowRun> {\n  return api.get<{ task?: ConductorFreshTask; history: ConductorHistoryRow[] }>(",
    )
    assert "const gateReason = fresh?.gate_reason ?? task.gate_reason ?? null;" in builder, (
        "the fresh re-fetch must win over the caller's possibly-stale "
        "gate_reason, same as workflowStep/gateState just above it")
    assert "workflowStep, gateState, gateReason, stranded," in builder, (
        "gateReason must actually be assigned onto the returned "
        "conductorTask object, not just computed and dropped")
