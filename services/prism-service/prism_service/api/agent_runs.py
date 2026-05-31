"""Agent-run telemetry API (task f4498190).

POST /ingest persists one per-agent/step telemetry row to scores.db
(idempotent on (run_id, agent_id, step)); GET / reads rows back with
filtering. Resolves scores_db via get_project(project)._data_dir — the
exact pattern api/learning.py uses — so a row POSTed here is readable
back through a SEPARATE GET (on-disk persistence, not in-memory).
"""

from typing import Optional

from fastapi import APIRouter, Body, Query

from prism_service.project_context import get_project
from prism_service.services.agent_runs_data import (
    get_agent_runs,
    upsert_agent_run,
)

router = APIRouter()


def _scores_db(project: str) -> str:
    return str(get_project(project)._data_dir / "scores.db")


@router.post("/ingest")
def ingest(
    project: str = Query("default"),
    row: dict = Body(...),
) -> dict:
    """Persist one agent-run telemetry row. Re-POSTing the same
    (run_id, agent_id, step) UPDATES the existing row."""
    upsert_agent_run(_scores_db(project), row)
    return {"ok": True, "run_id": row.get("run_id"),
            "agent_id": row.get("agent_id"), "step": row.get("step")}


@router.get("")
def list_runs(
    project: str = Query("default"),
    task_id: Optional[str] = Query(None),
    session_id: Optional[str] = Query(None),
    workflow_name: Optional[str] = Query(None),
    role: Optional[str] = Query(None),
    step: Optional[str] = Query(None),
    limit: int = Query(500, ge=1, le=2000),
) -> dict:
    rows = get_agent_runs(
        _scores_db(project), limit=limit,
        task_id=task_id, session_id=session_id,
        workflow_name=workflow_name, role=role, step=step,
    )
    return {"rows": rows}
