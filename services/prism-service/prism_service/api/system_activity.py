"""System activity feed API -- what the daemon's background passes are
doing RIGHT NOW, for the /live page's "SYSTEM ACTIVITY" panel.

A thin read-through to `services.system_activity`'s in-memory ring buffer:
answers in well under 5ms, never touches sqlite or git, so it stays fast
even while the engine itself is busy driving a task.
"""

from fastapi import APIRouter, Query

from prism_service.services import system_activity

router = APIRouter()


@router.get("")
def activity(project: str = Query("prism")) -> dict:
    """{"running": [...], "recent": [...]} -- running is every in-flight
    pass (own live elapsed_ms as of this call), recent is the most recent
    completed passes, newest first."""
    return system_activity.snapshot(project=project)
