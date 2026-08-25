"""Task b837bc98: a triage workflow with explicit behavior.

Pins the NAMED workflow registry (models/workflow.py WORKFLOWS/steps_for)
and the catalog half (api/workflows.py GET /api/workflows) for a second,
deliberately short workflow beside the 10-step implement/conductor SDLC:
intake -> classify (role sm: bucket Open/Monitoring/Resolved/Dropped with a
one-line reason) -> decide (the single human/owner gate) -> done.

SCOPE NOTE (HARD STOP, same ticket): the per-task step WALK that actually
MOVES a task forward (ConductorService._workflow_steps/_step_by_id/
advance_task in services/conductor_service.py -- explicitly "No per-task
override of WORKFLOW_STEPS: every task walks the default sequence from
models.workflow") is a control-plane POLICY file
(services.control_plane.POLICY_FILES) outside this ticket's allowed_files,
and untouched here. So a real task driven through api/conductor_flow.py's
flow_next/flow_report today ALWAYS walks the implement sequence regardless
of task.workflow -- advance_task never consults task.workflow at all.
test_a_simulated_triage_walk_visits_intake_classify_decide_done_never_draft_story
below demonstrates the MODEL-LEVEL contract steps_for() provides for that
future walk (a follow-up policy-change slice on conductor_service.py), by
manually indexing steps_for(task.workflow) the same way advance_task indexes
WORKFLOW_STEPS today -- it does not exercise advance_task itself, since
advance_task cannot yet honor a per-task workflow.
"""

from __future__ import annotations

import re
import types
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
_WORKFLOWS_PAGE = (
    _SERVICE_ROOT / "prism_service" / "web" / "src" / "pages" / "WorkflowsPage.tsx"
)


# ── WORKFLOWS registry + steps_for ──────────────────────────────────────

def test_workflows_has_triage_with_four_steps_in_order_and_one_gate():
    from prism_service.models.workflow import WORKFLOWS

    assert "triage" in WORKFLOWS
    ids = [s["id"] for s in WORKFLOWS["triage"]]
    assert ids == ["intake", "classify", "decide", "done"]

    gates = [s["id"] for s in WORKFLOWS["triage"] if s["type"] == "gate"]
    assert gates == ["decide"], (
        f"decide must be the ONLY gate in the triage workflow, got {gates}")


def test_classify_is_owned_by_the_sm_role():
    from prism_service.models.workflow import WORKFLOWS

    classify = next(s for s in WORKFLOWS["triage"] if s["id"] == "classify")
    assert classify["agent"] == "sm"
    assert classify["type"] == "agent"


def test_steps_for_triage_returns_the_triage_steps():
    from prism_service.models.workflow import WORKFLOWS, steps_for

    assert steps_for("triage") == WORKFLOWS["triage"]


def test_steps_for_implement_returns_workflow_steps_unchanged():
    from prism_service.models.workflow import WORKFLOW_STEPS, steps_for

    assert steps_for("implement") == WORKFLOW_STEPS


def test_steps_for_unknown_falls_back_to_implement():
    from prism_service.models.workflow import WORKFLOW_STEPS, steps_for

    assert steps_for("carrier-pigeon") == WORKFLOW_STEPS
    assert steps_for("") == WORKFLOW_STEPS
    assert steps_for(None) == WORKFLOW_STEPS


def test_steps_for_resolves_a_catalog_id_via_workflow_aliases():
    """steps_for reuses models.task.WORKFLOW_ALIASES ("implement" ->
    "conductor") so the catalog id itself also resolves correctly, the
    same join api/workflows.py's _task_count_by_workflow performs."""
    from prism_service.models.workflow import WORKFLOW_STEPS, steps_for

    assert steps_for("conductor") == WORKFLOW_STEPS


# ── A default task's own sequence is byte-for-byte unchanged ────────────

def test_a_default_tasks_step_sequence_is_unchanged():
    from prism_service.models.task import DEFAULT_WORKFLOW
    from prism_service.models.workflow import WORKFLOW_STEPS, steps_for

    assert DEFAULT_WORKFLOW == "implement"
    ids = [s["id"] for s in steps_for(DEFAULT_WORKFLOW)]
    assert ids == [s["id"] for s in WORKFLOW_STEPS]
    assert len(ids) == 10


# ── Simulated walk (see SCOPE NOTE at module top) ────────────────────────

def _walk_by_index(workflow: str) -> list[str]:
    """Mirrors ConductorService._step_index/advance_task's own indexing
    logic (services/conductor_service.py:1559-1571), except resolving the
    step list through steps_for(workflow) instead of the hardcoded global
    WORKFLOW_STEPS -- exactly the one-line change advance_task would need
    once it is allowed to read task.workflow (blocked this ticket, see
    SCOPE NOTE)."""
    from prism_service.models.workflow import steps_for

    steps = steps_for(workflow)
    ids = [s["id"] for s in steps]
    visited = []
    index = -1
    while index < len(ids) - 1:
        index += 1
        visited.append(ids[index])
    return visited


def test_a_simulated_triage_walk_visits_intake_classify_decide_done_never_draft_story():
    visited = _walk_by_index("triage")
    assert visited == ["intake", "classify", "decide", "done"]
    assert "draft_story" not in visited


def test_a_simulated_implement_walk_is_the_full_ten_step_sdlc():
    from prism_service.models.workflow import WORKFLOW_STEPS

    visited = _walk_by_index("implement")
    assert visited == [s["id"] for s in WORKFLOW_STEPS]


# ── GET /api/workflows: triage is a first-class catalog entry ───────────

def _mk_task(**over):
    from prism_service.models.task import Task

    base = dict(
        id="t-1", title="A task", description="", status="pending",
        priority=5, assigned_agent="", updated_at="2026-08-25T00:00:00Z",
        workflow_step="", gate_state="none", parent_id="", tags=[],
    )
    base.update(over)
    return Task(**base)


class _Svc:
    """Minimal task_svc stand-in — the endpoint only ever LISTS."""

    def __init__(self, tasks):
        self.tasks = list(tasks)

    def list(self, status=None, assigned_agent=None, tag=None,
             story_file=None, parent_id=None, id=None):
        return list(self.tasks)


def _scripted_validation(project="prism"):
    return {
        "id": "validation", "name": "Build and test",
        "description": f"{project} validation", "project_type": "python+react",
        "steps": [], "bots": [], "occupancy": {},
    }


def test_catalog_lists_triage_beside_conductor_with_its_four_steps(monkeypatch):
    from prism_service.api import workflows as workflows_api

    monkeypatch.setattr(workflows_api, "get_project",
                        lambda p: types.SimpleNamespace(task_svc=_Svc([])))
    monkeypatch.setattr(workflows_api, "_project_validation_workflow",
                        _scripted_validation)
    monkeypatch.setattr(workflows_api, "_conductor_behavior_workflows",
                        lambda project: [])

    body = workflows_api.get_workflows("prism")

    by_id = {w["id"]: w for w in body["workflows"]}
    assert "triage" in by_id, f"catalog entries: {list(by_id)}"
    triage = by_id["triage"]
    assert [s["id"] for s in triage["steps"]] == [
        "intake", "classify", "decide", "done"]
    # triage is a first-class root entry, a sibling of conductor -- not one
    # of conductor's own nested capabilities.
    assert "parent_id" not in triage
    assert "conductor" in by_id and "parent_id" not in by_id["conductor"]


def test_triage_task_count_counts_tasks_whose_workflow_is_triage(monkeypatch):
    from prism_service.api import workflows as workflows_api

    tasks = [
        _mk_task(id="t-1", workflow="triage", status="pending"),
        _mk_task(id="t-2", workflow="triage", status="in_progress"),
        _mk_task(id="t-3", workflow="triage", status="done"),  # excluded
        _mk_task(id="t-4", workflow="implement", status="pending"),
    ]
    monkeypatch.setattr(workflows_api, "get_project",
                        lambda p: types.SimpleNamespace(task_svc=_Svc(tasks)))
    monkeypatch.setattr(workflows_api, "_project_validation_workflow",
                        _scripted_validation)
    monkeypatch.setattr(workflows_api, "_conductor_behavior_workflows",
                        lambda project: [])

    body = workflows_api.get_workflows("prism")
    by_id = {w["id"]: w for w in body["workflows"]}
    assert by_id["triage"]["task_count"] == 2
    assert by_id["conductor"]["task_count"] == 1


# ── WorkflowsPage.tsx: catalog entries render generically ───────────────

def _page() -> str:
    return _WORKFLOWS_PAGE.read_text(encoding="utf-8")


def test_directory_renders_catalog_entries_generically_not_a_hardcoded_list():
    """The RENDERED JSX map over the live catalog array -- never a
    per-workflow branch or a comment claiming genericity. Parses the
    literal map expression so a comment above it can never satisfy this."""
    page = _page()
    generic_map = re.search(
        r'\{workflows\.filter\(\(workflow\) => !workflow\.parent_id\)\.map\(\(workflow\) => \{',
        page,
    )
    assert generic_map, (
        "WorkflowsPage.tsx must render root catalog entries via a generic "
        "workflows.filter(...).map(...) — a hardcoded per-workflow branch "
        "would silently omit triage")
    # And the row itself is driven off the workflow's own fields, not a
    # literal "conductor"/"validation" name.
    assert '{workflow.name}' in page
    assert '{workflow.steps.length}' in page
