"""Request-scoped MCP context.

The HTTP transport sets this once per MCP request so project scoping does
not depend on mutable globals or thread-local state.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Iterator

from prism_service.config import DEFAULT_PROJECT
from prism_service.models.workspace import Principal


def _local_principal() -> Principal:
    return Principal(
        user_id="local-user",
        email="local@localhost",
        display_name="Local user",
        mode="local",
        role="owner",
    )


@dataclass(frozen=True)
class PrismRequestContext:
    project_id: str = DEFAULT_PROJECT
    request_id: str = ""
    transport: str = "mcp-http"
    tool_profile: str = "interactive"
    # Stable identity only. Raw Authorization credentials never enter the
    # ContextVar, logs, tool arguments, or MCP response payloads.
    principal: Principal = field(default_factory=_local_principal)


_current_request: ContextVar[PrismRequestContext] = ContextVar(
    "prism_request_context",
    default=PrismRequestContext(),
)


def get_request_context() -> PrismRequestContext:
    """Return the current MCP request context."""
    return _current_request.get()


@contextmanager
def use_request_context(ctx: PrismRequestContext) -> Iterator[PrismRequestContext]:
    """Run a block under a request context and always restore the prior one."""
    token = _current_request.set(ctx)
    try:
        yield ctx
    finally:
        _current_request.reset(token)
