"""Bounded planning templates: resolve known facts, name the remaining questions."""
from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PlanningResolution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: Literal["complete", "unknown", "violated", "error"]
    template_id: str = ""
    template_version: int = 1
    input_hash: str = ""
    unresolved: list[str] = Field(default_factory=list)
    fields: dict = Field(default_factory=dict)
    reason: str = ""


def resolve_planning(task, stage: str) -> PlanningResolution:
    if task is None:
        return PlanningResolution(state="error", reason="Task context is unavailable")
    if stage not in {"draft_story", "verify_plan"}:
        return PlanningResolution(state="violated", reason="Unsupported planning stage")
    facts = {key: getattr(task, key, None) for key in
             ("id", "title", "description", "oracle", "verify", "allowed_files", "stop_if", "tags")}
    digest = hashlib.sha256(json.dumps(facts, sort_keys=True, default=str).encode()).hexdigest()
    # Selection is explicit: a populated form does not prove its semantic coverage.
    tags = facts["tags"] or []
    selected = [tag.split(":", 1)[1] for tag in tags if isinstance(tag, str) and tag.startswith("planning-template:")]
    if selected != ["bounded-change-v1"]:
        return PlanningResolution(state="unknown", input_hash=digest,
                                  unresolved=["Select or design a template for this task"],
                                  reason="No unique supported planning template is selected")
    missing = [key for key in ("title", "description", "oracle", "verify", "allowed_files", "stop_if")
               if not facts[key] or (isinstance(facts[key], str) and not facts[key].strip())
               or (isinstance(facts[key], list) and any(not isinstance(v, str) or not v.strip() for v in facts[key]))]
    if missing:
        return PlanningResolution(state="unknown", template_id=selected[0], input_hash=digest,
                                  unresolved=missing, reason="The template has unbound fields")
    fields = {
        "goal": facts["description"],
        "acs": [{"text": facts["title"], "colour": "red_at_base", "oracle": facts["oracle"],
                 "pytest_id": verification} for verification in facts["verify"]],
        "files_to_change": facts["allowed_files"],
        "implementation_notes": facts["description"] + "\nStop when:\n" + "\n".join(facts["stop_if"]),
    }
    if stage == "draft_story":
        fields = {"story_md": "\n".join([
            "## Summary", facts["title"], "## Requirements", facts["description"],
            "## Acceptance Criteria", f"- AC-1: {facts['title']} — oracle: {facts['oracle']}"])}
    return PlanningResolution(state="complete", template_id=selected[0], input_hash=digest,
                              fields=fields, reason="All declared template fields are bound")
