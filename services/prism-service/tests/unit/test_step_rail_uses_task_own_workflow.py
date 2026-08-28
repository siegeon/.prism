"""UI contract test: a task's own workflow renders ITS OWN FSM steps in the
Implementation rail, not always the "implement" workflow's 10-step SDLC.

The PRISM SPA has NO JS test runner, so UI behavior is pinned by asserting
the ACTUAL web source (TSX/TS) -- the same pattern as
tests/unit/test_conductor_page_animated_cleanup_ui.py and
tests/unit/test_step_rail_bar_scale_is_total.py.

BUG (owner, live): a task driven by a NAMED workflow other than "implement"
(e.g. "promote_to_law", "triage", "align_language", "quickfix") never
rendered its own steps in the rail -- it always showed the "implement"
workflow's 10-step conductor SDLC (or a bare "intake" placeholder). Owner:
"where are all of the steps from the bots flow?" and "i thought promote to
law bot has different fsm states, why would it look the same."

ROOT CAUSE: lib/useWorkflowDef.ts's useWorkflowSteps() took no argument and
resolved steps from ONLY the top-level `def.steps` field (GET /api/workflows'
"implement"/conductor entry), cached forever for the tab, regardless of
which task/workflow was being viewed.

FIX: useWorkflowSteps(workflow?) resolves steps FOR THAT WORKFLOW out of the
SAME cached `GET /api/workflows` response's `def.workflows[]` catalog (each
entry carries its own `.steps`), mirroring the backend's
models.workflow.steps_for() alias join (models.task.WORKFLOW_ALIASES:
"implement" -> the "conductor" catalog entry; every other named workflow
already IS its own catalog id). Threaded through StepRail, SdlcProgress,
PlanView's ConductorInfo, and TaskDetailPage's conductor object literal.
"""

from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).resolve()
_SRC = _HERE.parent.parent.parent / "prism_service" / "web" / "src"
_USE_WORKFLOW_DEF = _SRC / "lib" / "useWorkflowDef.ts"
_STEP_RAIL = _SRC / "components" / "conductor" / "StepRail.tsx"
_SDLC_PROGRESS = _SRC / "components" / "conductor" / "SdlcProgress.tsx"
_PLAN_VIEW = _SRC / "components" / "plan" / "PlanView.tsx"
_TASK_DETAIL_PAGE = _SRC / "pages" / "TaskDetailPage.tsx"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_use_workflow_steps_accepts_a_workflow_and_resolves_from_def_workflows():
    src = _read(_USE_WORKFLOW_DEF)

    # The hook's own signature must accept a workflow argument.
    assert "export function useWorkflowSteps(workflow?: string): RailStep[] {" in src, \
        "useWorkflowSteps must accept an optional `workflow` parameter"

    # Its resolution logic must consult def.workflows (the per-workflow
    # catalog), not just the bare top-level def.steps -- the actual bug.
    assert "def.workflows" in src, \
        "steps resolution must reference def.workflows (the per-workflow catalog), not only the top-level def.steps"
    assert re_find_catalog_lookup(src), \
        "must look up a catalog entry by id out of def.workflows (e.g. def.workflows?.find(...))"

    # A safe fallback to the top-level steps must remain for a blank/unknown
    # workflow value or an older service with no `workflows` field --
    # never blank, never throws.
    assert "entry ? entry.steps : def.steps" in src, \
        "must fall back to the top-level def.steps when no matching catalog entry exists"


def re_find_catalog_lookup(src: str) -> bool:
    import re
    return re.search(r"def\.workflows\?\.find\(", src) is not None


def test_use_workflow_steps_mirrors_the_backend_implement_to_conductor_alias():
    """models.task.WORKFLOW_ALIASES maps "implement" -> the "conductor"
    catalog entry; every other named workflow (triage, align_language,
    promote_to_law, quickfix) already stores the same string as its own
    catalog id. The frontend resolution must mirror this exact join."""
    src = _read(_USE_WORKFLOW_DEF)

    assert '{ implement: "conductor" }' in src, \
        "must mirror the backend's models.task.WORKFLOW_ALIASES (implement -> conductor)"


def test_step_rail_accepts_and_threads_workflow():
    src = _read(_STEP_RAIL)

    assert "workflow?: string;" in src, \
        "StepRail's props type must include an optional `workflow` field"
    assert "useWorkflowSteps(workflow)" in src, \
        "StepRail must pass its `workflow` prop into useWorkflowSteps"


def test_sdlc_progress_accepts_and_threads_workflow():
    src = _read(_SDLC_PROGRESS)

    assert "workflow?: string;" in src, \
        "SdlcProgress's props type must include an optional `workflow` field"
    assert "useWorkflowSteps(workflow)" in src, \
        "SdlcProgress must pass its `workflow` prop into useWorkflowSteps"


def test_conductor_info_carries_workflow_and_both_call_sites_pass_it():
    src = _read(_PLAN_VIEW)

    # ConductorInfo (the shape PlanView receives per task) must carry the
    # task's own workflow so it can be threaded to the rail.
    conductor_info_idx = src.index("export type ConductorInfo = {")
    conductor_info_end = src.index("};", conductor_info_idx)
    conductor_info_src = src[conductor_info_idx:conductor_info_end]
    assert "workflow?: string;" in conductor_info_src, \
        "ConductorInfo must carry an optional `workflow` field"

    # Both the SdlcProgress and StepRail JSX call sites in PlanView must
    # pass the task's workflow through.
    assert "workflow={c.workflow}" in src, \
        "PlanView must pass workflow={c.workflow} into at least one rail call site"
    assert src.count("workflow={c.workflow}") >= 2, \
        "PlanView must pass workflow={c.workflow} into BOTH the SdlcProgress and StepRail call sites"


def test_task_detail_page_conductor_literal_carries_workflow():
    src = _read(_TASK_DETAIL_PAGE)

    assert "workflow?: string;" in src, \
        "the task type must carry an optional `workflow` field"

    conductor_literal_idx = src.index("conductor={conductorOn ? {")
    conductor_literal_end = src.index("} : null}", conductor_literal_idx)
    conductor_literal_src = src[conductor_literal_idx:conductor_literal_end]
    assert "workflow: task.workflow," in conductor_literal_src, \
        "the conductor object literal must pass workflow: task.workflow through to PlanView's ConductorInfo"
