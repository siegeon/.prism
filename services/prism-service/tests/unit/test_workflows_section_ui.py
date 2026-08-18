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
    assert re.search(r'/api/workflows\?project=\$\{encodeURIComponent\(project\)\}', page), (
        "the page must scope its fetch to the selected project")
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
    for symbol in ("routeOrthogonal", "drawWire", "spawnPacket",
                   "stepPackets", "drawPackets"):
        assert symbol in graph, (
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
    assert "/api/workflows" in hook, (
        "lib/useWorkflowDef.ts must fetch the step definition from "
        "GET /api/workflows")
    assert re.search(r'export\s+function\s+useWorkflowSteps\s*\(', hook), (
        "useWorkflowSteps() is the hook the conductor rail consumes")


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
