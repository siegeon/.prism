"""GET /api/workflows/live -- the 1-second live channel (see
services/workflow_live.py for why this is a separate route from
GET /api/workflows: that endpoint answers the full catalog and measured
21-52s under load, while the live signal underneath it is tiny).

A standalone router, included directly in main.py rather than nested under
api/__init__.py's `api_router.include_router(workflows_router, ...)`, so
this stays wired even while api/workflows.py is mid-edit by a sibling pass.
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from prism_service.services import workflow_live

router = APIRouter()


@router.get("/live")
def get_workflows_live(project: str = Query("default")) -> dict:
    return workflow_live.live_for_project(project)
