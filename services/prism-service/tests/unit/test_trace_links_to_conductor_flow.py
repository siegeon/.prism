"""Task e14680ba: the task detail page's Trace tab links to that SAME
task's own conductor flow instance on the Workflows page.

Convention (test_workflows_section_ui.py / test_conductor_page_animated_
cleanup_ui.py): the PRISM SPA has no JS test runner, so UI ACs are pinned by
asserting the ACTUAL TSX source, parsed by enclosing structure rather than a
fixed character window.

CORRECTED SCOPE (owner, live, 2026-08-25): not a build-from-scratch feature.
WorkflowsPage.tsx already has the exact live/playback machinery this needs --
openConductorInstance -> fetchConductorRunFromTask -> replayHistoricalRun
(WorkflowsPage.tsx:974-982), reused today by the conductor rail's own pill
click. The gap is threefold: (1) nothing on the Trace tab links to it, (2)
WorkflowsPage has no way to auto-open an instance from a plain URL (only a
rail click drives it today), and (3) the opened instance has no link back to
the task's own detail page -- only an exit-to-live chip. These tests pin all
three, and guard against the likely misfire named on this ticket: building a
second, parallel instance/playback renderer instead of reusing
openConductorInstance/fetchConductorRunFromTask/replayHistoricalRun.
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
    src = re.sub(r"//[^\n]*", "", src)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    return src


def _read(*parts: str) -> str:
    path = _WEB.joinpath(*parts)
    assert path.exists(), f"expected {path} to exist"
    return _strip_comments(path.read_text(encoding="utf-8"))


def _function_body(src: str, signature: str) -> str:
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


# ---------------------------------------------------------------------------
# 1. The Trace tab exposes a control that opens THIS task's own conductor run.
# ---------------------------------------------------------------------------

def test_trace_view_receives_the_task_identity_it_needs_to_link_out():
    """SUPERSEDED PLACEMENT (owner, live, 2026-08-25, twice): the control
    was first built inside TraceView (keyed by taskId/taskStatus props),
    then the owner moved it to the Overview/Trace/Evidence tab row, then
    moved it AGAIN to the task detail page's own top breadcrumb bar so it is
    visible from every tab, not just Trace -- a page-level affordance needs
    no props threaded into TraceView at all, since it reads `task.id`
    directly in the page's own render. Oracle unchanged: "a visible control
    opens that task's own run on the Workflows page" -- just re-anchored to
    where that control actually lives now.
    """
    page = _read("pages", "TaskDetailPage.tsx")
    assert "task?.id" in page or "task.id" in page, "task id not in scope for the breadcrumb link"
    # TraceView itself must NOT have been re-widened back to needing these --
    # that would be the old, owner-rejected placement creeping back in.
    call = re.search(r'<TraceView\s+([^>]*?)/>', page)
    assert call, "TaskDetailPage.tsx no longer renders <TraceView ... />"
    assert "taskId=" not in call.group(1), (
        "TraceView should not receive taskId again -- the deep-link control "
        "lives in the page's breadcrumb bar now, not inside the Trace tab")


def test_trace_view_renders_a_control_that_deep_links_to_the_workflows_page():
    """The control itself: a real, clickable affordance in the task detail
    page's own breadcrumb row (re-anchored per the second owner placement
    revision above), not inside TraceView and not a comment describing one.
    """
    page = _read("pages", "TaskDetailPage.tsx")
    assert 'aria-label="Open this task\'s conductor flow"' in page, (
        "no discoverable control to open the task's own conductor run")
    assert re.search(r'to=\{`/workflows\?task=\$\{task\.id\}`\}', page), (
        "the control must deep-link to /workflows?task=<id> -- the same "
        "?workflow= deep-link convention WorkflowsPage already honors")
    # It must live in the page's own render, not TraceView's.
    trace_body = _function_body(page, "function TraceView(")
    assert 'aria-label="Open this task\'s conductor flow"' not in trace_body, (
        "the control moved OUT of TraceView per the owner's placement "
        "revisions -- it should not also still be rendered inside it")


# ---------------------------------------------------------------------------
# 2. WorkflowsPage reads ?task=<id> on mount and opens that instance through
#    the EXISTING pipeline -- never a second, parallel renderer.
# ---------------------------------------------------------------------------

def test_workflows_page_opens_the_deep_linked_task_on_mount():
    page = _read("pages", "WorkflowsPage.tsx")
    needle = 'searchParams.get("task")'
    assert needle in page, (
        "WorkflowsPage never reads a ?task= param -- a Trace-tab link into "
        "this page has nothing to land on")
    idx = page.index(needle)
    around = page[max(0, idx - 200):idx + 800]
    assert "openConductorInstance(" in around, (
        "the ?task= handler must open the instance through the SAME "
        "openConductorInstance pipeline the conductor rail's pill click "
        "uses -- not a second implementation")
    assert re.search(r'conductorManaged\.find\(|doneConductorTasks\.find\(', around), (
        "the handler must resolve the deep-linked id against the task "
        "lists this page already loads (conductorManaged / "
        "doneConductorTasks), not invent a third source of task data")


def test_the_pipeline_functions_stay_singly_defined():
    """The likely misfire named on this ticket: build a second animated
    instance/playback view instead of reusing the one thing that already
    works. Guards that the green step does not fork these definitions."""
    page = _read("pages", "WorkflowsPage.tsx")
    assert len(re.findall(r'const\s+openConductorInstance\s*=', page)) == 1
    assert len(re.findall(r'const\s+replayHistoricalRun\s*=', page)) == 1


# ---------------------------------------------------------------------------
# 3. The opened instance links back to the task's own detail page -- today
#    the only chip on an open instance exits to the live workflow, never to
#    /tasks/{id} (owner-verified live, 2026-08-25).
# ---------------------------------------------------------------------------

def test_an_open_instance_links_back_to_its_own_task_detail_page():
    page = _read("pages", "WorkflowsPage.tsx")
    needle = 'title="Historical run selected'
    assert needle in page, (
        "the exit-to-live chip this test anchors on is missing -- has "
        "leaveHistoricalReplay moved or been renamed?")
    idx = page.index(needle)
    window = page[max(0, idx - 200):idx + 1200]
    assert re.search(r'to=\{`/tasks/\$\{[^}]*conductorTask\?\.id\}`\}', window), (
        "an open conductor instance has no link back to /tasks/<id> -- the "
        "exit-to-live chip (leaveHistoricalReplay) is the ONLY affordance "
        "today, and it does not navigate to the task page")
    assert 'aria-label="Open task detail"' in window, (
        "the back-to-task-page link must be a real, labeled affordance, "
        "not merely reachable by accident")
