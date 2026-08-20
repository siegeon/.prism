"""Workflows API — the conductor FSM, its bots, and live occupancy as ONE
assembled read view.

In PRISM a workflow IS a bot: an FSM that agentically interacts with the
conductor's FSM. Both halves already exist, so this endpoint mints NOTHING
— no table, no persisted model, no parallel step definition. Every field is
read straight off an existing source of truth:

    steps      models/workflow.py WORKFLOW_STEPS  — the conductor FSM
    persona    models/roles.py STEP_ROLES         — who OWNS each step
    bots       services/context_builder.py ROLE_CARDS — the role briefs
    occupancy  the project's existing task rows   — task.workflow_step

`persona` is deliberately NOT a copy of the FSM row's `agent`. A gate has
agent=None because nobody AUTHORS a gate; the Steward ADJUDICATES it as the
independent reviewer (models/roles.py STEP_ROLES, enforced in
conductor_service.gate_decide). Resolving through role_for_step is what
lets the UI name that actor on a gate row.

This is also the single source of the step ORDERING for the SPA:
lib/workflowChips.ts used to carry a hand-maintained duplicate of
WORKFLOW_STEPS that nothing kept in sync. The rail now fetches it here.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from prism_service.models import roles
from prism_service.models.workflow import WORKFLOW_STEPS
from prism_service.project_context import get_project
from prism_service.services.context_builder import ROLE_CARDS, ContextBuilder
from prism_service.services import sqlite_db

try:
    # Only available when the process is launched under
    # `opentelemetry-instrument` (the AOS AppHost's `uv run --with
    # opentelemetry-distro ...` invocation) -- not a formal project
    # dependency, so plain test/dev runs must still import this module
    # cleanly. A no-op tracer's spans are simply never exported.
    from opentelemetry import trace
    _tracer = trace.get_tracer(__name__)
except ImportError:
    from contextlib import contextmanager

    class _NoOpSpan:
        def set_attribute(self, *a, **kw):
            pass

    class _NoOpTracer:
        @contextmanager
        def start_as_current_span(self, *a, **kw):
            yield _NoOpSpan()

    _tracer = _NoOpTracer()

router = APIRouter()

# The four cards an agent can wear on this board. sm/qa/dev are the canonical
# conductor roles; `architect` is a context_builder-only brief that folds to
# sm at routing time (models/roles.py ROLE_ALIASES) but is still a distinct
# hat worth drawing, so it is listed explicitly rather than derived.
BOT_IDS = ("sm", "qa", "dev", "architect")

STEP_ACTIONS = {
    "review_previous_notes": ("Existing project memory and source", "Review prior decisions and ground every premise in evidence", "A cited premise report"),
    "draft_story": ("Grounded premises and the requested outcome", "Author requirements and acceptance criteria with observable oracles", "A reviewable story"),
    "story_gate": ("The authored story", "An independent Steward adjudicates story completeness", "Approved story or a concrete refusal reason"),
    "verify_plan": ("The approved story", "Check that the implementation plan covers every acceptance criterion", "A coverage-backed plan"),
    "plan_gate": ("The verified plan", "An independent Steward adjudicates plan coverage", "Approved plan or a concrete refusal reason"),
    "write_failing_tests": ("Acceptance criteria and plan", "Write traced tests that fail for the missing behavior", "Reproducible red evidence"),
    "red_gate": ("Failing test evidence", "An independent Steward confirms the failure is relevant and honest", "Approved red state or a refusal reason"),
    "implement_tasks": ("Approved plan and failing tests", "Make the smallest source change that turns the tests green", "Implemented source changes"),
    "verify_green_state": ("Implementation and its verification commands", "Run the real verification suite and inspect the resulting evidence", "Full green evidence"),
    "green_gate": ("Green evidence and acceptance oracles", "An independent Steward decides whether the requested outcome is actually complete", "Accepted outcome or follow-up work"),
}

AOS_WORKFLOWS_URL = os.environ.get("AOS_WORKFLOWS_URL", "http://127.0.0.1:5273").rstrip("/")


class ScriptedStep(BaseModel):
    id: str
    title: str
    purpose: str
    runner: str
    command: str
    working_directory: str = Field(alias="workingDirectory")
    timeout_seconds: int = Field(alias="timeoutSeconds")
    depends_on: list[str] = Field(alias="dependsOn")
    success: str
    script_path: str = Field(default="", alias="scriptPath")
    script_language: str = Field(default="shell", alias="scriptLanguage")
    script_source: str = Field(default="", alias="scriptSource")
    average_duration_seconds: float | None = Field(default=None, alias="averageDurationSeconds")
    duration_sample_count: int = Field(default=0, alias="durationSampleCount")
    behavior_version: int = Field(default=1, alias="behaviorVersion")


class ProjectWorkflow(BaseModel):
    id: str
    name: str
    description: str
    project: str
    project_type: str = Field(alias="projectType")
    steps: list[ScriptedStep]
    behavior_version: int = Field(default=1, alias="behaviorVersion")


class WorkflowFixRequest(BaseModel):
    """Typed intent at the repair boundary.

    Callers identify the failed execution; PRISM re-reads the authoritative
    result and step contract.  Failure output, commands, and paths are never
    trusted from the browser or an agent.
    """

    model_config = ConfigDict(extra="forbid")

    instance_id: str = Field(min_length=1)
    step_id: str = Field(min_length=1)


class WorkflowSourceSnapshot(BaseModel):
    """Immutable source identity persisted with a validation run."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(alias="schemaVersion", ge=1)
    repository_root: str = Field(alias="repositoryRoot", min_length=1)
    base_commit: str = Field(alias="baseCommit", pattern=r"^[0-9a-f]{40}$")
    snapshot_commit: str = Field(alias="snapshotCommit", pattern=r"^[0-9a-f]{40}$")
    tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    dirty: bool
    included_untracked: int = Field(alias="includedUntracked", ge=0)
    excluded_runtime: int = Field(default=0, alias="excludedRuntime", ge=0)


class StepValidationFailure(BaseModel):
    check: str = Field(min_length=1)
    message: str = ""


class ConductorStepValidation(BaseModel):
    """Caller-facing ontology for validation of one conductor workflow step."""

    kind: Literal["conductor.step_validation"] = "conductor.step_validation"
    workflow_id: str
    instance_id: str
    step_id: str
    outcome: Literal["failed", "timed_out"]
    summary: str
    exit_code: int | None = None
    command: str
    working_directory: str
    success_contract: str
    failures: list[StepValidationFailure]
    evidence_uri: str
    raw_output_chars: int
    source_snapshot: WorkflowSourceSnapshot


def _workflow_engine_json(
    path: str, method: str = "GET", body: dict | None = None,
) -> dict:
    encoded = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if encoded else {}
    request = Request(
        f"{AOS_WORKFLOWS_URL}{path}", data=encoded,
        headers=headers, method=method,
    )
    try:
        with urlopen(request, timeout=2.0) as response:  # noqa: S310 - fixed local AOS service
            return json.loads(response.read())
    except HTTPError as exc:
        raise HTTPException(exc.code, f"workflow engine refused request: {exc.reason}") from exc
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise HTTPException(503, f"workflow engine unavailable: {exc}") from exc


def _project_validation_workflow(project: str) -> dict:
    definition = ProjectWorkflow.model_validate(
        _workflow_engine_json(f"/workflows/definitions/{project}")
    )
    persona_for = {"test": "qa", "build": "dev"}
    steps = []
    for scripted in definition.steps:
        persona = persona_for.get(scripted.id, "dev")
        steps.append({
            "id": scripted.id,
            "agent": persona,
            "type": "agent",
            "validation": scripted.success,
            "persona": persona,
            "persona_label": _persona_label(persona),
            "purpose": scripted.purpose,
            "input": "Project source plus the checked-in scripted step contract",
            "action": scripted.command,
            "output": "Captured stdout, stderr, exit code, duration, and status",
            "execution": "scripted",
            "runner": scripted.runner,
            "command": scripted.command,
            "working_directory": scripted.working_directory,
            "timeout_seconds": scripted.timeout_seconds,
            "depends_on": scripted.depends_on,
            "script_path": scripted.script_path,
            "script_language": scripted.script_language,
            "script_source": scripted.script_source,
            "average_duration_seconds": scripted.average_duration_seconds,
            "duration_sample_count": scripted.duration_sample_count,
        })
    return {
        "id": definition.id,
        "name": definition.name,
        "description": definition.description,
        "project_type": definition.project_type,
        "steps": steps,
        "bots": [],
        "occupancy": {step["id"]: 0 for step in steps},
    }


def _behavior_file(project: str, workflow_id: str) -> Path:
    from prism_service.services.claude_transcripts import _project_source_path
    configured = Path(_project_source_path(project))
    fallback = Path.home() / "projects" / project
    root = configured if configured.is_absolute() and configured.exists() else fallback
    if not root.exists():
        raise HTTPException(404, f"project source path is not configured: {project}")
    return root / ".prism" / "behaviors" / f"{workflow_id}.json"


def get_workflow_behavior(project: str, behavior_path: str = "validation") -> dict:
    workflow_id, _, step_id = behavior_path.strip("/").partition("/")
    if workflow_id != "validation":
        raise HTTPException(404, "unknown workflow behavior")
    definition = ProjectWorkflow.model_validate(
        _workflow_engine_json(f"/workflows/definitions/{project}"))
    if not step_id:
        return {"path": workflow_id, "behavior": definition.model_dump(by_alias=True)}
    step = next((item for item in definition.steps if item.id == step_id), None)
    if step is None:
        raise HTTPException(404, "unknown child behavior")
    return {"path": f"{workflow_id}/{step_id}", "parent": workflow_id,
            "behavior": step.model_dump(by_alias=True),
            "parentVersion": definition.behavior_version}


def provide_workflow_behavior(
    project: str, behavior_path: str, expected_version: int, behavior: dict,
) -> dict:
    """Atomically provide a new child revision; siblings retain lineage."""
    workflow_id, separator, step_id = behavior_path.strip("/").partition("/")
    if workflow_id != "validation" or not separator or not step_id:
        raise HTTPException(400, "provide a child path such as validation/test")
    current = ProjectWorkflow.model_validate(
        _workflow_engine_json(f"/workflows/definitions/{project}"))
    index = next((i for i, item in enumerate(current.steps) if item.id == step_id), None)
    if index is None:
        raise HTTPException(404, "unknown child behavior")
    prior = current.steps[index]
    if prior.behavior_version != expected_version:
        raise HTTPException(409, f"behavior revision changed: expected {expected_version}, current {prior.behavior_version}")
    allowed = {"title", "purpose", "runner", "command", "workingDirectory",
               "timeoutSeconds", "dependsOn", "success", "scriptPath",
               "scriptLanguage", "scriptSource"}
    unknown = set(behavior) - allowed - {"id", "behaviorVersion"}
    if unknown:
        raise HTTPException(422, f"unsupported behavior fields: {', '.join(sorted(unknown))}")
    if behavior.get("id", step_id) != step_id:
        raise HTTPException(409, "provided behavior id does not match its path")
    merged = prior.model_dump(by_alias=True)
    merged.update({key: value for key, value in behavior.items() if key in allowed})
    merged["behaviorVersion"] = prior.behavior_version + 1
    steps = [item.model_dump(by_alias=True) for item in current.steps]
    steps[index] = ScriptedStep.model_validate(merged).model_dump(by_alias=True)
    document = current.model_dump(by_alias=True)
    document["steps"] = steps
    document["behaviorVersion"] = current.behavior_version + 1
    validated = ProjectWorkflow.model_validate(document).model_dump(by_alias=True)
    destination = _behavior_file(project, workflow_id)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(validated, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, destination)
    return {"ok": True, "path": f"{workflow_id}/{step_id}",
            "version": merged["behaviorVersion"],
            "parentVersion": document["behaviorVersion"],
            "historyReset": [f"{workflow_id}/{step_id}", workflow_id],
            "preservedSiblingHistory": [item.id for item in current.steps if item.id != step_id]}


def _validation_failures(output: str) -> list[StepValidationFailure]:
    failures = []
    for line in output.splitlines():
        if not line.startswith(("FAILED ", "ERROR ")):
            continue
        _, detail = line.split(" ", 1)
        check, separator, message = detail.partition(" - ")
        failures.append(StepValidationFailure(
            check=check.strip(), message=message.strip() if separator else "",
        ))
        if len(failures) == 20:
            break
    if failures:
        return failures
    headings = re.findall(r"_{5,}\s*(.*?)\s*_{5,}", output)
    return [StepValidationFailure(check=item.strip()) for item in headings[-20:]]


def _validation_summary(output: str, outcome: str, failures: int) -> str:
    summaries = re.findall(
        r"^=+\s*(.+?(?:failed|error|timed out).+?)\s*=+$",
        output, flags=re.MULTILINE | re.IGNORECASE,
    )
    if summaries:
        return summaries[-1].strip()
    return f"{failures} failed check(s)" if failures else f"Step {outcome}"


def queue_workflow_fix(project: str, workflow_id: str, request: WorkflowFixRequest) -> dict:
    """Turn a real failed step into governed work for the PRISM agent."""
    if workflow_id != "validation":
        raise HTTPException(409, "only scripted project workflows can request a fix")
    run = _workflow_engine_json(f"/workflows/instances/{request.instance_id}")
    run_data = run.get("data", {})
    if str(run_data.get("project", "")).lower() != project.lower():
        raise HTTPException(409, "workflow run does not belong to this project")
    try:
        snapshot = WorkflowSourceSnapshot.model_validate(run_data.get("sourceSnapshot"))
    except Exception as exc:
        raise HTTPException(
            409, "workflow run has no reconstructable source snapshot",
        ) from exc
    from prism_service.services.claude_transcripts import _project_source_path
    configured = Path(_project_source_path(project))
    fallback = Path.home() / "projects" / project
    root = configured if configured.is_absolute() and configured.exists() else fallback
    if Path(snapshot.repository_root).resolve() != root.resolve():
        raise HTTPException(409, "workflow source snapshot belongs to another repository")
    from prism_service.services.source_snapshot import validate_source_snapshot
    try:
        validate_source_snapshot(root, snapshot.snapshot_commit, snapshot.tree)
    except RuntimeError as exc:
        raise HTTPException(409, f"workflow source snapshot is unavailable: {exc}") from exc
    definition = ProjectWorkflow.model_validate(
        _workflow_engine_json(f"/workflows/definitions/{project}")
    )
    step = next((item for item in definition.steps if item.id == request.step_id), None)
    if step is None:
        raise HTTPException(404, "workflow step not found")
    result_key = "tests" if request.step_id == "test" else request.step_id
    result = run.get("data", {}).get(result_key, {})
    if result.get("status") not in {"failed", "timed_out"}:
        raise HTTPException(409, "a fix can only be requested for a failed step")

    output = str(result.get("output", ""))
    failures = _validation_failures(output)
    validation = ConductorStepValidation(
        workflow_id=workflow_id,
        instance_id=request.instance_id,
        step_id=step.id,
        outcome=result["status"],
        summary=_validation_summary(output, result["status"], len(failures)),
        exit_code=result.get("exitCode"),
        command=step.command,
        working_directory=step.working_directory,
        success_contract=step.success,
        failures=failures,
        evidence_uri=f"/workflows/instances/{request.instance_id}",
        raw_output_chars=len(output),
        source_snapshot=snapshot,
    )
    task = get_project(project).task_svc.create(
        title=f"Fix {definition.name}: {step.title}",
        description=(
            "Repair this failed conductor workflow-step validation without "
            "weakening its success contract. Raw process output remains at "
            "the evidence URI and is not the task contract.\n\n"
            "```json\n"
            f"{validation.model_dump_json(by_alias=True, indent=2)}\n"
            "```"
        ),
        priority=10,
        tags=["workflow-fix", "agent-managed", step.id],
        assigned_agent="dev",
        oracle=f"The scripted {step.id} step exits successfully for workflow validation",
        proof_type="test",
        verify=[step.command],
        stop_if=[
            "The failure cannot be reproduced",
            "The repair requires changing the workflow's success contract",
            "The required change is outside this project",
        ],
    )
    from prism_service.services import task_workspace
    try:
        workspace = task_workspace.ensure_workspace(
            task.id, repo_root=str(root), base_ref=snapshot.snapshot_commit,
        )
    except RuntimeError as exc:
        get_project(project).task_svc.update(
            task.id, status="blocked",
            blocked_reason=f"source snapshot workspace unavailable: {exc}",
        )
        raise HTTPException(503, f"repair workspace unavailable: {exc}") from exc
    return {
        "queued": True,
        "task_id": task.id,
        "status": task.status,
        "validation": validation.model_dump(by_alias=True),
        "source_snapshot": snapshot.model_dump(by_alias=True),
        "workspace": workspace,
        "next": "A PRISM agent can claim this task with conductor_work; all normal gates still apply.",
    }


def _persona_label(role_id: str) -> str:
    """Human label for a role id. Canonical roles carry their own label in
    the registry; `architect` has no Role row (it aliases to sm) so it falls
    back to its own name rather than being mislabelled "Steward"."""
    role = roles.ROLES.get(role_id)
    return role.label if role else role_id.capitalize()


def _occupancy(project: str, step_ids: list[str]) -> dict[str, int]:
    """How many tasks are standing at each step RIGHT NOW, per project.

    Keyed by the FSM's own steps only, and seeded to 0 so the renderer can
    read a count directly instead of branching on presence. A done task is
    not standing anywhere, and a legacy row parked at a step id the FSM no
    longer contains must not invent a node the canvas cannot draw.
    """
    try:
        svc = get_project(project).task_svc
    except Exception as exc:
        raise HTTPException(404, f"unknown project: {project}: {exc}")

    counts = {sid: 0 for sid in step_ids}
    db_path = getattr(svc, "_db_path", None)
    if db_path:
        # This is a UI poll, not a reason to queue behind a long task-store
        # writer. Use an independent read-only connection with a short bound;
        # stale zero occupancy is preferable to an inspector that never opens.
        try:
            with sqlite_db.connect(
                f"file:{db_path}?mode=ro", uri=True, timeout=0.25,
            ) as conn:
                rows = conn.execute(
                    "SELECT workflow_step, COUNT(*) FROM tasks "
                    "WHERE status <> 'done' GROUP BY workflow_step"
                ).fetchall()
            for step, count in rows:
                if step in counts:
                    counts[step] = int(count)
            return counts
        except (sqlite3.Error, OSError):
            return counts

    for task in svc.list():
        if getattr(task, "status", "") == "done":
            continue
        step = getattr(task, "workflow_step", "") or ""
        if step in counts:
            counts[step] += 1
    return counts


def _conductor_behavior_workflows(project: str) -> list[dict]:
    """Each of the conductor bot's AosWorkflows Behaviors, as its OWN
    catalog entry nested under `conductor` -- not one synthetic wrapper node
    whose fake "steps" were just the behavior ids. Bot [1] uses FSM [1..*],
    FSM [1] has Behavior [0..*] (see the same ontology comment in
    AosWorkflows' Program.cs); a behavior IS a flow, so it gets a real
    catalog entry disclosing its OWN real steps (e.g. land's push/open-pr),
    not a fake single node standing in for the whole thing.

    These run OUTSIDE this process entirely -- AosWorkflows (WorkflowCore,
    separate service) owns their state, never prism-service. A missing bot
    definition, an unreachable engine, or a stale build without the /bots
    route are all the same case here: nothing to show yet, not an error the
    whole page should break on.
    """
    from prism_service.services.claude_transcripts import _project_source_path

    configured = Path(_project_source_path(project))
    fallback = Path.home() / "projects" / project
    root = configured if configured.is_absolute() and configured.exists() else fallback
    if not root.exists():
        return []
    encoded_root = quote(str(root))
    try:
        bot = _workflow_engine_json(f"/workflows/bots/conductor?repoPath={encoded_root}")
    except HTTPException:
        return []

    entries = []
    for fsm in bot.get("fsms") or bot.get("Fsms") or []:
        fsm_id = fsm.get("fsmId") or fsm.get("FsmId")
        for behavior_id in fsm.get("behaviorIds") or fsm.get("BehaviorIds") or []:
            try:
                behavior = _workflow_engine_json(
                    f"/workflows/bots/conductor/behaviors/{behavior_id}?repoPath={encoded_root}")
            except HTTPException:
                continue
            raw_steps = behavior.get("steps") or behavior.get("Steps") or []
            steps = []
            for i, step in enumerate(raw_steps):
                step_id = step.get("id") or step.get("Id")
                kind = step.get("kind") or step.get("Kind") or "shell"
                command = step.get("command") or step.get("Command") or ""
                url = step.get("url") or step.get("Url") or ""
                steps.append({
                    "id": step_id,
                    "agent": "conductor",
                    "type": "behavior",
                    "validation": "exit_code == 0",
                    "persona": "conductor",
                    "persona_label": "Conductor",
                    "purpose": step_id.replace("-", " ").replace("_", " ").capitalize(),
                    "input": "Previous step's result" if i else "The conductor bot's own repo checkout",
                    "action": command or url,
                    "output": "Captured stdout, stderr, and exit code" if kind == "shell"
                        else "HTTP response body and status",
                    # Deliberately "connected", not "scripted": "scripted"
                    # arms the canvas's "Run workflow" button, which posts to
                    # /{workflow_id}/runs -- a route hardcoded to the
                    # validation workflow only. Wiring that dispatch through
                    # to AosWorkflows' POST /workflows/bots/... is real,
                    # separate follow-up work, not implied by fixing the
                    # directory hierarchy.
                    "execution": "connected",
                    "runner": "process" if kind == "shell" else "http",
                    "command": command,
                    "working_directory": step.get("workingDirectory") or step.get("WorkingDirectory") or "",
                    "timeout_seconds": step.get("timeoutSeconds") or step.get("TimeoutSeconds") or 300,
                    "depends_on": [raw_steps[i - 1].get("id") or raw_steps[i - 1].get("Id")] if i else [],
                })
            entries.append({
                "id": behavior_id,
                "parent_id": "conductor",
                "name": behavior.get("name") or behavior.get("Name") or behavior_id.replace("-", " ").title(),
                "description": f"Runs on the '{fsm_id}' fsm, executed by AosWorkflows",
                "steps": steps,
                "bots": [],
                "occupancy": {step["id"]: 0 for step in steps},
            })
    return entries


@router.get("")
def get_workflows(project: str = Query("default")) -> dict:
    """The conductor FSM, the bots that drive it, and who is standing where."""
    steps = []
    for step in WORKFLOW_STEPS:
        persona = roles.role_for_step(step["id"])
        steps.append({
            "id": step["id"],
            "agent": step["agent"],
            "type": step["type"],
            "validation": step["validation"],
            "persona": persona,
            "persona_label": _persona_label(persona),
            "purpose": step["id"].replace("_", " ").capitalize(),
            "input": STEP_ACTIONS[step["id"]][0],
            "action": STEP_ACTIONS[step["id"]][1],
            "output": STEP_ACTIONS[step["id"]][2],
            "execution": "connected",
            "linked_workflow_id": (
                "validation" if step["id"] == "verify_green_state" else None
            ),
        })

    bots = [
        {"id": bid, "persona_label": _persona_label(bid), "card": ROLE_CARDS[bid]}
        for bid in BOT_IDS
    ]

    occupancy = _occupancy(project, [s["id"] for s in steps])
    conductor = {
        "id": "conductor",
        "name": "Conductor",
        "description": "PRISM delivery workflow",
        "steps": steps,
        "bots": bots,
        "occupancy": occupancy,
    }
    validation = _project_validation_workflow(project)
    conductor_behaviors = _conductor_behavior_workflows(project)

    catalog = [conductor, validation, *conductor_behaviors]

    return {
        "steps": steps,
        "bots": bots,
        "occupancy": occupancy,
        "workflows": catalog,
    }


@router.post("/{workflow_id}/runs")
def start_workflow_run(workflow_id: str, project: str = Query(...)) -> dict:
    if workflow_id != "validation":
        raise HTTPException(409, "only scripted project workflows can be run here")
    from prism_service.services.claude_transcripts import _project_source_path
    from prism_service.services.source_snapshot import capture_source_snapshot

    configured = Path(_project_source_path(project))
    fallback = Path.home() / "projects" / project
    root = configured if configured.is_absolute() and configured.exists() else fallback
    try:
        snapshot = capture_source_snapshot(root)
    except RuntimeError as exc:
        raise HTTPException(409, f"source snapshot unavailable: {exc}") from exc
    return _workflow_engine_json(
        f"/workflows/validation/{project}", method="POST",
        body={"sourceSnapshot": snapshot},
    )


@router.get("/runs/{instance_id}")
def get_workflow_run(instance_id: str) -> dict:
    return _workflow_engine_json(f"/workflows/instances/{instance_id}")


@router.get("/{workflow_id}/runs/active")
def get_active_workflow_run(workflow_id: str, project: str = Query(...)) -> dict:
    if workflow_id != "validation":
        raise HTTPException(404, "only scripted project workflows have runtime instances")
    return _workflow_engine_json(f"/workflows/active/{project}")


@router.get("/{workflow_id}/runs/history")
def get_workflow_run_history(
    workflow_id: str, project: str = Query(...), limit: int = Query(72, ge=1, le=200),
) -> dict:
    if workflow_id != "validation":
        raise HTTPException(404, "only scripted project workflows have runtime history")
    return _workflow_engine_json(f"/workflows/history/{project}?limit={limit}")


@router.post("/{workflow_id}/fixes")
def request_workflow_fix(
    workflow_id: str, body: WorkflowFixRequest, project: str = Query(...),
) -> dict:
    return queue_workflow_fix(project, workflow_id, body)


# ---------------------------------------------------------------------------
# Phase 0 of the Bot/Behavior FSM migration (see [[project_prism_workflow_engine_migration]]):
# the FIRST typed callback an AosWorkflows step can make into prism-service
# for real work. Wraps ContextBuilder.build() — already real, already used
# by the interactive MCP path (mcp/tools.py's context_bundle tool) — behind
# a Pydantic contract an AosWorkflows AgentStep can call directly. Read-only,
# no side effects: the safest possible first slice to prove the callback
# shape, typed both ways, traced end-to-end.
# ---------------------------------------------------------------------------


class StepEnrichRequest(BaseModel):
    workflow_id: str
    instance_id: str
    step_id: str
    persona: str = ""
    story_file: str = ""


class StepEnrichResponse(BaseModel):
    role_card: dict = {}
    rules: list = []
    brain_context: str = ""
    relevant_memory: list = []
    conventions: list = []
    active_tasks: dict = {}
    workflow_state: dict = {}


@router.post("/steps/context-enrich")
def workflow_step_context_enrich(
    body: StepEnrichRequest, project: str = Query(...),
) -> StepEnrichResponse:
    with _tracer.start_as_current_span("workflow.step.enrich") as span:
        span.set_attribute("workflow.instance.id", body.instance_id)
        span.set_attribute("workflow.step.id", body.step_id)
        span.set_attribute("workflow.project", project)
        ctx = get_project(project)
        bundle = ContextBuilder(
            project_id=project,
            brain_svc=ctx.brain_svc,
            memory_svc=ctx.memory_svc,
            task_svc=ctx.task_svc,
            workflow_svc=ctx.workflow_svc,
            governance=ctx.governance,
            request_id=body.instance_id,
        ).build(persona=body.persona or None, story_file=body.story_file or None)
        return StepEnrichResponse(
            role_card=bundle.get("role_card") or {},
            rules=bundle.get("rules") or [],
            brain_context=bundle.get("brain_context") or "",
            relevant_memory=bundle.get("relevant_memory") or [],
            conventions=bundle.get("conventions") or [],
            active_tasks=bundle.get("active_tasks") or {},
            workflow_state=bundle.get("workflow_state") or {},
        )
