"""MemoryOperation ABC — the typed contract every memory-op runner implements.

A ``MemoryOperation`` is a *typed* contract (not a duck-typed dict): the shared
runner (``memory_ops.runner.run_one``) and the env-gated worker
(``memory_ops_worker``) both program against this interface, so the only thing
a new op family must supply is:

  * ``select(project)``     — cheap, deterministic candidate ranking. MUST use
    the Tier-1 activation prior (``MemoryService.compute_activation`` /
    ``fading_entries``) so we rank before paying for any inference.
  * ``build_prompt(item, project)`` — the per-item Claude prompt.
  * ``apply_verdict(item, verdict, project)`` — side-effects of the verdict.

Plus four data attributes that drive routing + audit:

  * ``op_type``    — the op family name, written to ``consolidation_runs.op_type``.
  * ``schema``     — the JSON shape the verdict must satisfy.
  * ``model``      — per-op model name for ``claude_cli.invoke(model=...)``;
    empty string falls through to the CLI default (Sonnet). Cheap ops
    (forget / prune) default to a Haiku; synthesis ops (distill) to Opus.
  * ``provenance`` — static provenance metadata stamped onto stored memories.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class MemoryOperation(ABC):
    """Abstract base for a scheduled memory operation.

    Cannot be instantiated directly — concrete ops MUST implement
    ``select`` / ``build_prompt`` / ``apply_verdict``.
    """

    #: Op family name; written verbatim to ``consolidation_runs.op_type``.
    op_type: str = ""
    #: Verdict JSON schema (field -> type hint). Advisory, not enforced here.
    schema: dict[str, Any] = {}
    #: Per-op model for ``claude_cli.invoke(model=...)``. "" => CLI default.
    model: str = ""
    #: Static provenance metadata stamped onto memories this op produces.
    provenance: dict[str, Any] = {}

    @abstractmethod
    def select(self, project: str) -> list:
        """Return the candidate items to process this tick, cheaply ranked
        via the Tier-1 activation prior. Empty list => the tick is a no-op."""
        raise NotImplementedError

    @abstractmethod
    def build_prompt(self, item: Any, project: str) -> str:
        """Build the Claude prompt for ``item``."""
        raise NotImplementedError

    @abstractmethod
    def apply_verdict(self, item: Any, verdict: dict, project: str) -> None:
        """Apply the parsed verdict's side-effects for ``item``."""
        raise NotImplementedError
