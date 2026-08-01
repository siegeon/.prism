"""Single source of truth for PRISM agent roles and capability tiers.

A ROLE is a functional hat in the conductor SDLC (models/workflow.py
WORKFLOW_STEPS). Each role declares a model-AGNOSTIC capability *tier* and a
reasoning *effort*. PRISM never forces a model: it RECOMMENDS a tier and
RECORDS the model a driver actually reports on each agent run. This encodes
the "wargame" economics — spend frontier reasoning on planning and gate
judgment; let a fast model execute the already-decided plan.

Imported by services/context_builder.py, api/roles.py, the conductor
attribution path (services/conductor_service.py), and mirrored to the UI via
/api/roles. Do NOT declare roles anywhere else — fold new aliases in here.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

# Capability tiers — model-agnostic. A driver maps a tier to whatever model it
# has available; PRISM records the actual model string the driver reports.
TIERS = {
    "frontier": ("Most capable reasoning available. Deep planning, adversarial "
                 "gate judgment, wargaming the approach before any build."),
    "balanced": ("Solid reasoning at moderate cost. Adversarial verification "
                 "and failing-test authoring."),
    "fast": ("Cheapest capable model. Mechanical execution of an "
             "already-decided plan."),
}

EFFORTS = ("max", "high", "medium", "low")


@dataclass(frozen=True)
class Role:
    """A functional hat in the SDLC, bound to a capability tier + effort."""

    id: str
    label: str
    tier: str
    effort: str
    purpose: str

    def as_dict(self) -> dict:
        d = asdict(self)
        d["tier_desc"] = TIERS.get(self.tier, "")
        return d


# The 3 canonical roles. IDs kept stable (sm/qa/dev) for schema + task_history
# compatibility; labels carry the model-agnostic meaning.
ROLES = {
    "sm": Role("sm", "Steward", "frontier", "high",
               "Plans and wargames the approach on paper; owns story and plan; "
               "adjudicates gates as an INDEPENDENT reviewer."),
    "qa": Role("qa", "Verifier", "balanced", "high",
               "Adversarial: authors failing tests and verifies green against a "
               "real run, not a static check."),
    "dev": Role("dev", "Builder", "fast", "medium",
                "Executes the battle plan — the smallest change that turns the "
                "failing tests green."),
}

# Strays from the old divergent tables (context_builder architect/general,
# implement.js lead) fold into the canonical 3. Any unknown label -> DEFAULT.
ROLE_ALIASES = {
    "architect": "sm", "architecture": "sm", "planner": "sm", "planning": "sm",
    "story": "sm", "steward": "sm", "reviewer": "sm", "lead": "sm", "po": "sm",
    "sam": "sm", "winston": "sm",
    "developer": "dev", "engineer": "dev", "builder": "dev", "general": "dev",
    "quality": "qa", "test": "qa", "tester": "qa", "verifier": "qa",
    "quinn": "qa",
}

DEFAULT_ROLE = "dev"

# Step -> role. Agent steps mirror WORKFLOW_STEPS[].agent; gates are
# adjudicated by the Steward (frontier) acting as an INDEPENDENT reviewer — a
# distinct actor from whoever produced the step's work (enforced in
# conductor_service.gate_decide).
STEP_ROLES = {
    "review_previous_notes": "sm",
    "draft_story": "sm",
    "story_gate": "sm",
    "verify_plan": "sm",
    "plan_gate": "sm",
    "write_failing_tests": "qa",
    "red_gate": "sm",
    "implement_tasks": "dev",
    "verify_green_state": "qa",
    "green_gate": "sm",
}


def normalize_role(name: str | None) -> str:
    """Map any caller label (persona/agent/alias) to a canonical role id."""
    if not name:
        return DEFAULT_ROLE
    key = str(name).strip().lower()
    if key in ROLES:
        return key
    return ROLE_ALIASES.get(key, DEFAULT_ROLE)


def role_for_step(step_id: str | None) -> str:
    """Canonical role id responsible for a workflow step (gates included)."""
    if not step_id:
        return DEFAULT_ROLE
    return STEP_ROLES.get(str(step_id).strip(), DEFAULT_ROLE)


def role_obj(name: str | None) -> Role:
    """The Role for any label, normalized."""
    return ROLES[normalize_role(name)]


def tier_for_step(step_id: str | None) -> str:
    return role_obj(role_for_step(step_id)).tier


def effort_for_step(step_id: str | None) -> str:
    return role_obj(role_for_step(step_id)).effort


def registry() -> dict:
    """Serializable registry for /api/roles and the MCP prism_roles read."""
    return {
        "tiers": TIERS,
        "efforts": list(EFFORTS),
        "roles": {rid: r.as_dict() for rid, r in ROLES.items()},
        "step_roles": dict(STEP_ROLES),
    }


def doctrine() -> str:
    """Model-agnostic 'how to drive PRISM by role/tier' block for the MCP
    server instructions + prism_onboard. Phrased in TIERS, never model names,
    so any model/harness can honor it."""
    lines = [
        "ROLE & TIER ROUTING (model-agnostic):",
        "PRISM assigns every SDLC step a role with a capability TIER and a "
        "reasoning EFFORT. PRISM never forces a model — map the tier to "
        "whatever model you have, and report the model you used on each "
        "conductor_work call so PRISM can attribute cost.",
        "",
    ]
    for rid, r in ROLES.items():
        lines.append(f"- {r.label} ({rid}): tier={r.tier}, effort={r.effort} "
                     f"— {r.purpose}")
    lines += [
        "",
        "Spend frontier reasoning on planning and gate judgment; let a fast "
        "model execute the decided plan. Gates are adjudicated by the "
        "Steward as an INDEPENDENT reviewer — a DISTINCT actor from whoever "
        "produced the step's work (self-review is refused).",
    ]
    return "\n".join(lines)
