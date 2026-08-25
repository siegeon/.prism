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

# "Who may decide this gate, and how to recover from a wrong decision" —
# workflow behavior content surfaced on the Workflows page (owner 2026-08-25:
# "prevent it with workflow behavior content", after a session spent several
# turns explaining gate authority in chat instead of the app explaining it
# itself). story_gate/plan_gate/red_gate are always machine-adjudicable
# (task_runner + gate_adjudicator can decide them); green_gate additionally
# depends on the TASK's own proof_type, which this static per-step dict
# can't see — so its text stays generically true for both cases rather than
# picking one. Every gate's text ends the same way: the recovery lever is
# the "Rewind one step" control on the task's own Evidence tab, never a raw
# API call.
GATE_AUTHORITY = {
    "story_gate": (
        "Decided by an independent Steward — machine-adjudicable when the "
        "story rubric is met, or a human owner's own Approve otherwise. "
        "Approved in error? Use \"Rewind one step\" on the task's Evidence "
        "tab to reopen this gate."),
    "plan_gate": (
        "Decided by an independent Steward — machine-adjudicable when the "
        "plan rubric is met, or a human owner's own Approve otherwise. "
        "Approved in error? Use \"Rewind one step\" on the task's Evidence "
        "tab to reopen this gate."),
    "red_gate": (
        "Decided by an independent Steward — machine-adjudicable on a "
        "fresh passing EvidenceReceipt. Approved in error? Use \"Rewind "
        "one step\" on the task's Evidence tab to reopen this gate."),
    "green_gate": (
        "Decided by an independent Steward. Machine-adjudicable ONLY for "
        "proof_type=test tasks with a fresh passing EvidenceReceipt — a "
        "demo/review proof_type is human-only by standing rule and must "
        "never be machine- or self-approved. Approved in error? Use "
        "\"Rewind one step\" on the task's Evidence tab to reopen this "
        "gate — a passed gate can't be Rejected, only rewound."),
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
    read a count directly instead of branching on presence. A done,
    cancelled, or deleted task is not standing anywhere -- a cancelled task
    keeps its last workflow_step on the row forever (task_update never
    clears it), so excluding only 'done' let every cancelled task parked at
    a step (a real, common case: this project alone has cancel/redo cycles
    that left several tasks sitting at story_gate/plan_gate) inflate that
    step's occupancy count and kept the canvas showing a path as "running"
    long after the task was cancelled (owner, live: "the newest workflow
    is still running from the conductor?" -- for a task already
    cancelled). A legacy row parked at a step id the FSM no longer
    contains must not invent a node the canvas cannot draw either.
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
                    "WHERE status NOT IN ('done', 'cancelled', 'deleted') "
                    "GROUP BY workflow_step"
                ).fetchall()
            for step, count in rows:
                if step in counts:
                    counts[step] = int(count)
            return counts
        except (sqlite3.Error, OSError):
            return counts

    for task in svc.list():
        if getattr(task, "status", "") in ("done", "cancelled", "deleted"):
            continue
        step = getattr(task, "workflow_step", "") or ""
        if step in counts:
            counts[step] += 1
    return counts


def _task_count_by_workflow(project: str, catalog_ids: list[str]) -> dict[str, int]:
    """Active (pending|in_progress|blocked) tasks bound to each catalog
    entry, joined through models.task.WORKFLOW_ALIASES (task af396b2c) --
    a task never names a catalog id directly, it names a stable
    worker-facing value (task.workflow, "implement" today) that the alias
    map resolves to the entry that actually drives it. Legacy rows (blank
    column) normalize to DEFAULT_WORKFLOW at hydration time
    (task_service._row_to_task), so they count too. Same active-status
    filter as _occupancy above, for the same reason: a done/cancelled task
    is not standing behind any workflow's queue."""
    from prism_service.models.task import WORKFLOW_ALIASES, normalize_workflow

    counts = {cid: 0 for cid in catalog_ids}
    try:
        svc = get_project(project).task_svc
    except Exception:
        return counts
    for task in svc.list():
        if getattr(task, "status", "") not in ("pending", "in_progress", "blocked"):
            continue
        # normalize_workflow so a Task built without going through
        # TaskService's own hydration (a raw dataclass, a legacy row read
        # by some OTHER path) still resolves to the default driver instead
        # of silently miscounting a blank value as "no workflow".
        catalog_id = WORKFLOW_ALIASES.get(normalize_workflow(getattr(task, "workflow", "")))
        if catalog_id in counts:
            counts[catalog_id] += 1
    return counts


def _conductor_behavior_workflows(project: str) -> list[dict]:
    """Each of the conductor bot's AosWorkflows Behaviors, as its OWN
    catalog entry -- not one synthetic wrapper node whose fake "steps" were
    just the behavior ids. Bot [1] uses FSM [1..*], FSM [1] has Behavior
    [0..*] (see the same ontology comment in AosWorkflows' Program.cs); a
    behavior IS a flow, so it gets a real catalog entry disclosing its OWN
    real steps (e.g. land's push/open-pr), not a fake single node standing
    in for the whole thing.

    Nested under "conductor" only when a real state->behavior link exists --
    the same rule verify_green_state already follows via linked_workflow_id.
    RESOLVED (owner, 2026-08-21): "make the conductor's workflow have a
    final ship step when it's all done" -- green_gate approval DOES now
    trigger shipping, automatically, on BOTH tracks: the human-approved path
    (proof_type=demo/review, PRISM_SHIP_ON_APPROVE ship-on-approve queue,
    task 5b6aefc1) and the machine-adjudicated path (proof_type=test, added
    this session -- ship_worker._awaiting_ship_machine/_adjudicate_after_ship,
    closing the gap where a fully-autonomous task cleared every OTHER
    green_gate tooth and then parked forever on shipped-ness alone, because
    nothing ever landed its branch). `land` is `ship_worker.py`'s real
    counterpart in THIS registry, so it now nests under conductor as the
    FSM's terminal step (see _CONDUCTOR_LINKED_BEHAVIOR_IDS below) -- it is
    what a person reads to understand "what ships the code", even though
    the seat that actually executes it today is ship_worker.py's Python
    pipeline, not (yet) this JSON behavior fired by the AosWorkflows engine
    itself; wiring THAT dispatch through is the larger FSM-migration
    follow-up this docstring is not scoping. `ci-local-dev` has no
    corresponding conductor-state trigger and stays unparented.

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
            "authority": GATE_AUTHORITY.get(step["id"], ""),
            "execution": "connected",
            "linked_workflow_id": (
                "validation" if step["id"] == "verify_green_state"
                else "story-gate-check" if step["id"] == "story_gate"
                else "plan-gate-check" if step["id"] == "plan_gate"
                else "draft-story-loop" if step["id"] == "draft_story"
                else "review-previous-notes-loop" if step["id"] == "review_previous_notes"
                else "verify-plan-loop" if step["id"] == "verify_plan"
                else "write-failing-tests-loop" if step["id"] == "write_failing_tests"
                else "implement-tasks-loop" if step["id"] == "implement_tasks"
                else "red-gate-status" if step["id"] == "red_gate"
                else "green-gate-status" if step["id"] == "green_gate"
                else None
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
    # Nested, not a flat sibling of conductor: this IS the conductor's own
    # capability -- the FSM its own verify_green_state step links to
    # (linked_workflow_id below) -- same category as land/ci-local-dev, it
    # simply predates the Bot/Behavior registry and is sourced differently.
    validation["parent_id"] = "conductor"
    conductor_behaviors = _conductor_behavior_workflows(project)
    # Same rule as validation above: nest only the behavior(s) an actual
    # conductor state links to. story_gate now links to "story-gate-check"
    # (linked_workflow_id above). "land" nests too (owner, 2026-08-21): it
    # is the conductor's real final step -- green_gate approval ships the
    # branch automatically via ship_worker.py on both the human and machine
    # tracks, see _conductor_behavior_workflows' docstring. It has no
    # WORKFLOW_STEPS entry of its own to carry a linked_workflow_id (green_gate
    # is the FSM's structurally-terminal state, audited this session as too
    # risky to insert a new step after -- 14+ call sites treat "green_gate"
    # as literally the last one), so it nests via this set directly instead
    # of via a step link, same mechanism validation predates. "ci-local-dev"
    # stays unparented: no conductor state triggers it.
    _CONDUCTOR_LINKED_BEHAVIOR_IDS = (
        "story-gate-check", "plan-gate-check", "draft-story-loop",
        "review-previous-notes-loop", "verify-plan-loop",
        "write-failing-tests-loop", "implement-tasks-loop",
        "red-gate-status", "green-gate-status", "land",
    )
    for entry in conductor_behaviors:
        if entry["id"] in _CONDUCTOR_LINKED_BEHAVIOR_IDS:
            entry["parent_id"] = "conductor"

    catalog = [conductor, validation, *conductor_behaviors]
    # task_count (task af396b2c): the queue standing behind each catalog
    # entry -- see _task_count_by_workflow's docstring for the alias join.
    _counts = _task_count_by_workflow(project, [entry["id"] for entry in catalog])
    for entry in catalog:
        entry["task_count"] = _counts.get(entry["id"], 0)

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


class StoryGateCheckRequest(BaseModel):
    story_doc: str
    task_id: str = ""


class StoryGateCheckResponse(BaseModel):
    ok: bool
    reason: str


@router.post("/steps/story-gate-check")
def workflow_step_story_gate_check(
    body: StoryGateCheckRequest, project: str = Query(...),
) -> StoryGateCheckResponse:
    """Read-only: wraps the EXISTING story_complete rubric scorer behind a
    typed contract, same shape as /steps/context-enrich above. Does not
    write to any task, does not decide story_gate for real -- PRISM's own
    story_gate still autoclears via conductor_flow.py's _AUTOCLEAR_GATES,
    untouched. This is purely an observational capability so the conductor
    directory can show a real, callable behavior for story_gate instead of
    nothing -- cutting it over to actually decide the gate is separate,
    later work (owner call, not implied here)."""
    with _tracer.start_as_current_span("workflow.step.story_gate_check") as span:
        span.set_attribute("workflow.step.id", "story-gate-check")
        span.set_attribute("workflow.project", project)
        if body.task_id:
            span.set_attribute("workflow.task.id", body.task_id)
        from prism_service.services import arc_governance as gov
        rubric = gov.load_rubrics().get("story_complete") or {}
        evidence = {"story_md": body.story_doc}
        result = gov.score_story_complete(evidence, rubric)
        return StoryGateCheckResponse(
            ok=result.get("ok", False),
            reason=result.get("reason", ""),
        )


class PlanGateCheckRequest(BaseModel):
    plan_doc: str
    plan_diagram: str = ""
    task_id: str = ""


class PlanGateCheckResponse(BaseModel):
    ok: bool
    reason: str


@router.post("/steps/plan-gate-check")
def workflow_step_plan_gate_check(
    body: PlanGateCheckRequest, project: str = Query(...),
) -> PlanGateCheckResponse:
    """Read-only, same shape and same non-authoritative status as
    /steps/story-gate-check above -- wraps the EXISTING plan_coverage
    rubric scorer (conductor_service.py's _verify_rubric_gate is the real
    caller; this route mirrors its evidence/principles construction
    exactly, but only reads, never writes). PRISM's own plan_gate still
    autoclears via conductor_flow.py's _AUTOCLEAR_GATES, untouched."""
    with _tracer.start_as_current_span("workflow.step.plan_gate_check") as span:
        span.set_attribute("workflow.step.id", "plan-gate-check")
        span.set_attribute("workflow.project", project)
        if body.task_id:
            span.set_attribute("workflow.task.id", body.task_id)
        from prism_service.services import arc_governance as gov
        rubric = gov.load_rubrics().get("plan_coverage") or {}
        # Mirrors conductor_service.py's _verify_rubric_gate exactly: by
        # plan_gate time task.plan_doc holds the consolidated story+plan
        # document, so the SAME value is passed as both story_md (for the
        # AC-id coverage diff) and plan_doc -- not an oversight, the real
        # caller does this too.
        evidence = {
            "story_md": body.plan_doc,
            "plan_doc": body.plan_doc,
            "plan_diagram": body.plan_diagram,
        }
        ctx = get_project(project)
        principles = gov.load_principles(ctx.memory_svc) if ctx.memory_svc is not None else []
        result = gov.score_plan_coverage(evidence, rubric, principles)
        return PlanGateCheckResponse(
            ok=result.get("ok", False),
            reason=result.get("reason", ""),
        )


def _score_rubric(rubric_name: str, fields: dict, project: str) -> dict:
    """Dispatch to the right existing PURE scorer by rubric name, mapping
    the Reason stage's structured_output fields onto each scorer's own
    evidence shape. One place that knows "which scorer, which fields" so
    /steps/reason-loop stays generic instead of growing an if/elif per
    conductor state."""
    from prism_service.services import arc_governance as gov

    rubric = gov.load_rubrics().get(rubric_name) or {}
    if rubric_name == "story_complete":
        return gov.score_story_complete({"story_md": fields.get("story_md", "")}, rubric)
    if rubric_name == "premise_grounded":
        return gov.score_premise_grounded({"notes_md": fields.get("notes_md", "")}, rubric)
    if rubric_name == "plan_coverage":
        ctx = get_project(project)
        principles = gov.load_principles(ctx.memory_svc) if ctx.memory_svc is not None else []
        # Mirrors conductor_service.py's _verify_rubric_gate AND
        # /steps/plan-gate-check exactly: story_md gets the SAME value as
        # plan_doc (by plan_gate time task.plan_doc already embeds the
        # story's AC ids). A prior version read a "story_md" key here that
        # no reason-loop schema ever asks the model to produce -- caught
        # live: verify-plan-loop's real call always failed AC-coverage
        # with an empty story to diff against.
        plan_doc = fields.get("plan_doc", "")
        evidence = {
            "story_md": plan_doc,
            "plan_doc": plan_doc,
            "plan_diagram": fields.get("plan_diagram", ""),
        }
        return gov.score_plan_coverage(evidence, rubric, principles)
    return {"ok": False, "reason": f"unknown rubric: {rubric_name!r}"}


class ReasonLoopRequest(BaseModel):
    persona: str = "sm"
    prompt: str = Field(min_length=1)
    json_schema: dict
    rubric: str = ""
    model: str = "haiku"
    max_budget_usd: float = 0.5
    max_turns: int = 4
    task_id: str = ""


class ReasonLoopResponse(BaseModel):
    observe: dict
    reason: dict
    validation: dict


@router.post("/steps/reason-loop")
def workflow_step_reason_loop(
    body: ReasonLoopRequest, project: str = Query(...),
) -> ReasonLoopResponse:
    """GENERIC Observe -> Reason -> Validate loop -- every conductor
    authoring state (draft_story, verify_plan, write_failing_tests,
    implement_tasks, review_previous_notes) reuses THIS ONE endpoint via
    its own behavior JSON (persona/prompt/json_schema/rubric as data),
    instead of a bespoke Python function duplicated per state. Matches
    owner direction: the loop stages are generic and implicit; only Act
    (a real, typed side-effect) is genuinely custom code per behavior.

    Act is deliberately NOT here -- this is strictly Observe+Reason+
    Validate, inert, no writes to any real task, no gate decided for
    real. Authorize+Act require explicit owner sign-off before any real
    state gets wired to this (agreed: inert proof only, this round)."""
    with _tracer.start_as_current_span("workflow.loop.reason") as span:
        span.set_attribute("workflow.project", project)
        span.set_attribute("workflow.loop.persona", body.persona)
        span.set_attribute("workflow.loop.rubric", body.rubric)
        if body.task_id:
            span.set_attribute("workflow.task.id", body.task_id)

        # --- Observe: same ContextBuilder call /steps/context-enrich uses ---
        ctx = get_project(project)
        bundle = ContextBuilder(
            project_id=project, brain_svc=ctx.brain_svc, memory_svc=ctx.memory_svc,
            task_svc=ctx.task_svc, workflow_svc=ctx.workflow_svc, governance=ctx.governance,
            request_id=f"reason-loop:{body.task_id or 'adhoc'}",
        ).build(persona=body.persona, story_file=None)
        observe = {
            "ok": True,
            "conventions_count": len(bundle.get("conventions") or []),
            "has_role_card": bool(bundle.get("role_card")),
        }

        # --- Reason: schema-constrained claude -p, isolated from task_runner ---
        from pathlib import Path
        from prism_service.services.claude_transcripts import _project_source_path
        from prism_service.inference import claude_cli

        configured = Path(_project_source_path(project))
        fallback = Path.home() / "projects" / project
        root = configured if configured.is_absolute() and configured.exists() else fallback
        full_prompt = f"{body.prompt}\n\nProject conventions:\n{bundle.get('conventions')}"
        result = claude_cli.invoke(
            full_prompt, work_dir=root, plugin_dir=root,
            model=body.model, max_budget_usd=body.max_budget_usd, max_turns=body.max_turns,
            project=project, purpose="reason-loop",
            json_schema=body.json_schema,
        )
        fields = result.structured_output or {}
        reason = {
            "ok": bool(fields),
            "fields": fields,
            "cost_usd": result.usage.get("cost_usd", 0.0),
            "run_id": result.run_id,
        }

        # --- Validate: reuse the SAME pure rubric scorers story/plan-gate-check wrap ---
        if body.rubric:
            verdict = _score_rubric(body.rubric, fields, project)
            validation = {"ok": verdict.get("ok", False), "reason": verdict.get("reason", "")}
        else:
            validation = {"ok": None, "reason": "no rubric specified -- Validate skipped"}

        return ReasonLoopResponse(observe=observe, reason=reason, validation=validation)


class RedGateStatusRequest(BaseModel):
    task_id: str = Field(min_length=1)


class RedGateStatusResponse(BaseModel):
    has_fresh_red_receipt: bool
    red_sha: str
    reason: str
    latest_receipt_status: str
    latest_receipt_reason: str


@router.post("/steps/red-gate-status")
def workflow_step_red_gate_status(
    body: RedGateStatusRequest, project: str = Query(...),
) -> RedGateStatusResponse:
    """GOVERNANCE VISIBILITY for red_gate -- read-only, reuses the exact
    pure-read functions the real adjudicator (ConductorService.
    adjudicate_test_red_gate) consults, without calling that method or any
    of its embedded writes (record_history, park_red_gate). red_gate
    itself is untouched: still a real WORKFLOW_STEPS state, still decided
    the same way it always was. This just makes what's ALREADY on file
    observable as a typed, programmatic behavior instead of invisible
    inside conductor_service.py -- the owner's actual ask: "we could not
    see with governance what was happening on the step."""
    from prism_service.services import oracle_spec as osp

    with _tracer.start_as_current_span("workflow.step.red_gate_status") as span:
        span.set_attribute("workflow.project", project)
        span.set_attribute("workflow.task.id", body.task_id)

        ctx = get_project(project)
        task = ctx.task_svc.get(body.task_id)
        if task is None:
            return RedGateStatusResponse(
                has_fresh_red_receipt=False, red_sha="",
                reason=f"no such task: {body.task_id}",
                latest_receipt_status="", latest_receipt_reason="",
            )

        red_sha = ctx.conductor_svc._red_step_sha(body.task_id)
        spec = osp.OracleSpec.from_task(task)
        fresh = (
            osp.fresh_red_receipt(project, body.task_id, red_sha, spec.spec_hash())
            if red_sha else None
        )
        latest = osp.latest_receipt(project, body.task_id)

        if fresh is not None:
            reason = f"fresh red receipt on file at {red_sha[:12]}: {fresh.reason}"
        elif not red_sha:
            reason = "no red-step commit resolved yet -- write_failing_tests hasn't landed a tests-only commit"
        else:
            reason = f"no fresh red receipt for the current red-step commit ({red_sha[:12]})"

        return RedGateStatusResponse(
            has_fresh_red_receipt=fresh is not None,
            red_sha=red_sha,
            reason=reason,
            latest_receipt_status=(getattr(latest, "status", "") or "") if latest else "",
            latest_receipt_reason=(getattr(latest, "reason", "") or "") if latest else "",
        )


class GreenGateStatusRequest(BaseModel):
    task_id: str = Field(min_length=1)


class GateCheckStatus(BaseModel):
    id: str
    label: str
    ok: bool
    reason: str


class GreenGateStatusResponse(BaseModel):
    has_fresh_passing_receipt: bool
    reason: str
    latest_receipt_status: str
    latest_receipt_reason: str
    checks: list[GateCheckStatus] = []


# Every signal that can refuse a green_gate approve or the
# verify_green_state advance into it (owner directive, task 3baadd19,
# 2026-08-24: "make this real... make sure that it is a part of the flows
# and enforces our rules" -- the Workflows page's green-gate-status view
# was a single opaque oracle-receipt check while SIX other real teeth
# governed the same gate invisibly; then, seeing the old 1-step diagram:
# "if there are 5 [sic; 7] steps in the green gate behavior than you
# should show them, here so we can see"). ONE ordered registry, each
# entry a (label, compute_fn) pair calling the EXACT function the real
# enforcement path calls -- never a reimplementation, so neither the
# aggregate endpoint nor a single-check lookup can ever show a different
# answer than what actually happens. Both the aggregate
# /steps/green-gate-status endpoint AND the per-check
# /steps/green-gate-check endpoint (one JSON behavior step per entry, so
# the Workflows page diagram shows a real node per check) read this same
# registry -- one source of truth, not two.
def _green_gate_check_registry(ctx, task, project: str) -> "dict[str, tuple[str, object]]":
    from prism_service.services import conductor_service as _cs

    def _candidate_controls():
        from prism_service.services import control_plane as _cp
        return _cp.candidate_controls_judge_reason(task) or ""

    def _reachability():
        from prism_service.services import reachability_check as _rc
        return _rc.unreachable_entry_point_reason(task) or ""

    def _ui_artifact():
        return _cs.ui_artifact_gate_reason(
            getattr(task, "tags", None), getattr(task, "proof_type", ""),
            getattr(task, "completion_proof", "")) or ""

    def _screen_claim():
        return _cs._screen_claim_gate_reason(
            getattr(task, "tags", None), getattr(task, "proof_type", ""),
            getattr(task, "oracle", "")) or ""

    def _shipped_ness():
        return ctx.conductor_svc._unshipped_gate_reason(task) or ""

    def _demo_evidence():
        return _cs.demo_evidence_gate_reason(task, project) or ""

    def _oracle_receipt():
        refusal, _fresh = ctx.conductor_svc._oracle_receipt_refusal(
            task, override=False, reason="")
        return refusal or ""

    return {
        "candidate_controls": (
            "Judge integrity (no dirty policy files)", _candidate_controls),
        "reachability": (
            "New entry points have a real production caller", _reachability),
        "ui_artifact": (
            "Demo/screenshot artifact cited (ui-tagged tasks)", _ui_artifact),
        "screen_claim": (
            "A test-proof ticket does not claim a screen", _screen_claim),
        "shipped_ness": (
            "This task's own commit trailer reached origin/main", _shipped_ness),
        "demo_evidence": (
            "A demo/review claim has captured evidence", _demo_evidence),
        "oracle_receipt": (
            "A fresh passing oracle receipt is on file", _oracle_receipt),
    }


def _run_one_check(ctx, task, project: str, check_id: str) -> "GateCheckStatus":
    registry = _green_gate_check_registry(ctx, task, project)
    entry = registry.get(check_id)
    if entry is None:
        return GateCheckStatus(id=check_id, label=f"unknown check {check_id!r}",
                               ok=True, reason="")
    label, fn = entry
    try:
        reason = fn() or ""
    except Exception:
        return GateCheckStatus(id=check_id, label=label, ok=True, reason="")
    return GateCheckStatus(id=check_id, label=label, ok=not reason,
                           reason=reason)


def _green_gate_checks(ctx, task, project: str) -> list["GateCheckStatus"]:
    """The full ordered list, in registry order -- used by the aggregate
    endpoint. try/except per-tooth (matching this module's existing
    pre-flight convention, e.g. ConductorService.adjudicate_green_gate's
    own reachability_check/candidate_controls_judge calls): a tooth that
    cannot be evaluated for this task/environment reports ok=True with an
    empty reason (not-yet-applicable), never crashes the whole report.
    Excludes oracle_receipt -- the aggregate response already carries that
    signal via its own has_fresh_passing_receipt/reason fields."""
    registry = _green_gate_check_registry(ctx, task, project)
    return [_run_one_check(ctx, task, project, check_id)
            for check_id in registry if check_id != "oracle_receipt"]


@router.post("/steps/green-gate-status")
def workflow_step_green_gate_status(
    body: GreenGateStatusRequest, project: str = Query(...),
) -> GreenGateStatusResponse:
    """GOVERNANCE VISIBILITY for green_gate -- read-only, same pattern as
    /steps/red-gate-status. Reuses ConductorService._oracle_receipt_refusal
    directly -- the EXACT same read-only call gate_adjudicator.py's
    _pending_decline_reason already makes for green_gate reporting, not a
    reimplementation. Never calls adjudicate_green_gate or any of its
    writes. green_gate itself is untouched: still a real WORKFLOW_STEPS
    state, still decided the same way it always was.

    `checks` (task 3baadd19, 2026-08-24) makes this the COMPLETE picture,
    not just the oracle-receipt tooth: every pre-flight that can refuse
    green_gate, each calling the identical function the real enforcement
    path calls -- see _green_gate_checks."""
    from prism_service.services import oracle_spec as osp

    with _tracer.start_as_current_span("workflow.step.green_gate_status") as span:
        span.set_attribute("workflow.project", project)
        span.set_attribute("workflow.task.id", body.task_id)

        ctx = get_project(project)
        task = ctx.task_svc.get(body.task_id)
        if task is None:
            return GreenGateStatusResponse(
                has_fresh_passing_receipt=False,
                reason=f"no such task: {body.task_id}",
                latest_receipt_status="", latest_receipt_reason="",
                checks=[],
            )

        refusal, fresh = ctx.conductor_svc._oracle_receipt_refusal(
            task, override=False, reason="")
        latest = osp.latest_receipt(project, body.task_id)

        return GreenGateStatusResponse(
            has_fresh_passing_receipt=fresh is not None,
            reason=(refusal or (f"fresh passing receipt on file: {fresh.reason}" if fresh else "")),
            latest_receipt_status=(getattr(latest, "status", "") or "") if latest else "",
            latest_receipt_reason=(getattr(latest, "reason", "") or "") if latest else "",
            checks=_green_gate_checks(ctx, task, project),
        )


class GreenGateCheckRequest(BaseModel):
    task_id: str = Field(min_length=1)
    check: str = Field(min_length=1)


@router.post("/steps/green-gate-check")
def workflow_step_green_gate_check(
    body: GreenGateCheckRequest, project: str = Query(...),
) -> GateCheckStatus:
    """ONE named pre-flight tooth from _green_gate_check_registry, read-
    only, same governance-visibility contract as /steps/green-gate-status
    (never calls adjudicate_green_gate or any write). Exists so the
    Workflows page's green-gate-status BEHAVIOR can chain one JSON step
    per real check (owner, task 3baadd19, 2026-08-24, on seeing the old
    1-step diagram: "if there are 5 [sic; 7] steps in the green gate
    behavior than you should show them, here so we can see") -- a genuine
    node per tooth, not a checklist buried inside one opaque callback's
    response body. Always HTTP 200 regardless of ok=true/false (matching
    /steps/green-gate-status's own existing exit_code==0-always contract):
    a refused tooth is a REPORTED fact, not a callback FAILURE, so the
    chain reaches every subsequent check and Complete regardless of any
    single tooth's verdict."""
    with _tracer.start_as_current_span("workflow.step.green_gate_check") as span:
        span.set_attribute("workflow.project", project)
        span.set_attribute("workflow.task.id", body.task_id)
        span.set_attribute("workflow.check", body.check)

        ctx = get_project(project)
        task = ctx.task_svc.get(body.task_id)
        if task is None:
            return GateCheckStatus(id=body.check, label=body.check,
                                   ok=False, reason=f"no such task: {body.task_id}")
        return _run_one_check(ctx, task, project, body.check)
