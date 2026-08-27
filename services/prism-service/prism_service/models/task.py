"""Task data models for PRISM task management."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

# Channel vocabulary (task b480eb15, epic d6966b43) — WHERE a task came from.
# Defined ONCE here; every entry point (REST, MCP, mirror import) validates
# against this tuple. Blank is allowed (legacy rows predate the field).
CHANNELS: tuple[str, ...] = (
    "ui", "mcp", "github", "jira", "slack", "outlook", "daemon", "ontology",
)


def validate_channel(channel: str) -> str:
    """Return the normalized channel or raise ValueError for an unknown one.
    Blank passes through unchanged (legacy / not-yet-attributed rows)."""
    value = (channel or "").strip().lower()
    if value and value not in CHANNELS:
        raise ValueError(
            f"unknown channel {channel!r}; expected one of {', '.join(CHANNELS)}"
        )
    return value


# Workflow provenance (task af396b2c): WHICH PRISM workflow drives a task.
# api/workflows.py's GET /api/workflows builds a catalog of
# [conductor, validation, *behaviors] (~624-710) -- a task never names a
# catalog entry directly (conductor_service.py, a POLICY file, stays
# untouched by this feature); it names a stable, worker-facing value that
# MAPS to one, defined ONCE here so every entry point (REST, MCP) resolves
# it the same way. "implement" is the only value with a real driver today
# (the 10-step SDLC conductor loop) and maps to the catalog's "conductor"
# entry.
WORKFLOW_ALIASES: dict[str, str] = {"implement": "conductor"}
DEFAULT_WORKFLOW = "implement"


def validate_workflow(workflow: str) -> str:
    """Return the normalized workflow name or raise ValueError for a name
    outside WORKFLOW_ALIASES. Blank passes through unchanged -- callers
    that want the default resolve it themselves (TaskService.create/
    update), and a legacy row's blank column reads as DEFAULT_WORKFLOW via
    normalize_workflow at hydration time, never here."""
    value = (workflow or "").strip().lower()
    if value and value not in known_workflows():
        raise ValueError(
            f"unknown workflow {workflow!r}; expected one of "
            f"{', '.join(sorted(known_workflows()))}"
        )
    return value


def known_workflows() -> set[str]:
    """Every name a task may be bound to: the alias names (implement ->
    conductor) plus every registered workflow in models.workflow.WORKFLOWS
    (triage, ...). Lazy import: models.workflow.steps_for imports this
    module, so a top-level import here would be circular. Task 6f22d0ad
    found the validator only knew the aliases, so a triage task could be
    walked by the conductor but never CREATED through REST/MCP."""
    names = set(WORKFLOW_ALIASES)
    try:
        from prism_service.models.workflow import WORKFLOWS
        names |= set(WORKFLOWS)
    except Exception:  # pragma: no cover - registry import must never break create
        pass
    return names


def normalize_workflow(workflow: str) -> str:
    """A persisted blank workflow column (a legacy row from before this
    field existed, or a row written by a path that never set it) reads as
    the default driver so nothing breaks. Never raises."""
    return (workflow or "").strip().lower() or DEFAULT_WORKFLOW


# Status vocabulary (task f5352fa1, epic 3a652b3b: "the ontology is
# respected throughout the system") -- the lifecycle values a task row may
# hold. Defined ONCE here; validated on WRITE only (REST create/update, MCP
# task_create/task_update) -- a legacy row is never re-validated on read,
# same posture as validate_channel/validate_workflow above. "deleted" and
# "archived" are real statuses conductor_service.py itself transitions rows
# through (soft-delete / archival), confirmed live via the Terms tab's
# held_back list (30 deleted + 2 archived rows) -- declaring only the
# "visible" five would refuse those genuine transitions.
STATUSES: tuple[str, ...] = (
    "pending", "in_progress", "blocked", "done", "cancelled",
    "deleted", "archived",
)


def validate_status(status: str) -> str:
    """Return the normalized status or raise ValueError for an unknown one.
    Blank passes through unchanged (a write that doesn't touch status)."""
    value = (status or "").strip().lower()
    if value and value not in STATUSES:
        raise ValueError(
            f"unknown status {status!r}; expected one of {', '.join(STATUSES)}"
        )
    return value


# Proof-type vocabulary (task f5352fa1) -- the kind of completion evidence a
# task's green_gate demands (see Task.proof_type below). Kept in sync with
# the values mcp/tools.py's task_create/task_update schemas already
# document (test|demo|artifact|metric|review|source_backed_answer|decision),
# plus "pr" (task afb47c33): the evidence is a merged pull request.
PROOF_TYPES: tuple[str, ...] = (
    "test", "demo", "artifact", "metric", "review",
    "source_backed_answer", "decision", "pr",
)


def validate_proof_type(proof_type: str) -> str:
    """Return the normalized proof_type or raise ValueError for an unknown
    one. Blank passes through unchanged (proof_type is optional)."""
    value = (proof_type or "").strip().lower()
    if value and value not in PROOF_TYPES:
        raise ValueError(
            f"unknown proof_type {proof_type!r}; expected one of "
            f"{', '.join(PROOF_TYPES)}"
        )
    return value


@dataclass
class Task:
    """A work item tracked through the PRISM workflow."""

    id: str = ""
    title: str = ""
    description: str = ""
    status: str = "pending"  # pending | in_progress | done | blocked
    priority: int = 0
    story_file: str = ""
    assigned_agent: str = ""
    created_at: str = ""
    updated_at: str = ""
    completed_at: str = ""
    blocked_reason: str = ""
    dependencies: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    # Conductor v2 — per-task workflow state machine. workflow_step holds
    # the id of the current entry in models.workflow.WORKFLOW_STEPS (empty
    # string means the task has not entered the workflow yet). gate_state
    # is meaningful only when workflow_step points at a gate-type entry:
    #   none    — not at a gate (or pre-decision)
    #   pending — at a gate awaiting gate_decide
    #   passed  — gate approved (transient; auto-advance clears this)
    #   failed  — gate rejected; gate_reason carries the explanation
    workflow_step: str = ""
    gate_state: str = "none"  # none | pending | passed | failed
    gate_reason: str = ""
    # Structural hierarchy (parent → children). Empty string = a root/
    # top-level task. Distinct from `dependencies` (scheduling/blocking):
    # parent_id answers "what epic do I belong to?", dependencies answers
    # "what must finish before I can start?". The /tasks board shows only
    # roots; a parent's children are reached by clicking into its detail.
    parent_id: str = ""
    # Oracle (ported from goalbuddy state.yaml goal.oracle/intake) — an
    # upfront, observable completion signal defined BEFORE work starts and
    # checked at green_gate, closing the "tests-pass ≠ feature-works" gap.
    #   oracle           — the observable signal that proves the user outcome
    #   proof_type       — test|demo|artifact|metric|review|
    #                      source_backed_answer|decision
    #   completion_proof — receipt-backed evidence captured when done
    oracle: str = ""
    proof_type: str = ""
    completion_proof: str = ""
    # likely_misfire (goalbuddy GAP-2) — "how could this pass but be WRONG?".
    # The cheapest high-leverage defense against false-greens: recorded
    # upfront beside the oracle and AUDITED at green_gate (advisory note
    # when set but completion_proof doesn't visibly address it). Defaults
    # to '' (additive/non-breaking); round-trips via the same DB path.
    likely_misfire: str = ""
    # full_outcome_complete (goalbuddy GAP-4) — a green SLICE is not proof the
    # owner's full outcome is met. The conductor sets this True only when the
    # slice is green AND there are no incomplete child tasks AND completion_proof
    # is strong; otherwise it stays False (default) and an advisory
    # "slice-complete, not owner-outcome-complete" note rides the green_gate.
    # Additive/non-breaking; round-trips via the same DB path, stored as 0/1.
    full_outcome_complete: bool = False
    # Worker contract (ported from goalbuddy state.yaml task T003) — bounds an
    # implementation slice so it stays explicit, verified, and reversible:
    #   allowed_files — the file allowlist the dev step may touch
    #   verify        — commands that prove the slice (e.g. the test cmd)
    #   stop_if       — conditions that HALT the slice (out-of-scope file,
    #                   ambiguous behavior, verification fails twice)
    # Parallel workers are safe only with provably-disjoint allowed_files.
    allowed_files: list[str] = field(default_factory=list)
    verify: list[str] = field(default_factory=list)
    stop_if: list[str] = field(default_factory=list)
    # Rich plan rendering — a synthesized plan stored ON the task so the
    # SPA can render it as a document instead of a raw <pre> blob:
    #   plan_doc     — markdown of the proposed change (rendered below)
    #   plan_diagram — Mermaid source for a sequence/UML diagram (on top)
    # Both default to '' (additive, non-breaking); absent => the detail
    # page falls back to the existing description view.
    plan_doc: str = ""
    plan_diagram: str = ""
    # premise_notes (task 3928b7ac, issue #222 continued) — review_previous_
    # notes' OWN dedicated field, never task.completion_proof. completion_
    # proof is SHARED with the green_gate oracle tooth (score_green_outcome);
    # several test fixtures stage green-proof-shaped content there on a
    # fresh task before walking it through advance_task, so gating
    # premise_grounded on that same field made an unrelated value look like
    # (or fail to look like) a premise report. This field decouples the two
    # so premise_grounded can be unconditional without that collision.
    premise_notes: str = ""
    # Channel provenance (task b480eb15): the entry point that created the
    # task (one of CHANNELS above, '' for legacy rows) and an opaque origin
    # reference — a session id, an issue URL, a message permalink.
    channel: str = ""
    channel_ref: str = ""
    # Workflow provenance (task af396b2c): which PRISM workflow drives this
    # task (see WORKFLOW_ALIASES above). Defaults to "implement" for a
    # freshly-constructed Task; a persisted legacy row with a blank column
    # normalizes to the same default at hydration time
    # (task_service._row_to_task), never stored as blank going forward.
    workflow: str = DEFAULT_WORKFLOW

    def __post_init__(self) -> None:
        if not self.id:
            self.id = str(uuid4())
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()


@dataclass
class TaskHistory:
    """An audit record for a task state change."""

    id: int = 0
    task_id: str = ""
    actor: str = ""
    action: str = ""
    details: str = ""
    timestamp: str = ""
