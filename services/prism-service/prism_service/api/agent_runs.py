"""Agent-run telemetry API (task f4498190).

POST /ingest persists one per-agent/step telemetry row to scores.db
(idempotent on (run_id, agent_id, step)) UNLESS the row claims a passing
gate verdict, which is refused by name (task 682b7e48); GET / reads rows
back with filtering, annotated with the gate_source provenance of any
passing verdict. Resolves scores_db via get_project(project)._data_dir —
the exact pattern api/learning.py uses — so a row POSTed here is readable
back through a SEPARATE GET (on-disk persistence, not in-memory).
"""

from typing import Optional

from fastapi import APIRouter, Body, Query

from prism_service.project_context import get_project
from prism_service.services.agent_runs_data import (
    PRODUCER_GATE_VERDICT_REFUSAL,
    get_agent_runs,
    producer_gate_verdict_reason,
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
    """Persist one agent-run telemetry row, UNLESS it claims a passing
    gate verdict. Re-POSTing the same (run_id, agent_id, step) UPDATES the
    existing row, so refusing the write outright is also what stops a
    producer from overwriting a genuine machine row at that same key.

    This door is by construction a NON-machine caller: the conductor's own
    seat builds its row in-process and calls upsert_agent_run directly,
    never over the wire. So the refusal below reads the verdict being
    claimed and never the identity typed into the payload — a
    workflow_name=="conductor" check would block only the honest.

    The refusal answers 200 with ok:false, not 4xx: authorization already
    owns the 4xx band on this route, and a content refusal must not blur
    that contract.
    """
    refusal = producer_gate_verdict_reason(row)
    if refusal:
        return {"ok": False, "refused": PRODUCER_GATE_VERDICT_REFUSAL,
                "reason": refusal, "run_id": row.get("run_id"),
                "agent_id": row.get("agent_id"), "step": row.get("step")}
    upsert_agent_run(_scores_db(project), row)
    # gamify walking skeleton: publish onto the bus so /sse/work and the
    # /live graph can add/update this session's node live. Best-effort,
    # never lets a publish failure surface as a write failure (the row is
    # already committed above).
    try:
        import time

        from prism_service.events import bus

        bus.publish({
            "project": project,
            "type": "agent.run",
            "task_id": row.get("task_id"),
            "session_id": row.get("session_id"),
            "agent_id": row.get("agent_id"),
            "parent_agent_id": row.get("parent_agent_id"),
            "step": row.get("step"),
            "role": row.get("role"),
            "model": row.get("model"),
            "ok": row.get("ok"),
            "ts": time.time(),
        })
    except Exception:
        pass
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
