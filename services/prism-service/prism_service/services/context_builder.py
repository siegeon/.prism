"""Deterministic MCP-side context pack builder."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Any, Optional


CONTEXT_PACK_SCHEMA = "prism.context_pack.v1"

# Default top-N cap for push-injected conventions (env PRISM_CONTEXT_CONVENTIONS_N).
DEFAULT_CONVENTIONS_N = 8

ROLE_ALIASES = {
    "architect": "architect",
    "architecture": "architect",
    "dev": "dev",
    "developer": "dev",
    "engineer": "dev",
    "qa": "qa",
    "quality": "qa",
    "test": "qa",
    "tester": "qa",
    "sm": "sm",
    "story": "sm",
    "planning": "sm",
    "sam": "sm",
    "quinn": "qa",
    "winston": "architect",
    "po": "sm",
}

ROLE_CARDS = {
    "architect": """# Architect
You design system changes before implementation. Read the existing project
shape first, surface constraints, compare options, and return decisions that
can be implemented and verified. Keep PRISM itself MCP-first: service-owned
context, deterministic assembly, optional client adapters.""",
    "dev": """# Developer
You implement the smallest correct change for the active task. Start from the
returned PRISM context, inspect source before editing, preserve public MCP tool
contracts, and verify with focused tests or benchmarks before declaring done.""",
    "qa": """# QA
You protect acceptance criteria and regression safety. Map requirements to
deterministic tests, prefer behavior checks over implementation checks, and
report evidence, gaps, and risk clearly.""",
    "sm": """# Story Manager
You turn product intent into executable tasks. Keep scope tight, write concrete
acceptance criteria, expose dependencies and risk, and avoid implementation
work unless explicitly asked.""",
    "general": """# PRISM Agent
Use the returned PRISM context as the operating frame. Work from indexed
project knowledge, active tasks, workflow state, and durable memory before
making assumptions.""",
}

RULES = {
    "mcp-first": (
        "PRISM MCP is the source of truth for project memory, tasks, workflow, "
        "role framing, and context assembly. Plugins and hooks are adapters."
    ),
    "deterministic-context": (
        "Context packs are assembled by deterministic service code. Do not "
        "invent missing role rules or template structure client-side."
    ),
    "retrieval-led": (
        "Prefer indexed Brain results, Memory entries, and direct source reads "
        "over assumptions. Cite concrete files when making technical claims."
    ),
    "compatibility": (
        "Preserve existing MCP tool names and response fields unless a migration "
        "plan and tests cover the change."
    ),
    "minimum-change": (
        "Builder doctrine — climb the ladder, stop at the first rung that works: "
        "(1) does this need to exist at all? (2) reuse existing code in THIS repo "
        "(brain_search before you write)? (3) stdlib? (4) the framework already "
        "here? (5) an installed dependency? (6) a one-line addition? — only THEN "
        "build new. Smallest correct diff, fewest new files (extend the existing "
        "owner). HARD CARVE-OUT: never drop or skip a demonstrable UI surface to "
        "cut lines — UI-FIRST beats LOC; a diff that removes the customer-visible "
        "surface is a regression, not a win."
    ),
}

# Role-scoped rule allow-list. A rule id absent from a role's list is NOT
# injected for that role. The base rules apply to every role; `minimum-change`
# is Builder(dev)-ONLY on purpose — a lazy-senior doctrine must never reach the
# Verifier(qa) or Steward(sm), whose whole value is being adversarial/thorough
# (fd297cf0 misfire #2). Unlisted roles fall back to BASE_RULE_IDS.
BASE_RULE_IDS = ["mcp-first", "deterministic-context", "retrieval-led",
                 "compatibility"]
ROLE_RULES = {
    "dev": BASE_RULE_IDS + ["minimum-change"],
    "qa": BASE_RULE_IDS,
    "sm": BASE_RULE_IDS,
    "architect": BASE_RULE_IDS,
    "general": BASE_RULE_IDS,
}

TEMPLATES = {
    "architect-decision": """## Architecture Response
1. Current system shape
2. Decision and rationale
3. Interfaces or MCP contract changes
4. Migration and compatibility notes
5. Verification plan""",
    "dev-implementation": """## Developer Response
1. Task interpretation
2. Files and behavior changed
3. Tests or benchmarks run
4. Compatibility impact
5. Remaining risk""",
    "qa-gate": """## QA Response
1. Acceptance criteria trace
2. Tests added or inspected
3. Evidence and results
4. Regression and benchmark risk
5. Gate recommendation""",
    "sm-task": """## Task Brief
1. Problem statement
2. Acceptance criteria
3. Implementation boundaries
4. Dependencies and risks
5. Validation signals""",
    "general": """## Response
1. Relevant PRISM context
2. Action taken or recommendation
3. Verification
4. Follow-up work""",
}

ROLE_TEMPLATES = {
    "architect": "architect-decision",
    "dev": "dev-implementation",
    "qa": "qa-gate",
    "sm": "sm-task",
    "general": "general",
}


@dataclass(frozen=True)
class ContextAsset:
    id: str
    content: str

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()[:12]

    def as_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "content": self.content,
            "digest": self.digest,
        }


def normalize_persona(persona: Optional[str]) -> str:
    """Map caller-provided persona labels to canonical PRISM roles."""
    if not persona:
        return "general"
    key = persona.strip().lower()
    return ROLE_ALIASES.get(key, key if key in ROLE_CARDS else "general")


def _role_asset(persona_key: str) -> ContextAsset:
    content = ROLE_CARDS.get(persona_key, ROLE_CARDS["general"])
    return ContextAsset(id=f"role-card:{persona_key}", content=content)


def role_rule_assets(persona: Optional[str]) -> list[ContextAsset]:
    """Role-scoped rule assets — the SINGLE source of truth for which rules a
    role is told to follow. Consumed by BOTH injection points: context_bundle
    (build/_pack) and the conductor_work per-job splice (conductor_flow._job),
    so a role sees the same rules however the context reaches it."""
    persona_key = normalize_persona(persona)
    rule_ids = ROLE_RULES.get(persona_key, BASE_RULE_IDS)
    return [
        ContextAsset(id=f"rule:{rule_id}", content=RULES[rule_id])
        for rule_id in rule_ids
        if rule_id in RULES
    ]


def _rule_assets(persona: Optional[str] = None) -> list[ContextAsset]:
    return role_rule_assets(persona)


def _template_asset(persona_key: str) -> ContextAsset:
    template_id = ROLE_TEMPLATES.get(persona_key, ROLE_TEMPLATES["general"])
    return ContextAsset(
        id=f"template:{template_id}",
        content=TEMPLATES[template_id],
    )


class ContextBuilder:
    """Build the model-agnostic context bundle returned by MCP."""

    def __init__(
        self,
        *,
        project_id: str,
        brain_svc: Any,
        memory_svc: Any,
        task_svc: Any,
        workflow_svc: Any,
        governance: Any,
        request_id: str = "",
    ) -> None:
        self.project_id = project_id
        self.brain_svc = brain_svc
        self.memory_svc = memory_svc
        self.task_svc = task_svc
        self.workflow_svc = workflow_svc
        self.governance = governance
        self.request_id = request_id

    def build(
        self,
        *,
        persona: Optional[str] = None,
        story_file: Optional[str] = None,
    ) -> dict[str, Any]:
        persona_key = normalize_persona(persona)
        brain_context = self.brain_svc.system_context(
            story_file=story_file,
            persona=persona_key if persona_key != "general" else persona,
        )
        relevant_memory = self._recall_memory(persona_key)
        conventions = self._recall_conventions(persona_key, relevant_memory)
        active_tasks = {
            "in_progress": self.task_svc.list(status="in_progress"),
            "next": self.task_svc.next_task(),
        }
        workflow_state = self.workflow_svc.get_state()
        health = self._health_report()

        context_pack = self._pack(
            persona_input=persona,
            persona_key=persona_key,
            story_file=story_file,
            brain_context=brain_context,
            relevant_memory=relevant_memory,
            active_tasks=active_tasks,
            workflow_state=workflow_state,
            health=health,
        )

        return {
            "brain_context": brain_context,
            "relevant_memory": relevant_memory,
            "conventions": conventions,
            "active_tasks": active_tasks,
            "workflow_state": workflow_state,
            "health": health,
            "context_pack": context_pack,
            "role_card": context_pack["role_card"],
            "rules": context_pack["rules"],
            "template": context_pack["template"],
            "asset_versions": context_pack["asset_versions"],
        }

    def _recall_memory(self, persona_key: str) -> list[Any]:
        if persona_key == "general":
            return []
        try:
            return self.memory_svc.recall(
                query=persona_key,
                domain=persona_key,
                limit=5,
            )
        except Exception:
            return []

    def _recall_conventions(
        self, persona_key: str, persona_memory: list[Any]
    ) -> list[Any]:
        """Push-inject the living conventions every agent must see.

        arc-kit PUSH model: in addition to the domain=persona recall, pull
        domain="feedback" conventions (UI-FIRST, render-structured, gate-
        enforcement, etc.), merge with persona memories, rank by importance
        descending, dedupe by id/name, and cap at top-N (env
        PRISM_CONTEXT_CONVENTIONS_N, default 8). The old _recall_memory scoped
        recall to domain=persona ONLY, so feedback conventions never surfaced.
        """
        cap = self._conventions_cap()
        feedback: list[Any] = []
        try:
            # list_entries returns ALL active feedback-domain entries
            # deterministically (recall() is FTS-relevance-ranked + truncated,
            # which would drop low-relevance-but-high-importance conventions
            # and defeat the importance ranking + top-N cap below).
            feedback = self.memory_svc.list_entries(
                domain="feedback",
                status_filter="active",
            )
        except Exception:
            feedback = []
        # Drop temporally-invalidated (superseded) entries.
        feedback = [e for e in feedback if not self._entry_invalid(e)]

        merged: list[Any] = list(persona_memory) + list(feedback)

        # Dedupe by id (falling back to name) — same convention reachable via
        # both recall paths appears once.
        deduped: list[Any] = []
        seen: set[str] = set()
        for entry in merged:
            key = self._entry_key(entry)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(entry)

        # Rank by importance descending (highest-priority conventions first).
        deduped.sort(key=self._entry_importance, reverse=True)
        return deduped[:cap]

    @staticmethod
    def _conventions_cap() -> int:
        raw = os.environ.get("PRISM_CONTEXT_CONVENTIONS_N", "")
        try:
            n = int(raw)
            return n if n > 0 else DEFAULT_CONVENTIONS_N
        except (TypeError, ValueError):
            return DEFAULT_CONVENTIONS_N

    @staticmethod
    def _entry_key(entry: Any) -> str:
        if isinstance(entry, dict):
            return entry.get("id") or entry.get("name") or repr(entry)
        return getattr(entry, "id", None) or getattr(entry, "name", None) or repr(entry)

    @staticmethod
    def _entry_invalid(entry: Any) -> bool:
        if isinstance(entry, dict):
            return bool(entry.get("invalid_at"))
        return bool(getattr(entry, "invalid_at", ""))

    @staticmethod
    def _entry_importance(entry: Any) -> int:
        if isinstance(entry, dict):
            val = entry.get("importance", 0)
        else:
            val = getattr(entry, "importance", 0)
        try:
            return int(val)
        except (TypeError, ValueError):
            return 0

    def _health_report(self) -> dict[str, Any]:
        try:
            return self.governance.get_health_report()
        except Exception:
            return {"error": "Governance health report unavailable"}

    def _pack(
        self,
        *,
        persona_input: Optional[str],
        persona_key: str,
        story_file: Optional[str],
        brain_context: str,
        relevant_memory: list[Any],
        active_tasks: dict[str, Any],
        workflow_state: Any,
        health: dict[str, Any],
    ) -> dict[str, Any]:
        role = _role_asset(persona_key)
        rules = _rule_assets(persona_key)
        template = _template_asset(persona_key)
        return {
            "schema": CONTEXT_PACK_SCHEMA,
            "version": 1,
            "project_id": self.project_id,
            "request": {
                "request_id": self.request_id,
                "persona": persona_key,
                "persona_input": persona_input or "",
                "story_file": story_file or "",
            },
            "role_card": role.as_dict(),
            "rules": [rule.as_dict() for rule in rules],
            "template": template.as_dict(),
            "relevant_context": {
                "brain_context": brain_context,
                "memory": relevant_memory,
                "active_tasks": active_tasks,
                "workflow_state": workflow_state,
                "health": health,
            },
            "asset_versions": {
                "role_card": role.digest,
                "rules": {rule.id: rule.digest for rule in rules},
                "template": template.digest,
            },
            "determinism": {
                "builder": "prism_service.services.context_builder.ContextBuilder",
                "llm_generated": False,
            },
        }
