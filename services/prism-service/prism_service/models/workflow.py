"""Workflow state model and step definitions for PRISM."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


# Gates inherit their validation kind from the nearest PRECEDING agent
# step's `validation` (ConductorService._validation_for_gate). story_gate
# and plan_gate (task 8579d49e) make the story_complete / plan_coverage
# rubrics REACHABLE end-to-end: both are scored by pure YAML-rubric
# functions (services/arc_governance.py) — no override required on a
# compliant drive.
WORKFLOW_STEPS = [
    {"id": "review_previous_notes", "agent": "sm", "type": "agent", "validation": None},
    {"id": "draft_story", "agent": "sm", "type": "agent", "validation": "story_complete"},
    {"id": "story_gate", "agent": None, "type": "gate", "validation": None},
    {"id": "verify_plan", "agent": "sm", "type": "agent", "validation": "plan_coverage"},
    {"id": "plan_gate", "agent": None, "type": "gate", "validation": None},
    {"id": "write_failing_tests", "agent": "qa", "type": "agent", "validation": "red_with_trace"},
    {"id": "red_gate", "agent": None, "type": "gate", "validation": None},
    {"id": "implement_tasks", "agent": "dev", "type": "agent", "validation": "green"},
    {"id": "verify_green_state", "agent": "qa", "type": "agent", "validation": "green_full"},
    {"id": "green_gate", "agent": None, "type": "gate", "validation": None},
]


@dataclass
class WorkflowState:
    """Current state of the PRISM workflow engine."""

    active: bool = False
    workflow: str = ""
    current_step: str = ""
    current_step_index: int = 0
    total_steps: int = len(WORKFLOW_STEPS)
    story_file: str = ""
    paused_for_manual: bool = False
    session_id: str = ""
    model: str = ""
    total_tokens: int = 0
    step_history: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# SDLC LIFECYCLE — the six phases that wrap the 10-step machine above.
#
# WORKFLOW_STEPS enforces the INTERNALS of the implement phase (a TDD cycle).
# The LIFECYCLE is the coarser arc a feature travels end to end:
#   discover -> prototype -> implement -> verify -> ship -> operate
# Each phase declares a DELIVERABLE (the proof that closes it), a capability
# TIER (models/roles.py), the SKILL that runs it, and its honest ENFORCEMENT
# STATUS today. This is the ONE place the six phases are named — taught to any
# connecting model via the MCP instructions + prism_guide. Extend HERE.
# ---------------------------------------------------------------------------

# enforced=conductor gates protect this boundary; partial=conductor drives some
# of it; gates_only=exists only as gates inside another phase; skill_only=a real
# skill runs it with no state-machine backing; missing=no engine yet.
PHASE_STATUSES = ("enforced", "partial", "gates_only", "skill_only", "missing")


@dataclass(frozen=True)
class Phase:
    id: str
    title: str
    purpose: str          # WHAT the phase is for
    deliverable: str      # the artifact/proof that CLOSES the phase
    tier: str             # capability tier (roles.py): frontier|balanced|fast
    skill: str            # the /command that runs it
    status: str           # one of PHASE_STATUSES
    steps: tuple          # conductor WORKFLOW_STEPS this phase covers (else ())

    def as_dict(self) -> dict:
        d = asdict(self)
        d["steps"] = list(self.steps)
        return d


LIFECYCLE = [
    Phase("discover", "Discover",
          "Establish WHAT to build: actors, views, constraints, journeys.",
          "A requirements artifact (actors / views / constraints / journeys).",
          "frontier", "/discover", "skill_only", ()),
    Phase("prototype", "Prototype",
          "Prove the approach before building: research -> plan -> clickable "
          "mock-data prototype you can SEE the feature in.",
          "A clickable prototype + an approved plan (story_gate + plan authoring).",
          "frontier", "/prototype", "partial",
          ("review_previous_notes", "draft_story", "story_gate", "verify_plan")),
    Phase("implement", "Implement",
          "Build the smallest correct change end to end: domain -> db -> api "
          "-> query -> frontend, test-first.",
          "A green build: tests fail first (red_gate), then pass (green_gate).",
          "fast", "/implement", "enforced",
          ("plan_gate", "write_failing_tests", "red_gate", "implement_tasks",
           "verify_green_state", "green_gate")),
    Phase("verify", "Verify",
          "Prove QUALITY beyond the slice: tests, storybook, a11y, docs, "
          "matrix, static analysis.",
          "A quality matrix green across the axes.",
          "balanced", "/verify", "skill_only", ()),
    Phase("ship", "Ship",
          "Deliver: checkin -> CI -> docs -> handoff.",
          "A released, production-verified build.",
          "balanced", "/ship", "skill_only", ()),
    Phase("operate", "Operate",
          "Run it in production: logs, debug, audit, browser automation.",
          "Healthy production (clean logs / audit).",
          "fast", "/operate", "skill_only", ()),
]

_PHASE_BY_ID = {p.id: p for p in LIFECYCLE}
PHASE_ORDER = [p.id for p in LIFECYCLE]
_STEP_PHASE = {s: p.id for p in LIFECYCLE for s in p.steps}


def phase_for_step(step_id: str | None) -> str | None:
    """The lifecycle phase a WORKFLOW_STEPS id belongs to (or None)."""
    if not step_id:
        return None
    return _STEP_PHASE.get(str(step_id).strip())


def phase_obj(phase_id: str | None) -> Phase | None:
    return _PHASE_BY_ID.get(str(phase_id).strip()) if phase_id else None


def lifecycle_registry() -> dict:
    """Serializable lifecycle for the MCP prism_guide read / any API."""
    return {"order": list(PHASE_ORDER), "statuses": list(PHASE_STATUSES),
            "phases": {p.id: p.as_dict() for p in LIFECYCLE}}


def lifecycle_doctrine() -> str:
    """Model-agnostic 'how to move through PRISM' text for the MCP surface:
    the arc, the order, deliverable + tier per phase, and — honestly — which
    phases the state machine enforces vs which are skill-only today."""
    lines = ["SDLC LIFECYCLE (the arc every feature travels):",
             "  " + " -> ".join(PHASE_ORDER),
             "Move a feature phase by phase; each CLOSES on a concrete "
             "deliverable and runs at a capability tier (report the model "
             "you use).", ""]
    for p in LIFECYCLE:
        lines.append(f"- {p.title} ({p.skill}) [tier={p.tier}, {p.status}]: "
                     f"{p.purpose} Deliverable: {p.deliverable}")
    lines += ["", "Only IMPLEMENT is fully state-machine-enforced (the "
              "conductor TDD gate cycle); PROTOTYPE drives the planning gates; "
              "DISCOVER/VERIFY/SHIP/OPERATE run as skills without gate backing "
              "yet (VERIFY also composes with implement's green_gate). Use the "
              "matching /skill per phase; drive implement through the conductor."]
    return "\n".join(lines)
