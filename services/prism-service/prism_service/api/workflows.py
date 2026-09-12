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
from typing import Literal, Optional
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
from prism_service.services.agent_runs_data import (
    NODE_TREND_WINDOW,
    node_last_run,
    node_recent_runs,
    node_run_counts,
    node_token_trend,
)

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

# task 408138e8 (epic 61821448): every step's `action` text names the
# trigger that starts it -- the skill-description-says-when SHACL rule
# (ontology/shapes.ttl) reads this text (via _catalog_entries' network-
# free fallback, ontology_prototype_projection.py) when the live /api/
# workflows catalog is unreachable, so the trigger clause must live HERE,
# not only in the live catalog's own description strings below.
STEP_ACTIONS = {
    "review_previous_notes": ("Existing project memory and source", "Review prior decisions and ground every premise in evidence. Runs first, when a task starts the implement workflow.", "A cited premise report"),
    "draft_story": ("Grounded premises and the requested outcome", "Author requirements and acceptance criteria with observable oracles. Runs when a task enters draft_story, right after review_previous_notes.", "A reviewable story"),
    "story_gate": ("The authored story", "An independent Steward adjudicates story completeness. Runs when a task's draft_story step finishes.", "Approved story or a concrete refusal reason"),
    "verify_plan": ("The approved story", "Check that the implementation plan covers every acceptance criterion. Runs when a task enters verify_plan, right after story_gate.", "A coverage-backed plan"),
    "plan_gate": ("The verified plan", "An independent Steward adjudicates plan coverage. Runs when a task's verify_plan step finishes.", "Approved plan or a concrete refusal reason"),
    "write_failing_tests": ("Acceptance criteria and plan", "Write traced tests that fail for the missing behavior. Runs when a task enters write_failing_tests, right after plan_gate.", "Reproducible red evidence"),
    "red_gate": ("Failing test evidence", "An independent Steward confirms the failure is relevant and honest. Runs when a task's write_failing_tests step finishes.", "Approved red state or a refusal reason"),
    "implement_tasks": ("Approved plan and failing tests", "Make the smallest source change that turns the tests green. Runs when a task enters implement_tasks, right after red_gate.", "Implemented source changes"),
    "verify_green_state": ("Implementation and its verification commands", "Run the real verification suite and inspect the resulting evidence. Runs when a task enters verify_green_state, right after implement_tasks.", "Full green evidence"),
    "green_gate": ("Green evidence and acceptance oracles", "An independent Steward decides whether the requested outcome is actually complete. Runs when a task's verify_green_state step finishes.", "Accepted outcome or follow-up work"),
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
# Triage workflow (task b837bc98): step content for the catalog entry, same
# role STEP_ACTIONS plays for the implement/conductor steps above -- kept
# separate since triage step ids (intake/classify/decide/done) aren't in
# models.roles.STEP_ROLES, so this dict also stands in for that persona
# lookup (see _triage_workflow below) rather than reaching into roles.py.
TRIAGE_STEP_CONTENT = {
    "intake": ("The item as it arrived on its channel", "Register the item and enter the triage flow", "A tracked item awaiting classification"),
    "classify": ("The tracked item", "Bucket it Open, Monitoring, Resolved, or Dropped with a one-line reason", "A bucketed item and its reason"),
    "decide": ("The classification and its reason", "The single human/owner stop — confirm or override the bucket", "A decided item"),
    "done": ("A decided item", "Close out triage for this item", "A triaged item"),
}

# Align-language workflow (task f07c9cea): step content for the catalog
# entry, same role TRIAGE_STEP_CONTENT plays for triage — this workflow's
# step ids (collect/align/verify/done) also are not in models.roles.
# STEP_ROLES, so this dict again stands in for that persona lookup (see
# _align_language_workflow below). No step here is a gate: the whole pass
# is machine-run, per owner rule mx-f49a5c.
ALIGN_LANGUAGE_STEP_CONTENT = {
    "collect": (
        "Every task in the project",
        "Run a dry-run scan and count the tasks with loose language",
        "A dry-run report: how many tasks would change, and why",
    ),
    "align": (
        "The dry-run report",
        "Rewrite each flagged task's free text into plain Simplified "
        "Technical English through TaskService.update",
        "The tasks actually changed, and the rule that fired on each one",
    ),
    "verify": (
        "The tasks just aligned",
        "Run the scan again and confirm no loose language remains",
        "Rule counts from before and after, plus a clean second scan",
    ),
    "done": (
        "A finished align-language pass",
        "Close out this run",
        "A completed align-language run",
    ),
}

# Default align-language behaviour (task f07c9cea): every project starts
# here until it calls provide_workflow_behavior("align_language", ...) to
# override a field. Read by align_language_behavior_document below and by
# services/language_alignment_worker.py (the daemon seat).
DEFAULT_ALIGN_LANGUAGE_BEHAVIOR: dict = {
    "enabled": True,
    "mode": "apply",
    "fields": [
        "title", "description", "oracle", "likely_misfire", "stop_if",
        "completion_proof", "premise_notes",
    ],
    "batch_size": 50,
    "include_imported": True,
}

# Promote-to-law workflow (task c5650403): step content for the catalog
# entry, same role STEP_ACTIONS plays for the implement/conductor steps
# above -- kept separate since this workflow's step ids (draft/review/
# install/done) aren't in models.roles.STEP_ROLES.
PROMOTE_TO_LAW_STEP_CONTENT = {
    "draft": (
        "A memory worth promoting",
        "Draft a rule or a term from the memory, with its own fixtures",
        "A draft TTL, ready for the owner to review",
    ),
    "review": (
        "The drafted TTL and its fixtures",
        "The owner reviews the draft against the memory it came from",
        "An approved or a rejected draft",
    ),
    "install": (
        "An approved draft",
        "Write the TTL into the project's own law and prove the "
        "violating fixture fires",
        "An installed rule or term, or a clear refusal reason",
    ),
    "done": (
        "An installed rule or term",
        "Close out this promotion",
        "A promoted memory",
    ),
}

# Default promote-to-law behaviour (task c5650403): every project starts
# here until it calls provide_workflow_behavior("promote_to_law", ...) to
# override a field. require_fixture keeps install() honest -- a draft
# with no demonstrable violating fixture is refused, never installed
# quiet. target is always "project": a promoted rule or term is scoped to
# the project it was drafted in, never the shared package ontology.
DEFAULT_PROMOTE_TO_LAW_BEHAVIOR: dict = {
    "enabled": True,
    "require_fixture": True,
    "target": "project",
}

# Quickfix workflow (task 811fcce0): step content for the catalog entry,
# same role PROMOTE_TO_LAW_STEP_CONTENT plays above -- kept separate since
# this workflow's step ids (intake/apply_fix/verify_fix/done) aren't in
# models.roles.STEP_ROLES. verify_fix's own text names the check as a real
# subprocess run, not an LLM judgment call -- see the doc comment on
# models.workflow.QUICKFIX_STEPS for the Bot/Behavior reasoning.
QUICKFIX_STEP_CONTENT = {
    "intake": (
        "A task the owner already fully diagnosed -- oracle, "
        "likely_misfire, and a pinned test all written up front",
        "Register the task and enter the quickfix flow",
        "A quickfix task ready for its fix",
    ),
    "apply_fix": (
        "The task's own oracle and pinned test",
        "Make the exact change the oracle describes and run the pinned "
        "test",
        "The fix, committed, with its pinned test passing",
    ),
    "verify_fix": (
        "The applied fix",
        "Re-run the full pinned suite from workspace root as an "
        "independent, deterministic check -- a real pytest run, never a "
        "judgment call -- then commit and push",
        "A green suite, pushed to dev and main",
    ),
    "done": (
        "A verified quickfix",
        "Close out this quickfix",
        "A shipped, deterministic fix",
    ),
}

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

# Gates a human owner is NEVER routed to, regardless of task/proof_type
# (owner rule: "red_gate belongs to the MACHINE seat and must NEVER be
# routed to a human" — unlike story_gate/plan_gate/green_gate, whose
# GATE_AUTHORITY text above each carries a human path). The SPA reads this
# to stop rendering the "awaiting review" pill — which claims a human
# reviewer is owed a decision — while a machine-only gate is simply
# waiting on the adjudicator's next sweep (task be158613 follow-on, found
# live 2026-08-26 when the owner watched red_gate read "awaiting review"
# on their own screen via remote assist and asked not to be shown a state
# that isn't real).
MACHINE_ONLY_GATES = {"red_gate"}

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
    # THE ENGINE IS OPTIONAL INFRASTRUCTURE, NOT A DEPENDENCY OF THE PAGE.
    # This entry is sourced from the AosWorkflows engine, and every other
    # workflow on the page is built from local constants. Letting the
    # engine's absence raise took the WHOLE endpoint down with a 503, so a
    # machine with no engine could not read the conductor at all -- and
    # every pull request failed, because a GitHub runner has no engine to
    # reach. `_conductor_behavior_workflows` already degrades exactly this
    # way (`except HTTPException: return []`); this is the same contract
    # for its sibling: report the entry as unavailable, keep the page.
    try:
        definition = ProjectWorkflow.model_validate(
            _workflow_engine_json(f"/workflows/definitions/{project}")
        )
    except HTTPException:
        return {
            "id": "validation",
            "name": "Build and test",
            # The trigger sentence is REQUIRED, not decoration: the
            # skill-description-says-when SHACL rule reads this text and
            # fires without a real "when" clause (task 408138e8).
            "description": (
                "The project's scripted build and test workflow. The "
                "workflow engine is not reachable, so its steps cannot be "
                "read right now. Runs when a developer starts it directly "
                "or from CI."
            ),
            "project_type": "",
            "steps": [],
            "bots": [],
            "occupancy": {},
            "unavailable": True,
        }
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
    # task 408138e8 (epic 61821448): the AosWorkflows engine owns
    # definition.description's own text -- append the real trigger so the
    # skill-description-says-when SHACL rule reads a true "when" clause no
    # matter what the engine's own text says. UPDATED task 25b2a05c:
    # verify_green_state no longer links here (it now has its own honest
    # agentic node, verify-green-state-loop -- see get_workflows'
    # linked_workflow_id map); this build+test workflow stays reachable
    # from the conductor directory for browsing, run directly or from CI.
    trigger = "Runs when a developer starts it directly or from CI."
    description = f"{definition.description.rstrip()} {trigger}".strip()
    return {
        "id": definition.id,
        "name": definition.name,
        "description": description,
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


def align_language_behavior_document(project: str) -> dict:
    """The align-language workflow's current versioned behaviour for
    `project`: DEFAULT_ALIGN_LANGUAGE_BEHAVIOR merged under whatever
    .prism/behaviors/align_language.json (this project's own override
    file, via _behavior_file) carries, plus its own behaviorVersion. A
    missing or unreadable file reads as version 1 with every default
    untouched -- never raises."""
    path = _behavior_file(project, "align_language")
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    document = dict(DEFAULT_ALIGN_LANGUAGE_BEHAVIOR)
    for key in DEFAULT_ALIGN_LANGUAGE_BEHAVIOR:
        if key in data:
            document[key] = data[key]
    try:
        document["behaviorVersion"] = int(data.get("behaviorVersion") or 1)
    except (TypeError, ValueError):
        document["behaviorVersion"] = 1
    return document


def promote_to_law_behavior_document(project: str) -> dict:
    """The promote_to_law workflow's current versioned behaviour for
    `project`: DEFAULT_PROMOTE_TO_LAW_BEHAVIOR merged under whatever
    .prism/behaviors/promote_to_law.json (this project's own override
    file, via _behavior_file) carries, plus its own behaviorVersion. A
    missing or unreadable file reads as version 1 with every default
    untouched -- never raises."""
    path = _behavior_file(project, "promote_to_law")
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    document = dict(DEFAULT_PROMOTE_TO_LAW_BEHAVIOR)
    for key in DEFAULT_PROMOTE_TO_LAW_BEHAVIOR:
        if key in data:
            document[key] = data[key]
    try:
        document["behaviorVersion"] = int(data.get("behaviorVersion") or 1)
    except (TypeError, ValueError):
        document["behaviorVersion"] = 1
    return document


def get_workflow_behavior(project: str, behavior_path: str = "validation") -> dict:
    workflow_id, _, step_id = behavior_path.strip("/").partition("/")
    if workflow_id == "align_language":
        # Flat behaviour, no child steps (unlike "validation/<step>" below).
        if step_id:
            raise HTTPException(
                404, "the align_language behaviour has no child steps")
        return {"path": workflow_id,
                "behavior": align_language_behavior_document(project)}
    if workflow_id == "promote_to_law":
        if step_id:
            raise HTTPException(
                404, "the promote_to_law behaviour has no child steps")
        return {"path": workflow_id,
                "behavior": promote_to_law_behavior_document(project)}
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
    if workflow_id == "align_language":
        if separator or step_id:
            raise HTTPException(
                400, "the align_language behaviour has no child steps")
        current = align_language_behavior_document(project)
        if current["behaviorVersion"] != expected_version:
            raise HTTPException(
                409, f"behavior revision changed: expected "
                     f"{expected_version}, current {current['behaviorVersion']}")
        allowed = set(DEFAULT_ALIGN_LANGUAGE_BEHAVIOR)
        unknown = set(behavior) - allowed
        if unknown:
            raise HTTPException(
                422, f"unsupported behavior fields: {', '.join(sorted(unknown))}")
        merged = dict(current)
        merged.update({k: v for k, v in behavior.items() if k in allowed})
        merged["behaviorVersion"] = current["behaviorVersion"] + 1
        destination = _behavior_file(project, "align_language")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, destination)
        return {"ok": True, "path": "align_language",
                "version": merged["behaviorVersion"]}
    if workflow_id == "promote_to_law":
        if separator or step_id:
            raise HTTPException(
                400, "the promote_to_law behaviour has no child steps")
        current = promote_to_law_behavior_document(project)
        if current["behaviorVersion"] != expected_version:
            raise HTTPException(
                409, f"behavior revision changed: expected "
                     f"{expected_version}, current {current['behaviorVersion']}")
        allowed = set(DEFAULT_PROMOTE_TO_LAW_BEHAVIOR)
        unknown = set(behavior) - allowed
        if unknown:
            raise HTTPException(
                422, f"unsupported behavior fields: {', '.join(sorted(unknown))}")
        merged = dict(current)
        merged.update({k: v for k, v in behavior.items() if k in allowed})
        merged["behaviorVersion"] = current["behaviorVersion"] + 1
        destination = _behavior_file(project, "promote_to_law")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, destination)
        return {"ok": True, "path": "promote_to_law",
                "version": merged["behaviorVersion"]}
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
        channel="daemon",
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


def _occupancy(project: str, step_ids: list[str], svc=None) -> dict[str, int]:
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
    if svc is None:
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


def _task_count_by_workflow(project: str, catalog_ids: list[str], svc=None) -> dict[str, int]:
    """Active (pending|in_progress|blocked) tasks bound to each catalog
    entry, joined through models.task.WORKFLOW_ALIASES (task af396b2c) --
    a task never names a catalog id directly, it names a stable
    worker-facing value (task.workflow, "implement" today) that the alias
    map resolves to the entry that actually drives it. Legacy rows (blank
    column) normalize to DEFAULT_WORKFLOW at hydration time
    (task_service._row_to_task), so they count too. Same active-status
    filter as _occupancy above, for the same reason: a done/cancelled task
    is not standing behind any workflow's queue.

    task b837bc98 (triage): WORKFLOW_ALIASES only carries an entry for
    values that need TRANSLATING to a differently-named catalog id
    ("implement" -> "conductor"). A value that already IS its own catalog
    id (e.g. "triage", task.workflow == the triage catalog entry's own id)
    has no reason to appear there, so the join falls back to the
    normalized value itself when no alias exists."""
    from prism_service.models.task import WORKFLOW_ALIASES, normalize_workflow

    counts = {cid: 0 for cid in catalog_ids}
    if svc is None:
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
        normalized = normalize_workflow(getattr(task, "workflow", ""))
        catalog_id = WORKFLOW_ALIASES.get(normalized, normalized)
        if catalog_id in counts:
            counts[catalog_id] += 1
    return counts


# task 408138e8 (epic 61821448): the real trigger for each conductor
# behavior, keyed by its AosWorkflows behavior_id -- matches
# get_workflows' own linked_workflow_id map (a conductor FSM state calls
# most of these) plus land (green_gate's ship step) and ci-local-dev (no
# FSM state calls it; a person runs it by hand). Read by
# _conductor_behavior_workflows below so every behavior's description
# names a real "when", never the generic fallback text only.
_BEHAVIOR_TRIGGER = {
    "draft-story-loop": "Runs when a task starts the implement workflow.",
    "story-gate-check": "Runs when a task's draft_story step finishes and story_gate needs a decision.",
    "verify-plan-loop": "Runs when a task's story_gate is approved and verify_plan starts.",
    "plan-gate-check": "Runs when a task's verify_plan step finishes and plan_gate needs a decision.",
    "write-failing-tests-loop": "Runs when a task's plan_gate is approved and write_failing_tests starts.",
    "red-gate-status": "Runs when a task's write_failing_tests step finishes and red_gate needs a decision.",
    "implement-tasks-loop": "Runs when a task's red_gate is approved and implement_tasks starts.",
    "red-test-ids": "Runs when a task's implement_tasks step needs the red test ids -- names which of task.verify's pinned targets are demonstrated red at the task's red anchor, from data on file, no model involved.",
    # task 25b2a05c: verify_green_state's own node -- an honest agentic
    # loop (a real qa-role judgement call, never a deterministic check
    # wearing a codified name), so the step is no longer scored ONLY via
    # its drill-down into the unrelated "validation" build+test catalog
    # entry (see get_workflows()'s linked_workflow_id ladder below).
    "verify-green-state-loop": "Runs when a task's implement_tasks step finishes and verify_green_state starts.",
    "green-gate-status": "Runs when a task's verify_green_state step finishes and green_gate needs a decision.",
    "review-previous-notes-loop": "Runs first, when a task starts the implement workflow.",
    "land": "Runs when a task's green_gate is approved and the branch is ready to ship.",
    # task f97c196d. The trigger is a SUCCESSFUL land, not `status == done`:
    # the worktree and the branch are only provably disposable once the work
    # is really on origin/main. ship_worker.py fires it in the same block
    # that records the `land` node.
    "reap": "Runs when a task's land step merged the branch and its worktree is no longer needed.",
    "ci-local-dev": "Run this when a developer wants local CI results before pushing.",
}
_DEFAULT_BEHAVIOR_TRIGGER = "Runs when the conductor bot's own FSM calls this behavior."

# --- Bot tiers (task 0c396de2, owner 2026-08-27, mx-7df790) -----------------
# Each models.workflow.WORKFLOWS entry is a TIER-0 Bot: a deterministic
# finite state machine. The behaviours its states call are its TIER-1
# agentic children. Nothing else carries a tier -- a live read such as
# knowledge_health, or a behaviour no conductor state links to, is not in
# the Bot tree at all.
BOT_HEADER = "BOT · TIER 0 · deterministic FSM"

# The catalog entry id that renders each FSM, where the two names differ.
# Every other WORKFLOWS key names its own entry.
_FSM_ENTRY_ID = {"implement": "conductor"}

# An http step whose url enters the reason loop hands the work to a model.
_REASON_LOOP = "reason-loop"


def _step_is_agentic(kind: str, url: str) -> bool:
    """True when a behaviour step hands its work to a model.

    Derived from WHAT THE STEP DOES -- an http call into the reason loop --
    never from the step's id. A step named "run-tests" that posts to the
    reason loop is agentic; one named "call-llm" that runs a shell command
    is not (task 0c396de2's likely_misfire)."""
    return (kind or "").lower() == "http" and _REASON_LOOP in (url or "")


def _fsm_entry_ids() -> dict[str, str]:
    """{catalog entry id: fsm id} for every WORKFLOWS FSM.

    Read off WORKFLOWS itself so a new FSM becomes a tier-0 Bot by being
    added there and nowhere else -- the id list this replaces was the
    misfire the task names."""
    from prism_service.models.workflow import WORKFLOWS

    return {_FSM_ENTRY_ID.get(key, key): key for key in WORKFLOWS}


def _apply_bot_tiers(catalog: list[dict]) -> None:
    """Stamp tier/fsm_id/bot_header on the FSM Bots, a DEPTH tier on
    everything reachable below one, and an `agentic` flag on every step.

    Entries that are neither an FSM nor reachable from one get NO tier
    and NO parent_id, so they stay outside the Bot tree (AC-8).

    TIER IS DEPTH, NOT A KIND (owner 2026-09-10: "bots can call bots as
    bots are just workflows ... workflows that have workflows
    (behaviors) that have nodes"). This walked `parent_id == "conductor"`
    and nothing else, so the tree could only ever be two deep and a bot
    that calls a bot had nowhere to sit. Now a parent at tier N gives its
    children N+1, however deep the chain runs -- conductor(0) ->
    steward(1) -> plan-gate-check(2). model.ttl's o:tier carries the same
    definition so the ontology and this stamp cannot drift."""
    fsm_ids = _fsm_entry_ids()
    by_id = {entry.get("id"): entry for entry in catalog}
    for entry in catalog:
        fsm_id = fsm_ids.get(entry.get("id"))
        if fsm_id is not None:
            entry["tier"] = 0
            entry["fsm_id"] = fsm_id
            entry["bot_header"] = BOT_HEADER
    for entry in catalog:
        if entry.get("tier") == 0:
            continue
        depth, cursor, seen = 0, entry, {entry.get("id")}
        while True:
            parent = by_id.get(cursor.get("parent_id"))
            # A missing parent, or a cycle, means this entry is not
            # reachable from an FSM: leave it untiered rather than
            # inventing a depth for it.
            if parent is None or parent.get("id") in seen:
                break
            depth += 1
            seen.add(parent.get("id"))
            if fsm_ids.get(parent.get("id")) is not None:
                entry["tier"] = depth
                break
            cursor = parent
    for entry in catalog:
        for step in entry.get("steps") or []:
            # A behaviour step already decided this from its kind+url above;
            # an FSM step is agentic when its declared type is "agent".
            step.setdefault("agentic", step.get("type") == "agent")


# --- Role bots (owner 2026-09-10) -------------------------------------
# "the roles are the bots that build things in prism"; "bots are
# workflows that have nodes that perform the steps involved executing
# tasks"; "bots can call bots as bots are just workflows".
#
# SUPERSEDES the roles-are-not-bots split of task 0c396de2 (2026-08-27),
# which put the persona cards in their own list OUTSIDE the bot tree.
# That split was right that a Bot is a workflow and a card is not one;
# it was wrong that the Steward therefore is not a bot. The repair is to
# give the Steward a workflow rather than to keep it out of the tree --
# so a role bot's nodes are the conductor steps it is the seat for, and
# the behaviour behind each of those steps re-parents under it.
#
# Nothing new is invented here: `persona` already says which role owns a
# step and `linked_workflow_id` already says which behaviour runs it.
_ROLE_BOT_IDS = {"sm": "steward", "qa": "verifier", "dev": "builder"}

# Every catalog description must say WHEN its workflow runs
# (test_workflow_descriptions_say_when, rule skill-description-says-when).
# A role's `purpose` in models/roles.py says what the seat DOES, never
# when it is called, so each bot gets its trigger written here.
_ROLE_BOT_WHEN = {
    "sm": "Runs when a conductor step needs the story, needs the plan, or "
          "needs an independent decision at a gate.",
    "qa": "Runs when a conductor step must write the failing tests, or must "
          "verify the green state against a real run.",
    "dev": "Runs when a conductor step must make the smallest change that "
           "turns the failing tests green.",
}


def _role_bot_workflows(conductor: dict, project: str, svc=None) -> list[dict]:
    """One catalog entry per role bot, built from the conductor's OWN
    steps grouped by persona -- never a second hand-kept step list that
    could drift from the conductor's."""
    from prism_service.models import roles as _roles

    entries = []
    for role_id, entry_id in _ROLE_BOT_IDS.items():
        steps = [s for s in conductor["steps"] if s.get("persona") == role_id]
        if not steps:
            continue  # a role with no step it owns is not a bot here
        role = _roles.ROLES.get(role_id)
        entries.append({
            "id": entry_id,
            "name": _persona_label(role_id),
            "description": " ".join(
                p for p in ((role.purpose if role else ""),
                            _ROLE_BOT_WHEN.get(role_id, "")) if p),
            "steps": steps,
            "bots": [],
            "parent_id": "conductor",
            "occupancy": _occupancy(project, [s["id"] for s in steps], svc=svc),
        })
    return entries


def _reparent_behaviours_under_role_bots(conductor: dict, behaviours: list[dict]) -> None:
    """Move each conductor behaviour under the role bot whose step calls
    it, so the tree reads conductor -> steward -> plan-gate-check.

    A behaviour no conductor step links to (land, reap, refresh-maps,
    brain-health) has no role seat and stays a direct child of the
    conductor, which is the truth: the conductor runs it itself."""
    owner_of = {
        s["linked_workflow_id"]: _ROLE_BOT_IDS.get(s.get("persona"))
        for s in conductor["steps"] if s.get("linked_workflow_id")
    }
    for entry in behaviours:
        parent = owner_of.get(entry.get("id"))
        if parent:
            entry["parent_id"] = parent


def _attach_node_trend(scores_db, steps: list[dict]) -> None:
    """Stamp each step with its own measured token trend, in place.

    Takes an ALREADY-RESOLVED scores path rather than a project id: the
    view resolves its project exactly once per request
    (test_the_view_is_project_scoped pins that), so this must never call
    get_project itself.

    Reads the SAME node_token_trend get_workflows uses for the conductor's
    own steps, so a behaviour sub-node's multiplier/sample count means
    exactly what a conductor node's does. Degrades to an honest
    indeterminate (never a fabricated 0 or 1.0) when there is no scores.db
    -- the shape several test doubles present.
    """
    # KEY BY THE ROUTE, NOT THE STEP ID. A behaviour step is named for its
    # position in the flow ("gather", "render", "check") while the run it
    # performs is recorded under the route it calls ("premise-gather",
    # "premise-render", "premise-citation-check"). Looking the trend up by
    # id found nothing and every sub-node stayed at 0 samples even with 149
    # real runs on file -- the fields were present and empty, which reads
    # exactly like no data.
    def _key(step: dict) -> str:
        route = step.get("route")
        if route:
            return str(route)
        url = step.get("url") or step.get("action") or ""
        if "/steps/" in url:
            return url.split("/steps/")[-1].split("?")[0]
        return step["id"]

    keys = [_key(s) for s in steps]
    try:
        trend = node_token_trend(str(scores_db), keys) if scores_db else {}
    except Exception:
        trend = {}
    try:
        counts = node_run_counts(str(scores_db), keys) if scores_db else {}
    except Exception:
        counts = {}
    # DID IT RUN JUST NOW. A behaviour's sub-steps were served with a
    # hardcoded occupancy of 0, so the canvas could never light one up
    # however often it fired -- "I still don't see any activity" was true
    # of the payload, not of the work. A total count cannot answer it
    # either: 149 runs last week reads the same as one a second ago.
    try:
        recent = node_recent_runs(str(scores_db), keys) if scores_db else {}
    except Exception:
        recent = {}
    # WHEN DID IT LAST RUN. A count alone cannot answer "how recently" --
    # 149 runs last month reads the same as 149 runs ending a second ago
    # (task 1cdf1d70: "each of the four nodes shows its last run time and
    # its run total").
    try:
        last_run = node_last_run(str(scores_db), keys) if scores_db else {}
    except Exception:
        last_run = {}
    for step in steps:
        step["run_count"] = counts.get(_key(step), 0)
        step["running_now"] = bool(recent.get(_key(step), 0))
        step["last_run_at"] = last_run.get(_key(step))
        t = trend.get(_key(step)) or {}
        step["token_multiplier"] = t.get("multiplier")
        step["avg_tokens"] = t.get("avg_tokens")
        step["token_sample_count"] = t.get("sample_count", 0)
        step["token_window"] = t.get("window", NODE_TREND_WINDOW)
        step["token_indeterminate"] = t.get("indeterminate", True)


def _conductor_behavior_workflows(project: str) -> list[dict]:
    """Each of the conductor bot's AosWorkflows Behaviors, as its OWN
    catalog entry -- not one synthetic wrapper node whose fake "steps" were
    just the behavior ids. Bot [1] uses FSM [1..*], FSM [1] has Behavior
    [0..*] (see the same ontology comment in AosWorkflows' Program.cs); a
    behavior IS a flow, so it gets a real catalog entry disclosing its OWN
    real steps (e.g. land's push/open-pr), not a fake single node standing
    in for the whole thing.

    Nested under "conductor" only when a real state->behavior link exists,
    with two documented exceptions (`land`, terminal step; `validation`,
    predates this registry -- see get_workflows below for both).
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
                route = (url.split("/steps/")[-1].split("?")[0]
                         if "/steps/" in url else "")
                steps.append({
                    "id": step_id,
                    "agent": "conductor",
                    "type": "behavior",
                    # A step is agentic from WHAT IT DOES, never from what
                    # it is called (task 0c396de2). A shell step runs a
                    # fixed command; an http step INTO the reason-loop
                    # hands the work to a model. "run-tests" reaching the
                    # reason loop is agentic; "call-llm" running a shell
                    # command is not.
                    "agentic": _step_is_agentic(kind, url),
                    # The PRISM route this step calls. A behaviour step is
                    # named for its position in the flow ("gather"), while
                    # its runs are recorded under the route ("premise
                    # -gather") -- keeping the route here is what lets the
                    # canvas find them. The url itself is not carried on the
                    # built step (it becomes `action`), so without this the
                    # lookup silently fell back to the id and every node
                    # read zero.
                    "route": route,
                    "validation": "exit_code == 0",
                    "persona": "conductor",
                    "persona_label": "Conductor",
                    "purpose": step_id.replace("-", " ").replace("_", " ").capitalize(),
                    "input": "Previous step's result" if i else "The conductor bot's own repo checkout",
                    "action": command or url,
                    "output": "Captured stdout, stderr, and exit code" if kind == "shell"
                        else "HTTP response body and status",
                    # DEPTH IS NOT TWO LEVELS. A behaviour's own step may
                    # itself call a deeper behaviour, which may call another,
                    # as far down as the work actually decomposes (owner
                    # 2026-08-29: "you seem to think there are only two
                    # layers when they are infinitely [nested as] need[ed] to
                    # resolve our work"; and "bot -> (agentic flow state |
                    # bot) is progressive and infinitely hierarchical as
                    # needed").
                    #
                    # Only the conductor's own 10 states carried a link
                    # before this, from a hardcoded chain in get_workflows,
                    # so a behaviour step was always a leaf and the tree
                    # could never be deeper than conductor -> behaviour ->
                    # steps. The step's own JSON declares it now, so depth
                    # is bounded by the work, not by the renderer. The
                    # canvas already walks any depth: `workflowPath` is an
                    # appended array with per-level breadcrumbs.
                    "linked_workflow_id": (
                        step.get("linkedWorkflowId")
                        or step.get("LinkedWorkflowId")
                        or None),
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
            trigger = _BEHAVIOR_TRIGGER.get(behavior_id, _DEFAULT_BEHAVIOR_TRIGGER)
            entries.append({
                "id": behavior_id,
                "name": behavior.get("name") or behavior.get("Name") or behavior_id.replace("-", " ").title(),
                "description": f"Runs on the '{fsm_id}' fsm, executed by AosWorkflows. {trigger}",
                "steps": steps,
                "bots": [],
                "occupancy": {step["id"]: 0 for step in steps},
            })
    return entries


def _triage_workflow(project: str, svc=None) -> dict:
    """The triage workflow's own catalog entry (task b837bc98): a second,
    first-class entry beside conductor, built from
    models.workflow.WORKFLOWS["triage"] the same way conductor's own steps
    above are built from WORKFLOW_STEPS -- except persona is resolved
    directly off each step's own `agent` (falling back to "sm", the
    Steward, who owns intake/decide/done the same way it adjudicates every
    gate) rather than through roles.role_for_step/STEP_ROLES, which only
    know the implement workflow's step ids."""
    from prism_service.models.workflow import WORKFLOWS

    steps = []
    for step in WORKFLOWS["triage"]:
        persona = step["agent"] or "sm"
        content = TRIAGE_STEP_CONTENT[step["id"]]
        steps.append({
            "id": step["id"],
            "agent": step["agent"],
            "type": step["type"],
            "validation": step["validation"],
            "persona": persona,
            "persona_label": _persona_label(persona),
            "purpose": step["id"].replace("_", " ").capitalize(),
            "input": content[0],
            "action": content[1],
            "output": content[2],
            "authority": (
                "Decided by the item's owner — the single human stop in "
                "this triage flow." if step["id"] == "decide" else ""
            ),
            "execution": "connected",
            "linked_workflow_id": None,
        })
    # svc threaded from get_workflows so the view resolves the project ONCE
    # (test_the_view_is_project_scoped pins a single get_project per request).
    occupancy = _occupancy(project, [s["id"] for s in steps], svc=svc)
    return {
        "id": "triage",
        "name": "Triage",
        "description": (
            "Bucket an item and stop once for the owner's decision. "
            "Runs when a new signal or task needs a decision."
        ),
        "steps": steps,
        "bots": [],
        "occupancy": occupancy,
    }


def _align_language_workflow(project: str, svc=None) -> dict:
    """The align-language workflow's own catalog entry (task f07c9cea,
    owner rule mx-f49a5c): a fourth, first-class entry beside conductor
    and triage, built from models.workflow.WORKFLOWS["align_language"]
    the same way _triage_workflow above builds triage's. Every step's
    persona resolves off the step's own `agent` (falling back to "sm"),
    and no step carries an `authority` string, because this workflow has
    no gate — the whole pass is machine-run end to end.

    Also carries "coverage" (task c7edf4e2, epic cc9a44c8): the ingestion
    paths services.language_alignment has actually seen register a real
    STE write, read straight off its coverage() registry -- a stale or
    never-exercised path is what the SPA card (WorkflowsPage.tsx) renders
    in a warning tone."""
    from prism_service.models.workflow import WORKFLOWS
    from prism_service.services import language_alignment

    steps = []
    for step in WORKFLOWS["align_language"]:
        persona = step["agent"] or "sm"
        content = ALIGN_LANGUAGE_STEP_CONTENT[step["id"]]
        steps.append({
            "id": step["id"],
            "agent": step["agent"],
            "type": step["type"],
            "validation": step["validation"],
            "persona": persona,
            "persona_label": _persona_label(persona),
            "purpose": step["id"].replace("_", " ").capitalize(),
            "input": content[0],
            "action": content[1],
            "output": content[2],
            "authority": "",
            "execution": "connected",
            "linked_workflow_id": None,
        })
    occupancy = _occupancy(project, [s["id"] for s in steps], svc=svc)
    try:
        coverage = language_alignment.coverage(project)
    except Exception:
        coverage = []
    return {
        "id": "align_language",
        "name": "Align language",
        "description": (
            "Bring loose task text into plain Simplified Technical "
            "English — a fully machine-run pass, no owner stop. Runs "
            "when its own timer fires, sweeping every task's text for "
            "loose language."
        ),
        "steps": steps,
        "bots": [],
        "occupancy": occupancy,
        "coverage": coverage,
    }


def _promote_to_law_workflow(project: str, svc=None) -> dict:
    """The promote-to-law workflow's own catalog entry (task c5650403,
    epic 61821448: "Understand writes the law, the ontology holds it, the
    code obeys it"): a fifth first-class root workflow, built from
    models.workflow.WORKFLOWS["promote_to_law"] the same way
    _align_language_workflow builds align_language's. review is the ONE
    owner stop -- persona resolves off each step's own `agent` (falling
    back to "sm"), same as triage/align_language above."""
    from prism_service.models.workflow import WORKFLOWS

    steps = []
    for step in WORKFLOWS["promote_to_law"]:
        persona = step["agent"] or "sm"
        content = PROMOTE_TO_LAW_STEP_CONTENT[step["id"]]
        steps.append({
            "id": step["id"],
            "agent": step["agent"],
            "type": step["type"],
            "validation": step["validation"],
            "persona": persona,
            "persona_label": _persona_label(persona),
            "purpose": step["id"].replace("_", " ").capitalize(),
            "input": content[0],
            "action": content[1],
            "output": content[2],
            "authority": (
                "Decided by the owner — the single human stop in this "
                "promotion." if step["id"] == "review" else ""
            ),
            "execution": "connected",
            "linked_workflow_id": None,
        })
    occupancy = _occupancy(project, [s["id"] for s in steps], svc=svc)
    return {
        "id": "promote_to_law",
        "name": "Promote to law",
        "description": (
            "Turn a memory into a rule or a term the ontology holds, "
            "with one owner review. Runs when a memory is ready to "
            "promote to law."
        ),
        "steps": steps,
        "bots": [],
        "occupancy": occupancy,
    }


def _quickfix_workflow(project: str, svc=None) -> dict:
    """The quickfix workflow's own catalog entry (task 811fcce0, epic
    3baadd19): a sixth first-class root workflow, built from
    models.workflow.WORKFLOWS["quickfix"] the same way
    _align_language_workflow builds align_language's -- persona resolves
    off each step's own `agent` (falling back to "sm"), and no step
    carries an `authority` string, because this workflow has no gate at
    all. See models.workflow.QUICKFIX_STEPS's own doc comment for why
    verify_fix's `agent` is None (a deterministic subprocess check, not
    an LLM judgment call) even though its `type` stays "agent"."""
    from prism_service.models.workflow import WORKFLOWS

    steps = []
    for step in WORKFLOWS["quickfix"]:
        persona = step["agent"] or "sm"
        content = QUICKFIX_STEP_CONTENT[step["id"]]
        steps.append({
            "id": step["id"],
            "agent": step["agent"],
            "type": step["type"],
            "validation": step["validation"],
            "persona": persona,
            "persona_label": _persona_label(persona),
            "purpose": step["id"].replace("_", " ").capitalize(),
            "input": content[0],
            "action": content[1],
            "output": content[2],
            "authority": "",
            "execution": "connected",
            "linked_workflow_id": None,
        })
    occupancy = _occupancy(project, [s["id"] for s in steps], svc=svc)
    return {
        "id": "quickfix",
        "name": "Quickfix",
        "description": (
            "A small, already-diagnosed fix with its own oracle and "
            "pinned test -- one agentic step, one deterministic check, "
            "no gate. Runs when a task is fully scoped and small enough "
            "to skip the full conductor SDLC."
        ),
        "steps": steps,
        "bots": [],
        "occupancy": occupancy,
    }


def _knowledge_health_workflow(project: str) -> dict:
    """The Knowledge health scoreboard's own catalog entry (task
    b1971944, epic 61821448): a seventh root workflow, same posture as
    triage/align_language/promote_to_law above -- no parent_id. It has no
    steps of its own (the metrics are a live read, never a run a person
    starts), so it carries "metrics" (services/knowledge_health.py)
    instead of the step/occupancy pair every other entry above builds."""
    try:
        from prism_service.services import knowledge_health
        metrics = knowledge_health.metrics(project)
    except Exception:
        metrics = {}
    return {
        "id": "knowledge_health",
        "name": "Knowledge health",
        "description": (
            "Is Understand actually helping? Search feedback, recall-to-use, "
            "evidence, and how many rules and modules carry real "
            "provenance. Runs when a person opens the Knowledge health tab."
        ),
        "steps": [],
        "bots": [],
        "occupancy": {},
        "metrics": metrics,
    }


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
            "machine_only_gate": step["id"] in MACHINE_ONLY_GATES,
            "execution": "connected",
            "linked_workflow_id": (
                "verify-green-state-loop" if step["id"] == "verify_green_state"
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

    # A persona is a ROLE a step is assigned to, never a Bot -- a Bot is an
    # FSM (task 0c396de2). The cards move to the response's own `roles`
    # section below; what stays on a workflow is the bare persona label of
    # whoever stands at its steps.
    bots = [
        {"id": bid, "persona_label": _persona_label(bid)}
        for bid in BOT_IDS
    ]
    # The three SDLC roles PRISM assigns steps to (Steward, Verifier,
    # Builder). "architect" stays in BOT_IDS for the per-workflow persona
    # labels above; it owns no WORKFLOW_STEPS step, so it is not a role a
    # person can be standing in.
    role_cards = [
        {"id": rid, "persona_label": _persona_label(rid), "card": ROLE_CARDS[rid]}
        for rid in ("sm", "qa", "dev")
    ]

    # Resolve the project ONCE for this view: test_the_view_is_project_scoped
    # pins exactly one get_project per request, and both occupancy and the
    # per-workflow task_count below read the same task service.
    try:
        _proj = get_project(project)
        _svc = _proj.task_svc
    except Exception as exc:
        raise HTTPException(404, f"unknown project: {project}: {exc}")
    occupancy = _occupancy(project, [s["id"] for s in steps], svc=_svc)
    # Per-node measured multiplier + trailing token trend (task 112dbb72,
    # owner: "each programmatic node is a token multiplier"). Read straight
    # off the agent_runs spine for these exact step ids, ceiling-filtered
    # against pre-fix corrupt rows (task fc471aed) -- never derived from a
    # step's declared type/agent, only from what its own runs measured.
    # `_data_dir` is absent on the bare test doubles several suites pass as
    # `get_project`'s return value (they only stub `.task_svc`) -- an
    # honest empty trend there, same as a node with zero runs, rather than
    # a 500 for a feature those suites never asked about.
    _scores_db = getattr(_proj, "_data_dir", None)
    trend = (node_token_trend(str(_scores_db / "scores.db"), [s["id"] for s in steps])
             if _scores_db is not None else {})
    for step in steps:
        t = trend.get(step["id"]) or {}
        step["token_multiplier"] = t.get("multiplier")
        step["avg_tokens"] = t.get("avg_tokens")
        step["token_sample_count"] = t.get("sample_count", 0)
        step["token_window"] = t.get("window", NODE_TREND_WINDOW)
        step["token_indeterminate"] = t.get("indeterminate", True)
    conductor = {
        "id": "conductor",
        "name": "Conductor",
        "description": (
            "PRISM delivery workflow. Runs when a task moves through "
            "story, plan, red, and green steps to a shipped change."
        ),
        "steps": steps,
        "bots": bots,
        "occupancy": occupancy,
    }
    validation = _project_validation_workflow(project)
    # Nested, not a flat sibling of conductor: this IS the conductor's own
    # capability, it simply predates the Bot/Behavior registry and is
    # sourced differently. UPDATED task 25b2a05c: verify_green_state's
    # linked_workflow_id now points at its own honest node
    # (verify-green-state-loop, below) instead of here -- validation stays
    # nested for browsability (a person can still open the project's real
    # build+test workflow from the conductor directory), it just is not
    # the step that lights up when a task is standing at verify_green_state.
    validation["parent_id"] = "conductor"
    conductor_behaviors = _conductor_behavior_workflows(project)
    from prism_service.services import drive_heartbeat
    # A BEHAVIOUR'S SUB-STEPS CARRY THEIR OWN MEASURED TREND. The trend
    # above is computed for the conductor's ten FSM steps only, so every
    # behaviour sub-node on the canvas read "too few runs (0/20)" however
    # often it ran -- and these run constantly: premise-gather had 149
    # recorded runs and premise-citation-check 147 while the canvas showed
    # nothing for either. The rows were always there; nobody read them.
    # Stamped HERE, off the path this view already resolved, so the project
    # is still resolved exactly once per request.
    #
    # LIVE-HEARTBEAT LOOKUP, COMPUTED ONCE (task b490fabc, second pass).
    # `running_now` below is retrospective only: node_recent_runs reads a
    # route's row in scores.db, written once the call RETURNS -- so a long
    # agentic dispatch (implement-tasks-loop's reason-loop ran 90+ minutes
    # and 108 turns on a single still-open HTTP call) reads idle for the
    # entire time it is actually running, because there is no completed
    # row yet to find. The owner watched this exact node paint "000" the
    # whole time (2026-09-11/12). The task's own drive heartbeat is the
    # same live signal /api/conductor/state's "driving" badge already
    # trusts -- so light a behaviour's entry node whenever a live,
    # non-stale task is parked at the FSM step that behaviour answers for
    # (_STEP_FOR_BEHAVIOUR), the same fallback the canvas itself already
    # documents: on a drilled layer with no per-step WorkflowCore run
    # behind it, occupancy is the only answer to "where is the work"
    # (workflowGraph.ts). FIRST VERSION of this called drive_heartbeat.
    # latest() and svc.list() inside the entry loop -- one sqlite connect
    # (with its own schema-check/ALTER TABLE) and one full task listing PER
    # BEHAVIOUR ENTRY PER CANDIDATE TASK. Measured live against this same
    # instance under real write contention: >90s, still not returned,
    # against ~5-50s for the same endpoint before -- reverted within
    # minutes of shipping it. Both loops now run exactly once, before the
    # per-entry pass, however many behaviour entries there are.
    _active_task_steps: dict[str, str] = {}
    for _t in _svc.list():
        if getattr(_t, "status", "") in ("done", "cancelled", "deleted"):
            continue
        _tid = getattr(_t, "id", "")
        if _tid:
            _active_task_steps[_tid] = getattr(_t, "workflow_step", "") or ""
    _heartbeats = (
        drive_heartbeat.latest_many(
            str(_scores_db / "scores.db"), _active_task_steps.keys())
        if _scores_db is not None and _active_task_steps else {})
    for _entry in conductor_behaviors:
        _steps = _entry.get("steps") or []
        _attach_node_trend(
            (_scores_db / "scores.db") if _scores_db is not None else None,
            _steps)
        # The canvas reads `occupancy` to decide what is live. It was a
        # dict of zeros built at construction time, which is why a node
        # that had just run still drew as idle.
        _entry["occupancy"] = {
            s["id"]: (1 if s.get("running_now") else 0) for s in _steps}
        _fsm_step = _STEP_FOR_BEHAVIOUR.get(_entry.get("id"))
        if _fsm_step and _steps:
            _entry_point_id = _steps[0]["id"]
            for _tid, _step in _active_task_steps.items():
                if _step != _fsm_step:
                    continue
                _beat = _heartbeats.get(_tid)
                if _beat is not None and _beat["age_s"] <= drive_heartbeat.HEARTBEAT_WINDOW_S:
                    _entry["occupancy"][_entry_point_id] = 1
                    break
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
        "write-failing-tests-loop", "implement-tasks-loop", "red-test-ids",
        "verify-green-state-loop",
        "red-gate-status", "green-gate-status", "land",
        # "reap" (task f97c196d): the step AFTER land, and the FSM's real
        # terminal node -- a drive that shipped still left its git worktree
        # and its prism/ws/<task_id> branch behind forever (256 worktrees,
        # 474 branches measured 2026-08-30). It nests through this set for
        # exactly the same reason land does: green_gate is the structurally
        # -terminal WORKFLOW_STEPS entry, so there is no step to hang a
        # linked_workflow_id on, and models/workflow.py is read by
        # conductor_service.py (a control_plane.POLICY_FILES entry).
        # "brain-health" (task 013c5197): the knowledge-side counterpart to
        # reap. reap cleans the repository a finished play leaves behind;
        # this one cleans the knowledge -- it indexes what the play wrote
        # and fails when coverage falls below its floor. It nests here for
        # the same structural reason land and reap do: green_gate is the
        # terminal WORKFLOW_STEPS entry, so there is no step to hang a
        # linked_workflow_id on. Without this the behaviour JSON exists and
        # renders nowhere, which is the built-but-unwired fault this node
        # was created to catch -- four such mechanisms shipped on
        # 2026-08-30 with no caller at all.
        "brain-health",
        # reap stays LAST: it deletes the worktree, so the knowledge-side
        # node must run before the workspace is destroyed.
        "reap",
    )
    for entry in conductor_behaviors:
        if entry["id"] in _CONDUCTOR_LINKED_BEHAVIOR_IDS:
            entry["parent_id"] = "conductor"

    # triage (task b837bc98): a second, first-class root workflow beside
    # conductor -- not one of conductor's own nested capabilities, so it
    # gets no parent_id, same as conductor and validation above.
    triage = _triage_workflow(project, svc=_svc)
    # align_language (task f07c9cea): a fifth root workflow, same posture
    # as triage above -- no parent_id, since it is not one of conductor's
    # own nested capabilities.
    align_language = _align_language_workflow(project, svc=_svc)
    # quickfix (task 811fcce0): a sixth root workflow, same posture as
    # triage/align_language above -- no parent_id, since it is not one of
    # conductor's own nested capabilities.
    quickfix = _quickfix_workflow(project, svc=_svc)
    # promote_to_law (task c5650403): a seventh root workflow, same posture
    # as triage/align_language above -- no parent_id.
    promote_to_law = _promote_to_law_workflow(project, svc=_svc)
    # knowledge_health (task b1971944): an eighth root workflow, same
    # posture -- no parent_id.
    knowledge_health = _knowledge_health_workflow(project)
    # Role bots (owner 2026-09-10): the Steward/Verifier/Builder sit
    # BETWEEN the conductor and the behaviours its steps call, so the
    # tree shows a bot calling a bot. Built and re-parented before the
    # catalog is tiered, because _apply_bot_tiers reads parent_id.
    role_bots = _role_bot_workflows(conductor, project, svc=_svc)
    _reparent_behaviours_under_role_bots(conductor, conductor_behaviors)
    catalog = [conductor, validation, triage, align_language, quickfix,
              promote_to_law, knowledge_health, *role_bots,
              *conductor_behaviors]
    # task_count (task af396b2c): the queue standing behind each catalog
    # entry -- see _task_count_by_workflow's docstring for the alias join.
    _counts = _task_count_by_workflow(project, [entry["id"] for entry in catalog], svc=_svc)
    for entry in catalog:
        entry["task_count"] = _counts.get(entry["id"], 0)
    # Tier the catalog LAST, once every entry (and every parent_id) is on
    # it -- see _apply_bot_tiers.
    _apply_bot_tiers(catalog)

    return {
        "steps": steps,
        "bots": bots,
        "roles": role_cards,
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


@router.get("/{workflow_id}/runs")
def get_workflow_runs(
    workflow_id: str, project: str = Query(...), task_id: str = Query(""),
) -> dict:
    """The STORED node executions for one task on one flow (task 8fbd5cf0).

    A read path only: it serves what each node said AT DECISION TIME. It
    never re-runs a tooth (stop_if: "a node panel recomputes a check
    instead of reading the stored execution") and it never reverse-maps
    task_history the way /{workflow_id}/instances does.
    """
    from prism_service.services import flow_run_recorder

    scores_db = str(get_project(project)._data_dir / "scores.db")
    runs = flow_run_recorder.runs_for_task(scores_db, task_id, workflow_id)
    step = str(runs[-1]["node_id"]) if runs else ""
    return {"workflow_id": workflow_id, "task_id": task_id,
            "progress": flow_run_recorder.progress_source(
                scores_db, task_id, step, project=project) if task_id else None,
            "nodes": list(flow_run_recorder.CONDUCTOR_NODES),
            "flow_version": flow_run_recorder.flow_version_for(workflow_id),
            "finished": flow_run_recorder.is_finished(runs),
            "visible": flow_run_recorder.is_visible(runs),
            "runs": runs}


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


class DecideGateCheckRequest(BaseModel):
    task_id: str


class DecideGateCheckResponse(BaseModel):
    ok: bool
    reason: str


@router.post("/steps/decide-gate-check")
def workflow_step_decide_gate_check(
    body: DecideGateCheckRequest, project: str = Query(...),
) -> DecideGateCheckResponse:
    """Read-only: wraps services.triage_decision.score behind a typed
    contract, the same shape as /steps/story-gate-check above. Scores the
    classification the triage `classify` step produced. Writes nothing and
    decides nothing -- the real decision runs in the gate_adjudicator sweep
    via triage_decision.adjudicate. This endpoint exists so the conductor
    directory can show a real, callable behaviour for `decide` instead of
    nothing (task edeab040)."""
    with _tracer.start_as_current_span("workflow.step.decide_gate_check") as span:
        span.set_attribute("workflow.step.id", "decide-gate-check")
        span.set_attribute("workflow.project", project)
        span.set_attribute("workflow.task.id", body.task_id)
        from prism_service.project_context import get_project
        from prism_service.services import triage_decision
        task = get_project(project).task_svc.get(body.task_id)
        if task is None:
            return DecideGateCheckResponse(
                ok=False, reason=f"no task {body.task_id!r} in {project!r}")
        ok, reason = triage_decision.score(task)
        return DecideGateCheckResponse(ok=ok, reason=reason)


# ONE shape for every named gate tooth -- green_gate's registry
# (_green_gate_check_registry) and plan_gate's deterministic teeth
# (services/plan_gate_checks.py) both report through it, so the Workflows
# page renders a real node per check for either gate.
class GateCheckStatus(BaseModel):
    id: str
    label: str
    ok: bool
    reason: str


class PlanGateCheckRequest(BaseModel):
    plan_doc: str
    plan_diagram: str = ""
    task_id: str = ""


class PlanGateCheckResponse(BaseModel):
    ok: bool
    reason: str
    checks: list[GateCheckStatus] = []


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
        # The rubric scores FORM. `checks` adds the three DETERMINISTIC
        # teeth the same machine seats now consult (task 72ccaf94: five
        # rounds at plan_gate, every defect caught by a human's own eyes).
        # Reported here so the aggregate answer is the whole picture, the
        # way green-gate-status already reports its own registry.
        checks: list[GateCheckStatus] = []
        task_svc = getattr(ctx, "task_svc", None)
        if body.task_id and task_svc is not None:
            from prism_service.services import plan_gate_checks as pgc
            task = task_svc.get(body.task_id)
            if task is not None:
                checks = [GateCheckStatus(**e)
                          for e in pgc.run_all(task, project)]
        ok = bool(result.get("ok", False)) and all(c.ok for c in checks)
        reason = str(result.get("reason", "") or "")
        failed = " | ".join(c.reason for c in checks if not c.ok)
        if failed:
            reason = f"{reason} | {failed}" if reason else failed
        return PlanGateCheckResponse(ok=ok, reason=reason, checks=checks)


# Default character budget for rendered conventions (env PRISM_CONTEXT_CONVENTIONS_CHARS).
DEFAULT_CONVENTIONS_CHARS = 1500


def _render_conventions(conventions: list | None) -> str:
    """Render a conventions list to compact plain text suitable for an
    inference prompt. Mirrors context_builder._conventions_cap pattern.

    Takes a list of convention objects (dataclass-like with getattr or dict
    with .get()) and produces one-line summaries: NAME | DESCRIPTION.
    Entries may be truncated or omitted if total budget is exceeded.
    Silently handles missing fields and gracefully degrades on errors.

    Returns empty string if conventions is None/empty, never a dangling
    "Project conventions:" header."""
    if not conventions:
        return ""

    # Read the character budget from env, defaulting to the module constant.
    try:
        budget = int(os.environ.get("PRISM_CONTEXT_CONVENTIONS_CHARS", ""))
        if budget <= 0:
            budget = DEFAULT_CONVENTIONS_CHARS
    except (TypeError, ValueError):
        budget = DEFAULT_CONVENTIONS_CHARS

    max_desc_len = 200  # Truncate individual descriptions to this length.
    lines = []
    dropped = 0
    truncated_count = 0
    total_chars = 0

    for entry in conventions:
        # Handle both dataclass-like objects and dicts.
        if isinstance(entry, dict):
            name = entry.get("name", "") or ""
            description = entry.get("description", "") or ""
        else:
            name = getattr(entry, "name", "") or ""
            description = getattr(entry, "description", "") or ""

        if not name:
            continue

        # Truncate description if necessary and mark truncation.
        desc_truncated = False
        if len(description) > max_desc_len:
            description = description[:max_desc_len].rstrip() + "…"
            desc_truncated = True
            truncated_count += 1

        line = f"• {name}: {description}".strip() if description else f"• {name}"

        # Check if adding this line would exceed the budget.
        line_len = len(line) + 1  # +1 for newline
        if total_chars + line_len > budget:
            dropped += 1
        else:
            lines.append(line)
            total_chars += line_len

    result = "\n".join(lines)

    # Append a note if entries were dropped or truncated. Reserve a small
    # buffer in the budget for the note itself (~100 chars).
    if dropped > 0 or truncated_count > 0:
        note_lines = []
        if dropped > 0:
            note_lines.append(f"({dropped} more entries omitted to stay within budget)")
        if truncated_count > 0:
            note_lines.append("(some descriptions truncated)")
        if note_lines:
            note_text = " ".join(note_lines)
            result = f"{result}\n\n{note_text}" if result else note_text

    return result


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
    if rubric_name == "test_drafted":
        return gov.score_test_drafted({"test_code": fields.get("test_code", ""),
                                        "test_file_path": fields.get("test_file_path", "")}, rubric)
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
    # NARROW BY DEFAULT (task eda5a843). Every declared agentic middle on the
    # conductor bot is a no-tool text generation -- it is handed its material
    # and asked to write a document. Leaving tools on costs the ~20k-token
    # interactive-agent envelope the workspace CLAUDE.md carries (measured
    # 7.13.289: 28,560 -> 8,159 input tokens on this very step) and buys
    # nothing, because the step reports a document, not a file edit. A caller
    # that genuinely needs to read the tree sets this False.
    narrow: bool = True


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
        conventions_text = _render_conventions(bundle.get("conventions"))
        full_prompt = f"{body.prompt}\n\nProject conventions:\n{conventions_text}" if conventions_text else body.prompt
        invoke_kwargs = {"allowed_tools": ()} if body.narrow else {}
        result = claude_cli.invoke(
            full_prompt, work_dir=root, plugin_dir=root,
            model=body.model, max_budget_usd=body.max_budget_usd, max_turns=body.max_turns,
            project=project, purpose="reason-loop",
            json_schema=body.json_schema, **invoke_kwargs,
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


# ----------------------------------------------------------------------
# review_previous_notes, leveled up (task cd33263f)
# ----------------------------------------------------------------------
# Owner: "how can we level up more nodes moving faster programmatically,
# finish tasks faster with less tokens as you find issues" / "enough
# agentic to generate the content for the task, but always striving to
# ensure maximum correct throughput." review_previous_notes ran FIRST on
# every task as ONE opaque reason-loop call whose prompt told the model to
# go "review the prior notes/decisions" with only Read/Glob/Grep
# (claude_cli.READ_ONLY_TOOLS) -- no memory_recall, no brain_search, no
# task history -- so grounding a citation meant grepping the repo cold,
# one tool-call round trip per claim, on every single task. Split into
# three nodes, agentic ONLY in the middle:
#   /steps/premise-gather          codified -- resolves real citations
#   /steps/premise-judge           agentic  -- judges load-bearing facts
#   /steps/premise-citation-check  codified -- verifies the report's shape
# Both codified steps call prism_service.services.premise_gather -- pure
# retrieval/regex, no model call, no repo lock or worktree op (the
# 2026-08-29 daemon wedge this must not reproduce).

class PremiseGatherRequest(BaseModel):
    task_id: str = Field(min_length=1)


class GatheredFactOut(BaseModel):
    kind: str
    text: str
    citation: str


class PremiseGatherResponse(BaseModel):
    facts: list[GatheredFactOut] = []
    reason: str = ""


@router.post("/steps/premise-gather")
def workflow_step_premise_gather(
    body: PremiseGatherRequest, project: str = Query(...),
) -> PremiseGatherResponse:
    """CODIFIED. Collects related memories, prior decisions on this task
    and its neighbours, and resolvable file:line references for symbols
    the task names -- every citation is one this step ACTUALLY resolved
    from a real row (memory_svc.recall / task_svc.history / task_svc.list
    / brain_svc.find_symbol, each a plain local read); nothing is
    invented. Never calls a model. An honest empty result carries a named
    `reason` instead of a silently empty list."""
    with _tracer.start_as_current_span("workflow.step.premise_gather") as span:
        span.set_attribute("workflow.project", project)
        span.set_attribute("workflow.task.id", body.task_id)

        ctx = get_project(project)
        task = ctx.task_svc.get(body.task_id)
        if task is None:
            return PremiseGatherResponse(
                facts=[], reason=f"no such task: {body.task_id}")

        from prism_service.services import premise_gather as pg
        facts = pg.gather(
            task, memory_svc=getattr(ctx, "memory_svc", None),
            task_svc=ctx.task_svc, brain_svc=getattr(ctx, "brain_svc", None))
        reason = ("" if facts else
                   "no memories, prior decisions, or resolvable symbols "
                   "found for this task")
        return PremiseGatherResponse(
            facts=[GatheredFactOut(kind=f.kind, text=f.text, citation=f.citation)
                   for f in facts],
            reason=reason)


class PremiseJudgeRequest(BaseModel):
    task_id: str = Field(min_length=1)
    model: str = "haiku"
    max_budget_usd: float = 0.5
    max_turns: int = 2


class PremiseJudgeResponse(BaseModel):
    facts_used: int
    reason: dict
    validation: dict


@router.post("/steps/premise-judge")
def workflow_step_premise_judge(
    body: PremiseJudgeRequest, project: str = Query(...),
) -> PremiseJudgeResponse:
    """AGENTIC -- the one model call left in review_previous_notes.
    Gathers the SAME facts /steps/premise-gather resolves, then asks the
    model ONLY to judge which are load-bearing for this task and to reuse
    each one's citation VERBATIM -- never to go find citations itself.
    `allowed_tools=()`: unlike the old single-step reason-loop call (up to
    4 turns of Read/Glob/Grep hunting for evidence), this call needs zero
    tool round trips because the facts already carry real citations."""
    with _tracer.start_as_current_span("workflow.step.premise_judge") as span:
        span.set_attribute("workflow.project", project)
        span.set_attribute("workflow.task.id", body.task_id)

        ctx = get_project(project)
        task = ctx.task_svc.get(body.task_id)
        if task is None:
            return PremiseJudgeResponse(
                facts_used=0, reason={"ok": False, "fields": {}},
                validation={"ok": False, "reason": f"no such task: {body.task_id}"})

        from prism_service.services import premise_gather as pg
        facts = pg.gather(
            task, memory_svc=getattr(ctx, "memory_svc", None),
            task_svc=ctx.task_svc, brain_svc=getattr(ctx, "brain_svc", None))
        facts_md = "\n".join(
            f"- ({f.kind}) {f.text} — {f.citation}" for f in facts
        ) or "(nothing gathered -- mark any claim you make UNVERIFIED)"

        prompt = (
            "Material already GATHERED for you is below; every line already "
            "carries a real citation. Decide which are load-bearing for this "
            "task and report them as a '## Premises' markdown list, one "
            "bullet per claim, reusing its citation VERBATIM. Never invent a "
            "new citation. You may add a claim of your own only if you mark "
            "it UNVERIFIED or REFUTED.\n\n"
            f"Task: {task.title}\n{task.description}\n\n"
            f"Gathered material:\n{facts_md}"
        )

        from pathlib import Path
        from prism_service.services.claude_transcripts import _project_source_path
        from prism_service.inference import claude_cli

        configured = Path(_project_source_path(project))
        fallback = Path.home() / "projects" / project
        root = configured if configured.is_absolute() and configured.exists() else fallback
        result = claude_cli.invoke(
            prompt, work_dir=root, plugin_dir=root,
            model=body.model, max_budget_usd=body.max_budget_usd,
            max_turns=body.max_turns, allowed_tools=(),
            project=project, purpose="premise-judge",
            json_schema={"type": "object",
                        "properties": {"notes_md": {"type": "string"}},
                        "required": ["notes_md"]},
        )
        fields = result.structured_output or {}
        reason = {"ok": bool(fields), "fields": fields,
                  "cost_usd": result.usage.get("cost_usd", 0.0),
                  "run_id": result.run_id}

        from prism_service.services import arc_governance as gov
        rubric = gov.load_rubrics().get("premise_grounded") or {}
        verdict = gov.score_premise_grounded(
            {"notes_md": fields.get("notes_md", "")}, rubric)
        validation = {"ok": verdict.get("ok", False), "reason": verdict.get("reason", "")}

        return PremiseJudgeResponse(facts_used=len(facts), reason=reason, validation=validation)


class PremiseCitationCheckRequest(BaseModel):
    notes_md: str = ""
    task_id: str = ""


class PremiseCitationCheckFailing(BaseModel):
    claim: str
    reason: str


class PremiseCitationCheckResponse(BaseModel):
    ok: bool
    section_present: bool
    claims_checked: int
    failing: list[PremiseCitationCheckFailing] = []
    reason: str


@router.post("/steps/premise-citation-check")
def workflow_step_premise_citation_check(
    body: PremiseCitationCheckRequest, project: str = Query(...),
) -> PremiseCitationCheckResponse:
    """CODIFIED. Verifies every claim bullet under review_previous_notes'
    Premises section ends with a citation or an explicit
    REFUTED/UNVERIFIED/UNRESOLVED marker, and names the bullets that
    fail. Pure regex (reuses the SAME grounding predicates
    arc_governance.score_premise_grounded enforces at story_gate, by
    import -- never a copy that can drift). Never calls a model. When
    `notes_md` is omitted, reads task.premise_notes for `task_id`."""
    with _tracer.start_as_current_span("workflow.step.premise_citation_check") as span:
        span.set_attribute("workflow.project", project)
        if body.task_id:
            span.set_attribute("workflow.task.id", body.task_id)

        notes_md = body.notes_md
        if not notes_md.strip() and body.task_id:
            ctx = get_project(project)
            task = ctx.task_svc.get(body.task_id)
            notes_md = getattr(task, "premise_notes", "") or "" if task else ""

        from prism_service.services import premise_gather as pg
        from prism_service.services import arc_governance as gov
        rubric = gov.load_rubrics().get("premise_grounded") or {}
        section_name = rubric.get("claims_section", "premises")
        result = pg.citation_check(notes_md, claims_section=section_name)
        return PremiseCitationCheckResponse(
            ok=result["ok"], section_present=result["section_present"],
            claims_checked=result["claims_checked"],
            failing=[PremiseCitationCheckFailing(**f) for f in result["failing"]],
            reason=result["reason"])


class PremiseSelectRequest(BaseModel):
    task_id: str = Field(min_length=1)
    keep_max: int = Field(default=0, ge=0, le=50)


class SelectedFactOut(BaseModel):
    kind: str
    text: str
    citation: str


class PremiseSelectResponse(BaseModel):
    kept: list[SelectedFactOut] = []
    dropped: list[SelectedFactOut] = []
    reason: str = ""


@router.post("/steps/premise-select")
def workflow_step_premise_select(
    body: PremiseSelectRequest, project: str = Query(...),
) -> PremiseSelectResponse:
    """CODIFIED. Chooses the load-bearing facts to assert, and names the
    ones it leaves out. Never calls a model.

    THE REFUSAL THIS REPLACES (task 6738006b). The premise chain used to
    give up whenever `gather` resolved more than four facts and hand the
    step to the paid judge. Its reason was sound -- a wide set needs
    SELECTING -- but it refused on COUNT without ever asking whether the
    render would pass. Measured 2026-09-08 over the 10 tasks blocked at
    review_previous_notes: 7 were refused by that bail, and 6 of the 7
    render a section arc_governance.score_premise_grounded ACCEPTS at zero
    tokens. Task 1bcb2b24 was one of them, and the judge it fell through
    to then failed 6 dispatches and parked the task 3 times.

    COVERAGE FIRST, so the bound is safe: a fact earns its slot by
    engaging an oracle clause no kept fact engages yet, scored with
    arc_governance's own `_clause_words`/`oracle_clauses` against the same
    threshold the real tooth applies. Remaining slots go to the facts
    sharing the most vocabulary with the ticket. The bound itself stays --
    asserting every retrieved fact is the noise generator this node exists
    to avoid."""
    with _tracer.start_as_current_span("workflow.step.premise_select") as span:
        span.set_attribute("workflow.project", project)
        span.set_attribute("workflow.task.id", body.task_id)

        ctx = get_project(project)
        task = ctx.task_svc.get(body.task_id)
        if task is None:
            return PremiseSelectResponse(
                reason=f"no such task: {body.task_id}")

        from prism_service.services import premise_gather as pg
        facts = pg.gather(
            task, memory_svc=getattr(ctx, "memory_svc", None),
            task_svc=ctx.task_svc, brain_svc=getattr(ctx, "brain_svc", None))
        if not facts:
            return PremiseSelectResponse(
                reason=("no memories, prior decisions, or resolvable symbols "
                        "found for this task, so there is nothing to select"))

        chosen = pg.select(
            task, facts,
            keep_max=body.keep_max or pg.DEFAULT_KEEP_MAX)
        span.set_attribute("workflow.premise.kept", len(chosen.kept))
        span.set_attribute("workflow.premise.dropped", len(chosen.dropped))

        def _out(items):
            return [SelectedFactOut(kind=f.kind, text=f.text,
                                    citation=f.citation) for f in items]

        return PremiseSelectResponse(kept=_out(chosen.kept),
                                     dropped=_out(chosen.dropped),
                                     reason=chosen.reason)


class PremiseRenderRequest(BaseModel):
    task_id: str = Field(min_length=1)
    notes_md: str = ""


class PremiseRenderResponse(BaseModel):
    premises_md: str = ""
    facts_used: int = 0
    repaired: bool = False
    reason: str = ""


@router.post("/steps/premise-render")
def workflow_step_premise_render(
    body: PremiseRenderRequest, project: str = Query(...),
) -> PremiseRenderResponse:
    """CODIFIED. Builds the Premises section from the facts `gather` already
    resolved, so this node stops depending on a model to retype them.

    THE LOSS CONDITION THIS CLOSES: premise_grounded refused 273 advances
    across 141 tasks (measured 2026-09-05), each burning a whole drive over
    facts PRISM already held. `premise-citation-check` only REPORTED that --
    it is advisory, so the step parked anyway. This node produces the
    section instead of naming its absence.

    Honest by construction: every citation comes from a real GatheredFact
    and none is invented; an oracle clause no fact engages is marked
    `clause N: UNRESOLVED, <why>`, the marker arc_governance's own
    oracle-engagement tooth names for a real gap. With nothing gathered it
    returns empty rather than asserting claims with no source. A report that
    already grounds every claim is returned untouched with repaired=false --
    a model that got it right is never overwritten. Never calls a model."""
    with _tracer.start_as_current_span("workflow.step.premise_render") as span:
        span.set_attribute("workflow.project", project)
        span.set_attribute("workflow.task.id", body.task_id)

        ctx = get_project(project)
        task = ctx.task_svc.get(body.task_id)
        if task is None:
            return PremiseRenderResponse(reason=f"no such task: {body.task_id}")

        from prism_service.services import premise_gather as pg

        notes_md = body.notes_md
        if not notes_md.strip():
            notes_md = getattr(task, "premise_notes", "") or ""
        if notes_md.strip() and pg.citation_check(notes_md).get("ok"):
            return PremiseRenderResponse(
                premises_md=notes_md, repaired=False,
                reason="the report already grounds every claim; left untouched")

        facts = pg.gather(
            task, memory_svc=getattr(ctx, "memory_svc", None),
            task_svc=ctx.task_svc, brain_svc=getattr(ctx, "brain_svc", None))
        rendered = pg.render_premises(task, facts)
        if not rendered.strip():
            return PremiseRenderResponse(
                facts_used=0, repaired=False,
                reason="nothing gathered for this task -- refusing to assert "
                       "claims that have no source")
        return PremiseRenderResponse(
            premises_md=rendered, facts_used=len(facts), repaired=True,
            reason=f"rendered {len(facts)} grounded premise(s) from the "
                   "codified gather")


# ----------------------------------------------------------------------
# CHALLENGE THE TEXT A NODE JUST WROTE (task d7947eb6)
# ----------------------------------------------------------------------
# Owner: "we must make sure all of the text generation nodes that hit
# artifacts use the ontology rules, they should have separate nodes to
# challenge, correct or provide access to prevent this from happening
# again." Same three-node shape review_previous_notes already uses --
# codified gather, agentic judge, codified check -- applied to every
# text-generating node: the generating step runs, then this CODIFIED
# step judges what it wrote with the LIVE ontology rules
# (services/text_challenge.py runs ontology_rules.run_shapes over a
# one-node probe graph, so the verdict is the same SPARQL the Rules tab
# runs), repairs what a machine can repair safely, and NAMES what it
# must not repair. Never a model call.


class TextChallengeRequest(BaseModel):
    task_id: str = Field(min_length=1)
    step_id: str = Field(min_length=1)


class TextChallengeViolation(BaseModel):
    field: str
    name: str
    message: str


class TextChallengeResponse(BaseModel):
    step: str
    repaired: list[str] = []
    unrepaired: list[TextChallengeViolation] = []
    fields_checked: list[str] = []
    reason: str


@router.post("/steps/text-challenge")
def workflow_step_text_challenge(
    body: TextChallengeRequest, project: str = Query(...),
) -> TextChallengeResponse:
    """CODIFIED. Judges the artifact `step_id` just wrote against the
    live ontology text rules, repairs what the deterministic normaliser
    and the lexicon aligner can repair without changing a claim, and
    reports every violation neither can fix. Holds an oracle, a stop_if,
    a heading, a table row and every hedge byte-identical. Never calls a
    model."""
    with _tracer.start_as_current_span("workflow.step.text_challenge") as span:
        span.set_attribute("workflow.project", project)
        span.set_attribute("workflow.task.id", body.task_id)
        span.set_attribute("workflow.step.id", body.step_id)

        from prism_service.services import text_challenge

        ctx = get_project(project)
        report = text_challenge.challenge_step_artifacts(
            ctx.task_svc, body.task_id, body.step_id, project=project)
        return TextChallengeResponse(
            step=report["step"], repaired=report["repaired"],
            unrepaired=[TextChallengeViolation(**v)
                        for v in report["unrepaired"]],
            fields_checked=sorted(report["fields"]),
            reason=report["reason"])


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


class RedTestIdsRequest(BaseModel):
    task_id: str = Field(min_length=1)


class RedTestIdsResponse(BaseModel):
    red_test_ids: list[str] = []
    anchor_sha: str = ""
    reason: str = ""


@router.post("/steps/red-test-ids")
def workflow_step_red_test_ids(
    body: RedTestIdsRequest, project: str = Query(...),
) -> RedTestIdsResponse:
    """CODIFIED implement_tasks helper -- names the red test ids from data
    PRISM already holds, never from a model. Task 404ef4ce (owner
    2026-08-30: "make a new codified step in the workflow, that's how we
    play, making maximum codified nodes from agentic blocks so we can not
    stall"). Task 8fbd5cf0 held a complete implementation and stalled
    anyway because implement-tasks-loop is AGENTIC (reason-loop prose) and
    the runner's stall-splitter greps that prose for a pytest node id --
    a fact a model has to retype instead of read off the system that
    already has it (task_runner.red_test_ids / _TEST_ID_RE).

    Same pattern as workflow_step_red_gate_status: pure reads over the
    exact functions the real red-gate path already trusts --
    oracle_spec.OracleSpec.from_task for task.verify's pinned targets,
    ConductorService._red_step_sha for the anchor, oracle_spec.
    fresh_red_receipt for the demonstration. NEVER calls a model, NEVER
    runs pytest, NEVER takes a worktree or repo lock inside this request
    handler (2026-08-29 wedged the whole daemon that way once already,
    see workflow_node_status's plan-gate-check branch) -- when no fresh
    red receipt is on file, this reports an honest empty result naming
    why, rather than guessing or invoking the trusted runner itself."""
    from prism_service.services import oracle_spec as osp

    with _tracer.start_as_current_span("workflow.step.red_test_ids") as span:
        span.set_attribute("workflow.project", project)
        span.set_attribute("workflow.task.id", body.task_id)

        ctx = get_project(project)
        task = ctx.task_svc.get(body.task_id)
        if task is None:
            return RedTestIdsResponse(reason=f"no such task: {body.task_id}")

        spec = osp.OracleSpec.from_task(task)
        if spec.adapter != osp.ADAPTER_PYTEST:
            return RedTestIdsResponse(
                reason="task's derived oracle spec is not pytest-backed "
                       f"(adapter={spec.adapter}) -- no pytest node ids "
                       "to name")

        pinned = [t for t in spec.target.split() if t]
        if not pinned:
            return RedTestIdsResponse(
                reason="task.verify names no pytest node ids or test paths")

        red_sha = ctx.conductor_svc._red_step_sha(body.task_id)
        if not red_sha:
            return RedTestIdsResponse(
                reason="no red-step commit resolved yet -- "
                       "write_failing_tests hasn't landed a tests-only commit")

        fresh = osp.fresh_red_receipt(
            project, body.task_id, red_sha, spec.spec_hash())
        if fresh is None:
            return RedTestIdsResponse(
                anchor_sha=red_sha,
                reason="no fresh red receipt for the current red-step "
                       f"commit ({red_sha[:12]}) -- nothing observed "
                       "failing there yet")

        return RedTestIdsResponse(
            red_test_ids=pinned, anchor_sha=red_sha,
            reason=f"red demonstrated at {red_sha[:12]}: {fresh.reason}")


class GreenGateStatusRequest(BaseModel):
    task_id: str = Field(min_length=1)


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


class PlanGateCheckOneRequest(BaseModel):
    task_id: str = Field(min_length=1)
    check: str = Field(min_length=1)


@router.post("/steps/plan-gate-check-one")
def workflow_step_plan_gate_check_one(
    body: PlanGateCheckOneRequest, project: str = Query(...),
) -> GateCheckStatus:
    """ONE named deterministic plan tooth from services/plan_gate_checks,
    read-only -- exactly the /steps/green-gate-check contract, for the other
    gate. Exists so plan-gate-check.json can chain a real node per check and
    the Workflows page shows what plan_gate actually asks, instead of one
    opaque rubric callback.

    Always HTTP 200 regardless of ok=true/false (same as
    /steps/green-gate-check): a refused tooth is a REPORTED fact, not a
    callback FAILURE, so the chain reaches every later check and Complete.

    Never decides plan_gate. The seats that act on the same verdict are
    api/conductor_flow.py's entry-time autoclear and gate_adjudicator's
    re-sweep; a human's Approve click is never blocked by it."""
    with _tracer.start_as_current_span("workflow.step.plan_gate_check") as span:
        span.set_attribute("workflow.project", project)
        span.set_attribute("workflow.task.id", body.task_id)
        span.set_attribute("workflow.check", body.check)

        from prism_service.services import plan_gate_checks as pgc
        ctx = get_project(project)
        task = ctx.task_svc.get(body.task_id)
        if task is None:
            return GateCheckStatus(
                id=body.check, label=body.check, ok=False,
                reason=f"no such task: {body.task_id}")
        return GateCheckStatus(**pgc.run_check(body.check, task, project))


class GateAdjudicationRequest(BaseModel):
    task_id: str = Field(min_length=1)
    stage: str = Field(min_length=1)


@router.post("/steps/gate-adjudication")
def workflow_step_gate_adjudication(
    body: GateAdjudicationRequest, project: str = Query(...),
) -> GateCheckStatus:
    """ONE named state of the gate-adjudication flow.

    A gate is a STATE the conductor hands a task to, and it is worked, not
    merely computed (owner 2026-08-29). Its work is layered: "it should be
    inferred AND rubric ... make it the MOST deterministic it can be by
    codifying things as much as it can, and leaving room for inference to
    deal with unknowns." Each layer is its own step here so the Workflows
    page shows what the gate actually does, rather than one opaque verdict.

      stage=codified  -- every mechanically checkable property. ok=false
                         carries the refusal, which is already a decision.
      stage=infer     -- the residue a rubric cannot express, judged by a
                         real read-only `claude -p` seat. Reached ONLY when
                         `codified` had nothing to say: inference never
                         talks a codified refusal away.

    Always HTTP 200, like /steps/green-gate-check: a refusal is a REPORTED
    fact, not a callback failure, so the chain reaches every later state.
    """
    with _tracer.start_as_current_span("workflow.step.gate_adjudication") as span:
        span.set_attribute("workflow.project", project)
        span.set_attribute("workflow.task.id", body.task_id)
        span.set_attribute("workflow.stage", body.stage)

        ctx = get_project(project)
        task = ctx.task_svc.get(body.task_id)
        if task is None:
            return GateCheckStatus(id=body.stage, label=body.stage, ok=False,
                                   reason=f"no such task: {body.task_id}")
        step_id = str(getattr(task, "workflow_step", "") or "")

        from prism_service.services import gate_adjudicator, gate_agent
        if body.stage == "codified":
            reason = gate_adjudicator._pending_decline_reason(
                ctx.conductor_svc, task, step_id, project)
            return GateCheckStatus(
                id="codified", label="Everything the rubric can decide",
                ok=not str(reason or "").strip(), reason=str(reason or ""))

        if body.stage == "infer":
            reason = gate_adjudicator._pending_decline_reason(
                ctx.conductor_svc, task, step_id, project)
            if str(reason or "").strip():
                return GateCheckStatus(
                    id="infer", label="Judgement on what the rubric cannot say",
                    ok=True,
                    reason="not reached: the codified layer already decided")
            if not gate_agent.is_enabled():
                return GateCheckStatus(
                    id="infer", label="Judgement on what the rubric cannot say",
                    ok=True, reason="inference seat is off "
                                    "(PRISM_GATE_AGENT_ENABLED)")
            decided = gate_agent.adjudicate(project, body.task_id, step_id)
            return GateCheckStatus(
                id="infer", label="Judgement on what the rubric cannot say",
                ok=True,
                reason=("the inference seat decided this gate" if decided
                        else "the inference seat reached no decision; "
                             "the gate stays as it was"))

        return GateCheckStatus(id=body.stage, label=body.stage, ok=False,
                               reason=f"unknown stage: {body.stage}")


# The behaviour a conductor STATE calls, inverted. Derived from the same
# linked_workflow_id chain get_workflows builds, so the two cannot drift:
# if a state's behaviour changes there, this map must change with it.
_BEHAVIOUR_FOR_STEP: dict[str, str] = {
    "verify_green_state": "validation",
    "story_gate": "story-gate-check",
    "plan_gate": "plan-gate-check",
    "draft_story": "draft-story-loop",
    "review_previous_notes": "review-previous-notes-loop",
    "verify_plan": "verify-plan-loop",
    "write_failing_tests": "write-failing-tests-loop",
    "implement_tasks": "implement-tasks-loop",
    "red_gate": "red-gate-status",
    "green_gate": "green-gate-status",
}
_STEP_FOR_BEHAVIOUR: dict[str, str] = {
    v: k for k, v in _BEHAVIOUR_FOR_STEP.items()}


class WorkflowInstance(BaseModel):
    id: str
    at: str
    actor: str
    kind: str
    outcome: str
    summary: str
    # The version of the flow definition this run EXECUTED against, or None
    # when the run predates version stamping. None means UNKNOWN and is
    # never silently read as "the current one": a run of plan-gate-check v1
    # (one opaque rubric callback) and a run of v3 (rubric + three teeth +
    # infer) are not the same execution, and listing them together would
    # misrepresent what actually ran (owner 2026-08-29: "it is INSTANCE ran
    # per THIS version of the Bot/Agentic flow").
    flow_version: Optional[int] = None


_FLOW_VERSION_RE = re.compile(r"flow_version=(\d+)")


def _behaviour_version(project: str, workflow_id: str) -> Optional[int]:
    """The version of the behaviour definition as it stands NOW."""
    from prism_service.services.claude_transcripts import _project_source_path
    try:
        root = Path(_project_source_path(project))
        doc = json.loads(
            (root / ".prism" / "behaviors" / "conductor"
             / f"{workflow_id}.json").read_text(encoding="utf-8"))
        return int(doc.get("version"))
    except Exception:
        return None


@router.get("/{workflow_id}/instances")
def workflow_instances(
    workflow_id: str,
    project: str = Query(...),
    task_id: str = Query(""),
    version: Optional[int] = Query(None),
) -> dict:
    """The EXECUTION INSTANCES of one layer.

    Owner 2026-08-29: "the rail ... should render the execution instances of
    the bot's layer we are looking at, each time we click into the next
    layer, then it should move to that historical view for that instance."

    A declarative FSM behaviour has no WorkflowCore run behind it --
    /runs/history 404s for anything but `validation`, which is why every
    bot-family entry showed an empty rail forever. But the executions DID
    happen and ARE recorded: a gate's runs are its `gate_decide` rows and a
    step's runs are the `advance_task` rows that left it. Measured on this
    project: 2,012 gate decisions and 3,246 advances on file.

    Scoped by `task_id` when given, because drilling in from one task's
    instance should show THAT task's history at the deeper layer, not every
    task's.
    """
    ctx = get_project(project)
    step_id = _STEP_FOR_BEHAVIOUR.get(workflow_id, "")
    if not step_id:
        return {"workflow_id": workflow_id, "step_id": "", "instances": []}

    task_svc = getattr(ctx, "task_svc", None)
    rows = []
    if task_svc is not None and hasattr(task_svc, "_db"):
        sql = ("SELECT id, task_id, actor, action, details, timestamp "
               "FROM task_history WHERE action IN ('gate_decide','advance_task')")
        args: list = []
        if task_id:
            sql += " AND task_id = ?"
            args.append(task_id)
        sql += " ORDER BY id DESC LIMIT 400"
        try:
            rows = task_svc._db.execute(sql, args).fetchall()
        except Exception:
            rows = []

    is_gate = step_id.endswith("_gate")
    out: list[WorkflowInstance] = []
    for r in rows:
        details = str(r["details"] or "")
        if is_gate:
            if r["action"] != "gate_decide" or f"gate={step_id};" not in details:
                continue
            outcome = ("approved" if "action=approve" in details
                       else "rejected" if "action=reject" in details else "decided")
        else:
            if r["action"] != "advance_task" or f"from={step_id};" not in details:
                continue
            outcome = "advanced"
        vm = _FLOW_VERSION_RE.search(details)
        ran_version = int(vm.group(1)) if vm else None
        # An explicit version filter matches only runs that RECORDED that
        # version. An unstamped run is unknown, not a match -- guessing
        # would put a v1 execution under a v3 heading.
        if version is not None and ran_version != version:
            continue
        out.append(WorkflowInstance(
            id=str(r["id"]), at=str(r["timestamp"] or ""),
            actor=str(r["actor"] or ""), kind=str(r["action"]),
            outcome=outcome,
            summary=details[:180],
            flow_version=ran_version,
        ))
    current = _behaviour_version(project, workflow_id)
    return {"workflow_id": workflow_id, "step_id": step_id,
            "task_id": task_id,
            "current_version": current,
            "unstamped": sum(1 for i in out if i.flow_version is None),
            "instances": [i.model_dump() for i in out]}


class NodeStatus(BaseModel):
    id: str
    state: str          # passed | refused | not_reached | unknown
    reason: str = ""


@router.get("/{workflow_id}/node-status")
def workflow_node_status(
    workflow_id: str,
    project: str = Query(...),
    task_id: str = Query(...),
) -> dict:
    """The REAL per-node state of one layer, for one task.

    Owner 2026-08-29, looking at a drilled-in gate layer: "I do not see any
    steps in what you are showing me with their progress bar like from
    conductor ... there is no indication anywhere what the hell is going
    on." Correct: WorkflowsPage only ever derives node state from
    `workflowRun.runtime`, and no WorkflowCore run backs a declarative FSM
    behaviour, so `activeProgress` is null on every drilled layer and the
    canvas draws a dead diagram.

    The verdicts were never missing -- only unexposed per node. Each node of
    a gate behaviour IS a check with an answer, so this reports it:

      passed      the check ran and is satisfied
      refused     it ran and says no, with the reason it gave
      not_reached the codified layer already decided, so inference never ran
      unknown     this layer has no per-node check to report (said plainly,
                  never dressed up as passed)
    """
    ctx = get_project(project)
    task = ctx.task_svc.get(task_id)
    if task is None:
        return {"workflow_id": workflow_id, "task_id": task_id, "nodes": []}

    nodes: list[NodeStatus] = []
    if workflow_id == "plan-gate-check":
        from prism_service.services import plan_gate_checks as pgc
        # The `rubric` node comes FIRST in the behaviour and is not one of
        # the deterministic teeth, so run_all does not cover it. Reporting
        # only the teeth left one node of five permanently blank, which is
        # the same "no indication what is going on" this endpoint exists to
        # end.
        try:
            # _score_rubric's plan_coverage branch reads fields["plan_doc"]
            # -- NOT plan_md/story_md. Passing the wrong key scored an EMPTY
            # document and reported a confident "story carries no AC-<n>
            # ids" on a plan that carries AC-1..AC-5, i.e. a false red on a
            # node this endpoint exists to tell the truth about.
            scored = _score_rubric(
                "plan_coverage",
                {"plan_doc": str(getattr(task, "plan_doc", "") or ""),
                 "plan_diagram": str(getattr(task, "plan_diagram", "") or "")},
                project)
            nodes.append(NodeStatus(
                id="rubric",
                state="passed" if scored.get("ok") else "refused",
                reason=str(scored.get("reason") or "")))
        except Exception as exc:
            nodes.append(NodeStatus(
                id="rubric", state="unknown",
                reason=f"could not score the plan rubric: {exc}"))
        # measure=False: THIS IS A READ. The measuring tier of
        # already_green_ac does `git worktree add --detach`, runs pytest in
        # the scratch tree, then `git worktree remove` (plan_gate_checks.py
        # ~326-342). Doing that inside a request handler wedged the whole
        # daemon on 2026-08-29: the worktree lock contended with agents
        # working the same shared repo, the handler blocked on a subprocess
        # that never returned, the thread pool drained, and the API stopped
        # accepting with 65 connections backlogged and an unreaped git
        # child. A status endpoint must be cheap and must never take a repo
        # lock; the cached/declaration tier still answers every node.
        # NEVER COMMITTED until 2026-08-30: this guard lived only as an
        # uncommitted edit in the shared checkout, so any reset or fresh
        # clone reopened the wedge. `git log -S measure=False --all` found
        # nothing before this commit.
        for entry in pgc.run_all(task, project, measure=False):
            nodes.append(NodeStatus(
                id=entry["id"],
                state="passed" if entry["ok"] else "refused",
                reason=str(entry.get("reason") or "")))
    elif workflow_id == "green-gate-status":
        # The registry IS the node list for this layer -- read it rather
        # than keeping a second copy that can drift from the behaviour JSON.
        for check in _green_gate_check_registry(ctx, task, project):
            got = _run_one_check(ctx, task, project, check)
            nodes.append(NodeStatus(
                id=check, state="passed" if got.ok else "refused",
                reason=got.reason))

    # `infer` is the last state of every gate behaviour: it runs only when
    # the codified layer had nothing left to say.
    behaviour_gates = {"story-gate-check", "plan-gate-check",
                       "red-gate-status", "green-gate-status"}
    if workflow_id in behaviour_gates:
        from prism_service.services import gate_adjudicator, gate_agent
        step_id = _STEP_FOR_BEHAVIOUR.get(workflow_id, "")
        decline = ""
        try:
            decline = gate_adjudicator._pending_decline_reason(
                ctx.conductor_svc, task, step_id, project)
        except Exception:
            decline = ""
        if str(decline or "").strip():
            nodes.append(NodeStatus(
                id="infer", state="not_reached",
                reason="the codified layer already decided: "
                       + str(decline)[:160]))
        elif not gate_agent.is_enabled():
            nodes.append(NodeStatus(
                id="infer", state="unknown",
                reason="inference seat is off (PRISM_GATE_AGENT_ENABLED)"))
        else:
            nodes.append(NodeStatus(id="infer", state="passed", reason=""))

    return {"workflow_id": workflow_id, "task_id": task_id,
            "nodes": [n.model_dump() for n in nodes]}


# ---------------------------------------------------------------------------
# The pipeline's TERMINAL node: reap (task f97c196d)
#
# `land` merges the branch; `reap` removes what the drive left behind. It is
# CODIFIED -- deterministic Python plus git, zero model calls, exactly like
# /steps/premise-gather and /steps/green-gate-check. Only /steps/reason-loop
# and /steps/premise-judge are agentic routes on this router.
#
# The behaviour lives in services/task_reaper.py (all five safety rules and
# the line that keeps each one are in that module's docstring); this route
# only resolves the task's real status and hands back the typed verdict.
# ---------------------------------------------------------------------------
class ReapRequest(BaseModel):
    task_id: str = Field(min_length=1)
    mode: str = Field(default="reap", pattern="^(reap|survey)$")


class WriteTestFileRequest(BaseModel):
    """The drafted test, and where it goes in the task's own worktree."""

    task_id: str = Field(min_length=1)
    test_file_path: str = Field(min_length=1)
    test_code: str = Field(min_length=1)


class RunPinnedSuiteRequest(BaseModel):
    """Run the task's pinned suite. `paths` empty means read task.verify."""

    task_id: str = Field(min_length=1)
    paths: list[str] = Field(default_factory=list)
    timeout_s: float = Field(default=600.0, gt=0)


class CommitTestsOnlyRequest(BaseModel):
    """Commit ONLY test files, carrying the task trailer."""

    task_id: str = Field(min_length=1)
    message: str = Field(default="")


class BrainHealthRequest(BaseModel):
    task_id: str = Field(min_length=1)


@router.post("/steps/brain-health")
def workflow_step_brain_health(
    body: BrainHealthRequest, project: str = Query(...),
) -> dict:
    """Index what a finished play wrote, then check knowledge coverage.

    CODIFIED (task 013c5197). Deterministic Python, no model call -- the
    knowledge-side counterpart to /steps/reap, which cleans the repository
    a finished play leaves behind while this cleans the knowledge.

    Always HTTP 200, the same contract /steps/reap and /steps/green-gate-
    check use: a coverage FALL is a reported fact, never a callback
    failure, so the behaviour reaches its next step and the reason reaches
    a person. `ship_worker._brain_health_after_land` calls the same
    `index_finished_play` on the same trigger, so this route and the seat
    can never disagree about what the node does -- one implementation, two
    entry points.
    """
    from prism_service.services import brain_health

    try:
        verdict = brain_health.index_finished_play(
            body.task_id, project=project)
        return {"kind": "conductor.brain_health", "ok": True, **verdict}
    except brain_health.CoverageBelowFloor as below:
        # The whole point of the node: a fall is REPORTED, never swallowed.
        return {"kind": "conductor.brain_health", "ok": False,
                "reason": str(below)}
    except Exception as exc:
        return {"kind": "conductor.brain_health", "ok": False,
                "reason": f"brain-health could not run: {exc}"}


class RefreshMapsRequest(BaseModel):
    task_id: str = Field(min_length=1)


@router.post("/steps/refresh-maps")
def workflow_step_refresh_maps(
    body: RefreshMapsRequest, project: str = Query(...),
) -> dict:
    """Rebuild the Understand maps once a play has landed.

    CODIFIED. Deterministic Python, no model call -- the READING-side
    counterpart to /steps/brain-health: that one re-indexes what the play
    wrote, this one redraws the maps a person opens to see it.

    Always HTTP 200, the same contract /steps/brain-health and /steps/reap
    use: a map that did not rebuild is a reported fact, never a callback
    failure, so the behaviour reaches its next step and the reason reaches a
    person. `ship_worker._refresh_maps_after_land` calls the same
    `map_refresh.refresh_maps` on the same trigger, so this route and the
    seat can never disagree about what the node does -- one implementation,
    two entry points.
    """
    from prism_service.services import map_refresh

    try:
        result = map_refresh.refresh_maps(project, task_id=body.task_id)
        return {"kind": "conductor.refresh_maps",
                "summary": map_refresh.summarise(result), **result}
    except Exception as exc:  # noqa: BLE001 - a map never fails the pipeline
        return {"kind": "conductor.refresh_maps", "ok": False,
                "reason": f"refresh-maps could not run: {exc}"}


@router.post("/steps/reap")
def workflow_step_reap(
    body: ReapRequest, project: str = Query(...),
    mode: str = Query(""),
) -> dict:
    """Reap one finished task's git worktree and `prism/ws/<id>` branch.

    Always HTTP 200, same contract as /steps/green-gate-check: a refusal is
    a REPORTED fact ("uncommitted changes", "3 commits exist nowhere else"),
    never a callback failure -- the behaviour must reach its next step and
    the reason must reach a person.

    `mode=survey` computes the identical verdict and deletes nothing, so the
    reap.json behaviour records the decision BEFORE anything is removed.
    """
    from prism_service.services import drive_heartbeat, task_reaper

    with _tracer.start_as_current_span("workflow.step.reap") as span:
        span.set_attribute("workflow.project", project)
        span.set_attribute("workflow.task.id", body.task_id)
        chosen = str(mode or body.mode or "reap")
        span.set_attribute("workflow.mode", chosen)

        ctx = get_project(project)
        task = ctx.task_svc.get(body.task_id)
        if task is None:
            return {"kind": "conductor.reap", "node_id": "reap",
                    "task_id": body.task_id, "outcome": "refused",
                    "reaped": False, "would_reap": False,
                    "reason": f"no such task: {body.task_id}"}

        # Rule 4, third net: a drive that beat within the last 15 minutes is
        # standing in that worktree right now. Unreadable heartbeat state is
        # NOT read as "nobody is there" -- reap_task fails closed on it.
        def _is_live(task_id: str) -> bool:
            age = drive_heartbeat.heartbeat_age_s(
                str(ctx._data_dir / "scores.db"), task_id)
            return age is not None and age < 900.0

        return task_reaper.reap_task(
            body.task_id, status=str(getattr(task, "status", "") or ""),
            mode=chosen, is_live=_is_live)


# ----------------------------------------------------------------------
# THE BUILD NODES (task ab9166d5). write_failing_tests used to declare one
# node -- a reason-loop that DRAFTS a test and is told not to write it --
# so the step fell back to a general claude -p carrying BUILD_TOOLS. That
# envelope measured 163,315 tokens against the engine's 131,072 window, so
# the step 400'd before inference and 27 dispatches recorded no model run
# at all. These three nodes do the work the draft cannot: write, run, and
# commit. Same contract as /steps/reap -- always HTTP 200, a refusal is a
# REPORTED fact, never an exception, so the flow reaches its next node and
# the reason reaches a person.
# ----------------------------------------------------------------------

def _task_worktree(task_id: str) -> tuple[object, str]:
    """(Path, "") for a resolvable worktree, else (None, reason)."""
    from pathlib import Path

    from prism_service.services import task_workspace

    ws = task_workspace.workspace_path(task_id) or ""
    if not ws:
        return None, f"task {task_id[:8]} has no worktree on disk"
    p = Path(ws)
    if not p.is_dir():
        return None, f"worktree {ws} is not a directory"
    return p, ""


def _inside(root, candidate: str) -> bool:
    """True when `candidate` resolves inside `root`. Blocks ../ escapes."""
    from pathlib import Path

    try:
        (root / candidate).resolve().relative_to(Path(root).resolve())
        return True
    except (ValueError, OSError):
        return False


def _record_node_run(project: str, task_id: str, route: str, ok: bool,
                     summary: str) -> None:
    """Self-record THIS route's own run (task 1cdf1d70).

    Before this, an agent_runs row for a codified node only existed when
    task_runner's declared-chain dispatch called the route in-process --
    a caller that hit the SAME route directly (a curl, a test, the
    conductor-adjudicator probing red) left no row at all, so the
    Workflows canvas read the node as never having run however many times
    it had actually fired. Measured live 2026-09-10 on task ab9166d5:
    write-test-file wrote 9701 bytes, run-pinned-suite returned rc=1,
    commit-tests-only made a real commit the adjudicator accepted as the
    red anchor -- and run_count stayed 0 for all three the whole time.

    Recording HERE, at the one seam every caller goes through (in-process
    dispatch and a raw HTTP call both land in this function body), is what
    makes the count mean "this route ran", not "task_runner drove it".
    task_runner's own dispatch loop no longer double-records these routes
    (see task_runner._SELF_RECORDING_ROUTES) now that this is the single
    source of truth.

    Never raises: a broken recorder must never break the node's real work,
    the same rule _record_codified_run already keeps.
    """
    if not task_id:
        return   # nothing to key a node card's history on
    try:
        import uuid as _uuid

        from prism_service.services import task_runner as _task_runner

        _task_runner._record_codified_run(
            project, task_id, route, str(_uuid.uuid4()), ok, summary)
    except Exception:
        pass


@router.post("/steps/write-test-file")
def workflow_step_write_test_file(
    body: WriteTestFileRequest, project: str = Query(...),
) -> dict:
    """Write the drafted test into the task's own worktree.

    REFUSES rather than raises on: no worktree, a path that escapes the
    worktree, and a path that is not a test file. The last one is the
    tests-only invariant's first gate -- the red seat anchors on a commit
    that carries tests and nothing else.
    """
    out = {"kind": "conductor.write_test_file", "node_id": "write-test-file",
           "task_id": body.task_id, "outcome": "refused", "written": False,
           "path": body.test_file_path, "bytes": 0, "reason": ""}
    try:
        with _tracer.start_as_current_span("workflow.step.write_test_file") as sp:
            sp.set_attribute("workflow.project", project)
            sp.set_attribute("workflow.task.id", body.task_id)
            root, why = _task_worktree(body.task_id)
            if root is None:
                out["reason"] = why
                return out
            rel = body.test_file_path.lstrip("/")
            if not _inside(root, rel):
                out["reason"] = f"path escapes the worktree: {rel}"
                return out
            name = rel.rsplit("/", 1)[-1]
            if not (name.startswith("test_") and name.endswith(".py")):
                out["reason"] = (
                    f"not a test file: {name} must match test_*.py, because "
                    f"the red anchor carries tests and nothing else")
                return out
            target = root / rel
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(body.test_code, encoding="utf-8")
            except OSError as exc:
                out["reason"] = f"write failed: {type(exc).__name__}: {exc}"
                return out
            out.update(outcome="ok", written=True,
                       bytes=len(body.test_code.encode("utf-8")))
            return out
    finally:
        # ANY caller of this route -- task_runner's in-process dispatch or
        # a direct HTTP call -- leaves the SAME agent_runs row, so a
        # node's history means "this route ran" (task 1cdf1d70).
        _record_node_run(
            project, out["task_id"], "write-test-file",
            out["outcome"] == "ok",
            out["reason"] or f"wrote {out['bytes']} byte(s) to {out['path']}")


@router.post("/steps/run-pinned-suite")
def workflow_step_run_pinned_suite(
    body: RunPinnedSuiteRequest, project: str = Query(...),
) -> dict:
    """Run the task's pinned suite in its worktree and REPORT the rc.

    The rc IS the product. oracle_spec's red check wants rc==1 and refuses
    rc 0, 2 and 4 by name, so this node never interprets -- it reports the
    integer and the tail, and the gate decides.
    """
    import subprocess

    out = {"kind": "conductor.run_pinned_suite", "node_id": "run-pinned-suite",
           "task_id": body.task_id, "outcome": "refused", "rc": None,
           "paths": [], "tail": "", "reason": ""}
    try:
        with _tracer.start_as_current_span("workflow.step.run_pinned_suite") as sp:
            sp.set_attribute("workflow.project", project)
            sp.set_attribute("workflow.task.id", body.task_id)
            root, why = _task_worktree(body.task_id)
            if root is None:
                out["reason"] = why
                return out
            paths = list(body.paths)
            if not paths:
                task = get_project(project).task_svc.get(body.task_id)
                paths = list(getattr(task, "verify", None) or [])
            paths = [p for p in paths if _inside(root, p.lstrip("/"))]
            if not paths:
                out["reason"] = (
                    "no pinned suite: task.verify is empty or names a path "
                    "outside the worktree")
                return out
            out["paths"] = paths
            cmd = ["python3", "-m", "pytest", *paths, "-q",
                   "-o", "faulthandler_timeout=120"]
            try:
                proc = subprocess.run(cmd, cwd=str(root), capture_output=True,
                                      text=True, timeout=body.timeout_s)
            except subprocess.TimeoutExpired:
                out["reason"] = f"suite exceeded {body.timeout_s}s"
                return out
            except OSError as exc:
                out["reason"] = f"could not run pytest: {exc}"
                return out
            combined = (proc.stdout or "") + (proc.stderr or "")
            out.update(outcome="ok", rc=proc.returncode,
                       tail="\n".join(combined.splitlines()[-30:]))
            return out
    finally:
        _record_node_run(
            project, out["task_id"], "run-pinned-suite",
            out["outcome"] == "ok",
            out["reason"] or f"pytest rc={out['rc']} over {out['paths']}")


@router.post("/steps/commit-tests-only")
def workflow_step_commit_tests_only(
    body: CommitTestsOnlyRequest, project: str = Query(...),
) -> dict:
    """Commit ONLY test files, carrying the task trailer.

    THE TESTS-ONLY RULE IS ENFORCED HERE, not asked for in a prompt: any
    staged path that is not a test file is a refusal, because a bundled
    tests+impl commit makes red undemonstrable and strands red_gate with a
    person. Nothing is committed when the refusal fires.
    """
    import subprocess

    out = {"kind": "conductor.commit_tests_only",
           "node_id": "commit-tests-only", "task_id": body.task_id,
           "outcome": "refused", "committed": False, "sha": "",
           "files": [], "reason": ""}

    def _git(*args: str):
        return subprocess.run(["git", *args], cwd=str(root),
                              capture_output=True, text=True, timeout=120)

    try:
        with _tracer.start_as_current_span("workflow.step.commit_tests_only") as s:
            s.set_attribute("workflow.project", project)
            s.set_attribute("workflow.task.id", body.task_id)
            root, why = _task_worktree(body.task_id)
            if root is None:
                out["reason"] = why
                return out
            try:
                changed = _git("status", "--porcelain")
            except (OSError, subprocess.TimeoutExpired) as exc:
                out["reason"] = f"git status failed: {exc}"
                return out
            files = [ln[3:].strip() for ln in
                     (changed.stdout or "").splitlines() if ln[3:].strip()]
            if not files:
                out["reason"] = "nothing to commit: the worktree is clean"
                return out
            stray = [f for f in files
                     if not f.rsplit("/", 1)[-1].startswith("test_")]
            if stray:
                out["files"] = files
                out["reason"] = (
                    f"not tests-only: {len(stray)} non-test path(s) are dirty "
                    f"({', '.join(stray[:3])}). The red anchor must carry tests "
                    f"and nothing else, so nothing was committed.")
                return out
            msg = body.message or f"test: pin the failing case [task:{body.task_id[:8]}]"
            if f"[task:{body.task_id[:8]}]" not in msg:
                msg = f"{msg} [task:{body.task_id[:8]}]"
            add = _git("add", *files)
            if add.returncode != 0:
                out["reason"] = f"git add failed: {(add.stderr or '').strip()[:200]}"
                return out
            made = _git("commit", "-m", msg)
            if made.returncode != 0:
                out["reason"] = f"git commit failed: {(made.stderr or '').strip()[:200]}"
                return out
            sha = _git("rev-parse", "HEAD")
            out.update(outcome="ok", committed=True, files=files,
                       sha=(sha.stdout or "").strip()[:40])
            return out
    finally:
        _record_node_run(
            project, out["task_id"], "commit-tests-only",
            out["outcome"] == "ok",
            out["reason"] or f"committed {out['sha'][:12]}: {', '.join(out['files'])}")
