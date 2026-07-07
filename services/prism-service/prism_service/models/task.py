"""Task data models for PRISM task management."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4


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
    # Jira mapping (Slice B) — the 1:1 key of the Jira issue this task maps
    # to (e.g. "PLAT-1"), or '' when the task is not linked to Jira. An
    # additive TEXT column (NOT a join table) that rides the same DB path as
    # the other additive fields; the sync worker (Slice D) reads/writes it.
    jira_issue_key: str = ""
    # Multi-source task model — WHERE this task originated. 'prism' (the
    # DEFAULT, so every existing row migrates to it) is a task created
    # in-app; 'jira' is one PULLED IN from a Jira issue (deterministically,
    # keyed by jira_issue_key); 'github' is reserved for a GitHub source.
    # Additive TEXT column, rides the same DB path as jira_issue_key.
    source: str = "prism"

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
