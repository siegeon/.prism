"""Execution semantics shared by the Python and Magic behavior runners."""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StepResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["workflow.step_result"] = "workflow.step_result"
    schema_version: Literal[1] = 1
    outcome: Literal["passed", "failed", "error", "skipped", "unknown"]
    reason: str = ""
    outputs: dict[str, Any] = Field(default_factory=dict)
    stop_chain: bool = False

    @property
    def permits_next(self) -> bool:
        return self.outcome in {"passed", "skipped"} and not self.stop_chain


def normalize_step_result(value: Any) -> StepResult:
    """Adapt legacy typed endpoints without confusing delivery with success."""
    if isinstance(value, StepResult):
        return value
    data = value.model_dump() if hasattr(value, "model_dump") else value
    if not isinstance(data, dict) and hasattr(value, "__dict__"):
        data = dict(vars(value))
        for key in ("ok", "reason", "validation", "observe", "stop_chain"):
            if hasattr(value, key):
                data[key] = getattr(value, key)
    if not isinstance(data, dict):
        return StepResult(outcome="error", reason="Step response must be an object")
    if data.get("kind") == "workflow.step_result":
        return StepResult.model_validate(data)
    failed = data.get("ok") is False or data.get("stop_chain") is True
    reason = data.get("reason") if isinstance(data.get("reason"), str) else ""
    for key in ("observe", "reason", "validation"):
        part = data.get(key)
        if isinstance(part, dict) and part.get("ok") is False:
            failed = True
            reason = str(part.get("reason") or reason or f"{key} refused")
    outputs = dict(data)
    if isinstance(data.get("reason"), dict):
        outputs.update(data["reason"].get("fields") or {})
    return StepResult(outcome="failed" if failed else "passed", reason=reason,
                      outputs=outputs, stop_chain=bool(data.get("stop_chain")))
