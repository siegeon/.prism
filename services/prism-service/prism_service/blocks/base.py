"""The `Block` pydantic model -- metadata for one multiplier block.

A Block instance never HOLDS the callable (pydantic models should stay
plain-data and hashable-by-value); the callable is paired with its
metadata in the registry (`registry.py`). This mirrors how a conductor
step's dict (api/workflows.py) is plain data and the dispatcher looks up
its handler separately.
"""
from typing import Literal

from pydantic import BaseModel, Field

BlockKind = Literal["deterministic", "agentic", "http"]
BlockScope = Literal["task", "project"]
OnFailure = Literal["continue", "stop"]


class Block(BaseModel):
    """One declared, typed, reusable unit of seat behaviour.

    - `id` is the route key: what `run_block` looks it up by, and what a
      future catalog entry's `step["route"]` would carry so
      `_attach_node_trend` finds its run count -- the exact mechanism a
      conductor behaviour sub-step already uses.
    - `kind` names whether `run(ctx)` is a plain deterministic read/write
      (never a model call), an agentic call (a claude -p dispatch), or an
      outbound http call -- so a caller can tell at a glance whether a
      block can ever stall on an LLM.
    - `inputs`/`outputs` are the field names the block's `run` consumes
      from and writes into its context -- not a full JSON Schema (out of
      scope for this landing), but enough that a flow author can see the
      contract without reading the body.
    - `on_failure` mirrors the conductor node vocabulary
      (project_node_steps_declare_onfailure): "stop" means a raised
      exception propagates to the caller after being recorded; "continue"
      means `run_block` swallows it, records the failure, and returns
      None -- the caller's own existing try/except-and-move-on shape,
      made explicit instead of implicit.
    """

    id: str
    title: str
    kind: BlockKind
    owner_seat: str = Field(
        description="module/service that owns the wrapped function, e.g. "
                     "'design_packet', 'resume_actuator', 'gate_adjudicator'")
    scope: BlockScope = "task"
    on_failure: OnFailure = "continue"
    cost_hint: Literal["zero", "low", "medium", "high"] = "zero"
    inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    description: str = ""

    model_config = {"frozen": True}
