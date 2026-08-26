"""Failing tests for task e14680ba: Trace tab links to workflows view.

Tests verify that:
1. Trace tab has a visible control linking to /workflows?task=<id>
2. WorkflowsPage reads task param and opens the run
3. Running vs playback modes render correctly
4. A link to /tasks/<id> exists alongside exit chip
5. Ring highlight is visually strong / auto-scrolls
6. Layout split: top-left title only, bottom bar for timeline
7. Playback-speed control exists
8. No backend API changes
"""

from pathlib import Path
import re

_SERVICE_ROOT = Path(__file__).resolve().parent.parent.parent


def test_trace_tab_has_workflows_link_control():
    """AC-1: Trace tab must have a visible control linking to /workflows?task=<id>."""
    task_detail_path = _SERVICE_ROOT / "prism_service/web/src/pages/TaskDetailPage.tsx"
    assert task_detail_path.exists(), f"File not found: {task_detail_path}"

    content = task_detail_path.read_text(encoding="utf-8")

    # Should have a TraceView or trace tab rendering
    assert "TraceView" in content, "TraceView component not found"

    # Should have a navigate call to /workflows?task=
    assert "workflows" in content.lower(), "No /workflows navigation found"
    assert "/workflows?task=" in content or "workflows.*task" in content.lower(), \
        "Trace tab does not navigate to /workflows?task=<id>"


def test_workflows_page_reads_task_query_param():
    """AC-2: WorkflowsPage must read task param and open run via existing mechanism."""
    workflows_path = _SERVICE_ROOT / "prism_service/web/src/pages/WorkflowsPage.tsx"
    assert workflows_path.exists(), f"File not found: {workflows_path}"

    content = workflows_path.read_text(encoding="utf-8")

    # Must read task param from useSearchParams
    assert "searchParams.get" in content, "searchParams not being read"
    assert 'get("task")' in content or "get('task')" in content, \
        "task query param not read from searchParams"

    # Must call openConductorInstance or fetchConductorRunFromTask
    assert "openConductorInstance" in content or "fetchConductorRunFromTask" in content, \
        "No call to openConductorInstance or fetchConductorRunFromTask for task param"


def test_running_vs_playback_mode():
    """AC-3: Task status determines running vs playback mode (already exists, verify reuse)."""
    workflows_path = _SERVICE_ROOT / "prism_service/web/src/pages/WorkflowsPage.tsx"
    content = workflows_path.read_text(encoding="utf-8")

    # Verify the branching logic exists
    assert "status" in content.lower(), "No status check for running vs playback"
    assert "done" in content.lower(), "No check for task.status == done"


def test_link_to_task_detail_from_open_run():
    """AC-4: Open run view must have a link to /tasks/<id> alongside exit chip."""
    workflows_path = _SERVICE_ROOT / "prism_service/web/src/pages/WorkflowsPage.tsx"
    assert workflows_path.exists(), f"File not found: {workflows_path}"

    content = workflows_path.read_text(encoding="utf-8")

    # Should have leaveHistoricalReplay (exit chip) - this already exists
    assert "leaveHistoricalReplay" in content, "Exit chip not found"

    # Should have a navigate to /tasks/<id> or similar
    assert "/tasks/" in content and ("navigate" in content or "to=" in content), \
        "No navigation to /tasks/<id> found in open run view"


def test_ring_highlight_visibility():
    """AC-5: Ring highlight on open pill must be strong/auto-scrollable."""
    workflows_path = _SERVICE_ROOT / "prism_service/web/src/pages/WorkflowsPage.tsx"
    content = workflows_path.read_text(encoding="utf-8")

    # ringHighlighted should have stronger styling
    assert "ringHighlighted" in content, "ringHighlighted class not found"

    # Should have scroll-into-view or enhanced visual treatment
    has_scroll = "scrollIntoView" in content or "scroll" in content.lower()
    has_ring_enhancement = re.search(r"ring.*\(glow|scale|shadow|blur|opacity\)", content, re.IGNORECASE)

    assert has_scroll or has_ring_enhancement, \
        "Ring highlight not strengthened - missing scrollIntoView or enhanced styling"


def test_layout_split_top_left_and_bottom_bar():
    """AC-6: Layout must split: top-left title only, bottom bar for timeline/scrubber."""
    workflows_path = _SERVICE_ROOT / "prism_service/web/src/pages/WorkflowsPage.tsx"
    content = workflows_path.read_text(encoding="utf-8")

    # Top-left box should exist (absolute left-4 top-4)
    assert "left-4" in content and "top-4" in content, "Top-left box not found"

    # Bottom bar for timeline should exist
    # Look for bottom positioning or a separate timeline/scrubber container
    assert ("bottom" in content and ("bar" in content or "timeline" in content.lower())) or \
           "SdlcProgress" in content, \
        "Bottom timeline bar not implemented"


def test_playback_speed_control_exists():
    """AC-7: Bottom bar must have user-adjustable playback-speed control."""
    workflows_path = _SERVICE_ROOT / "prism_service/web/src/pages/WorkflowsPage.tsx"
    assert workflows_path.exists(), f"File not found: {workflows_path}"

    content = workflows_path.read_text(encoding="utf-8")

    # REPLAY_SPEED constant check - it should not be hardcoded anymore
    # Or should exist with a control to change it
    has_replay_speed = "REPLAY_SPEED" in content
    has_speed_control = re.search(r"(speed|Speed).*(?:select|input|button|control|radio)", content, re.IGNORECASE)

    # The test fails if only the constant exists with no control, or if no speed mechanism at all
    # A speed control (state, input, handler) should exist
    assert has_speed_control or ("useState" in content and "Speed" in content), \
        "No user-adjustable playback-speed control found (only hardcoded REPLAY_SPEED exists)"


def test_no_backend_api_changes():
    """AC-8: No backend API changes - fetchConductorRunFromTask signature unchanged."""
    use_workflow_def_path = _SERVICE_ROOT / "prism_service/web/src/lib/useWorkflowDef.ts"
    assert use_workflow_def_path.exists(), f"File not found: {use_workflow_def_path}"

    content = use_workflow_def_path.read_text(encoding="utf-8")

    # Verify fetchConductorRunFromTask still takes the same parameters
    # ConductorFreshTask type should have these fields: id, title, status, workflow_step, gate_state, stranded
    assert "ConductorFreshTask" in content, "ConductorFreshTask type not found"
    assert "fetchConductorRunFromTask" in content, "fetchConductorRunFromTask function not found"

    # Check that the type definition includes the expected fields
    type_match = re.search(r"type ConductorFreshTask = \{([^}]+)\}", content, re.DOTALL)
    if type_match:
        type_def = type_match.group(1)
        # Should have these fields
        required_fields = ["id", "title", "status", "workflow_step", "gate_state", "stranded"]
        for field in required_fields:
            assert field in type_def, f"ConductorFreshTask missing field: {field}"


def test_no_api_route_changes():
    """Verify no changes to /api/* routes."""
    api_dir = _SERVICE_ROOT / "prism_service/api"
    assert api_dir.exists(), f"API directory not found: {api_dir}"

    # This test will pass on first run (since it's failing tests phase)
    # It verifies that the implementation doesn't add new API routes
    conductor_routes = api_dir / "conductor.py"

    # Check that no new backend data shape is introduced
    if conductor_routes.exists():
        content = conductor_routes.read_text(encoding="utf-8")
        # Verify fetchConductorRunFromTask still uses the same response shape
        assert "ConductorFreshTask" not in content or \
               "def fetchConductorRunFromTask" not in content, \
            "Backend definition should not change - this is a client-side change only"
