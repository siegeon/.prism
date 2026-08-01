"""Per-tool MCP call telemetry read surface (task f1e7e228).

GET /api/tool-usage returns the rows `mcp/server.py`'s dispatch point records,
so "is this MCP tool still used?" is answerable from data rather than from a
repo grep. Resolves scores_db via get_project(project)._data_dir — the same
pattern api/agent_runs.py uses — so a row written at dispatch is readable back
through a SEPARATE HTTP call (on-disk persistence, not an in-memory counter).

Silence here means NO EVIDENCE, never proof of death: a tool only ever called
by an installed hook script or an already-connected external client shows zero
rows until that client next runs.
"""

from typing import Optional

from fastapi import APIRouter, Query

from prism_service.project_context import get_project
from prism_service.services.tool_usage_data import (
    get_tool_calls,
    get_tool_usage_rollup,
)

router = APIRouter()


def _scores_db(project: str) -> str:
    return str(get_project(project)._data_dir / "scores.db")


@router.get("")
def list_tool_usage(
    project: str = Query("default"),
    tool: Optional[str] = Query(None),
    limit: int = Query(1000, ge=1, le=5000),
) -> dict:
    """Raw per-(tool, profile, outcome) counters, busiest first."""
    rows = get_tool_calls(_scores_db(project), tool=tool, project=project,
                          limit=limit)
    return {"rows": rows}


@router.get("/rollup")
def tool_usage_rollup(project: str = Query("default")) -> dict:
    """tool -> {calls, errors, profiles, last_ts}. The ledger's evidence
    column; an absent tool means "not observed yet", not "dead"."""
    return {"tools": get_tool_usage_rollup(_scores_db(project))}
