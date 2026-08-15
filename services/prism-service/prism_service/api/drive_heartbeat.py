"""Drive-heartbeat ingest door (task e3b7ebf6).

POST /api/drive-heartbeat/beat persists one mid-step liveness ping to
scores.db via drive_heartbeat.record_heartbeat -- the producer half of the
"driving" activity state consumed read-only by ConductorService.activity_for.
Mirrors api/agent_runs.py's ingest shape: refusals answer 200 with
ok:false (never 4xx), because authorization already owns the 4xx band and
a content refusal must not blur that contract.

A bare ping missing task_id/step/elapsed_s/last_tool/work_units is refused
BY NAME (drive_heartbeat.BEAT_REFUSAL) so a refused caller can self-diagnose
instead of guessing why its beat didn't register (AC-5).
"""

import time

from fastapi import APIRouter, Body, Query

from prism_service.project_context import get_project
from prism_service.services.drive_heartbeat import record_heartbeat

router = APIRouter()


def _scores_db(project: str) -> str:
    return str(get_project(project)._data_dir / "scores.db")


@router.post("/beat")
def beat(
    project: str = Query("default"),
    row: dict = Body(...),
) -> dict:
    """Persist one liveness ping. Returns the store's own ok:true/false
    shape unchanged -- a refusal already names itself (BEAT_REFUSAL) and
    lists the missing field(s), so this door adds no translation layer.

    On acceptance, also publishes a `drive.heartbeat` event onto the bus
    (gamify walking skeleton) so /sse/work and the /live graph can pulse
    the task node live -- best-effort, never lets a publish failure
    surface as a write failure (mirrors task_service.py's task.changed
    publish).
    """
    result = record_heartbeat(_scores_db(project), row)
    if result.get("ok"):
        try:
            from prism_service.events import bus

            bus.publish({
                "project": project,
                "type": "drive.heartbeat",
                "task_id": row.get("task_id"),
                "step": row.get("step"),
                "last_tool": row.get("last_tool"),
                "work_units": row.get("work_units"),
                "elapsed_s": row.get("elapsed_s"),
                "ts": time.time(),
            })
        except Exception:
            pass
    return result
