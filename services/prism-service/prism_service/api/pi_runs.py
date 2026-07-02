"""/api/pi-runs — audit ledger of PRISM's internal pi/local inference runs.

Backed by the file-based log at /data/pi_runs/. See
prism_service.services.pi_run_log for the on-disk schema. Sibling of
/api/claude-runs — the SPA's /internal-agent view renders these rows.

Endpoints:
  GET /api/pi-runs              recent manifest rows (newest first)
  GET /api/pi-runs/{run_id}     full manifest entry for one run
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from prism_service.services import pi_run_log

router = APIRouter()


@router.get("")
def list_runs(
    limit: int = Query(50, ge=1, le=500),
    project: Optional[str] = Query(None),
) -> dict:
    runs = pi_run_log.list_recent(limit=limit, project=project)
    return {"runs": runs, "count": len(runs)}


@router.get("/{run_id}")
def get_run(run_id: str) -> dict:
    entry = pi_run_log.get_run(run_id)
    if entry is None:
        raise HTTPException(404, f"run {run_id!r} not found")
    return entry
