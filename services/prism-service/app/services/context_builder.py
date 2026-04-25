"""Deterministic MCP-side context pack builder."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Optional


CONTEXT_PACK_SCHEMA = "prism.context_pack.v1"

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


def _rule_assets() -> list[ContextAsset]:
    return [
        ContextAsset(id=f"rule:{rule_id}", content=content)
        for rule_id, content in RULES.items()
    ]


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
        rules = _rule_assets()
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
                "builder": "app.services.context_builder.ContextBuilder",
                "llm_generated": False,
            },
        }
