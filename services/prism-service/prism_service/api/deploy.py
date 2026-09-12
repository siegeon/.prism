"""POST /api/deploy/run -- the manual trigger for the deploy seat (task
13cfe8ee).

Starts the IDENTICAL run ship_worker's own post-land hook starts --
`deploy_worker.deploy_once` -- so a hand click and an automatic land can
never disagree about what "deploy" does. `task_id` is optional context: a
task-scoped call records the requested version onto that task's own
history/evidence (what confirm_pending_deploy later resolves); a bare
manual redeploy with no task context still runs the pipeline, it just has
nowhere to write evidence.
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

router = APIRouter()


class DeployRunRequest(BaseModel):
    task_id: str = Field(
        default="",
        description="Task whose landed build this deploy is for. Optional "
                    "-- omit for a bare manual redeploy with no task to "
                    "record evidence against.")


@router.post("/run")
def run_deploy(body: DeployRunRequest,
              project: str = Query("default")) -> dict:
    from prism_service.project_context import get_project
    from prism_service.services import deploy_worker

    ctx = get_project(project)
    return deploy_worker.deploy_once(
        task_svc=ctx.task_svc, task_id=body.task_id, project=project)
