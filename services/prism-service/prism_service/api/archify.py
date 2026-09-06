"""Archify API — architecture diagram building and rendering.

Exposed routes:
  GET  /api/archify/doctor
  GET  /api/archify/maps
  POST /api/archify/maps/{kind}/build
  GET  /api/archify/maps/{kind}
  GET  /api/archify/maps/{kind}/html
  GET  /api/archify/maps/{kind}/ir
  GET  /api/archify/maps/{kind}/receipt
"""

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse

from prism_service.project_context import get_project
from prism_service.services.archify_service import (
    ArchifyBuildError,
    ArchifyService,
)

router = APIRouter()


@router.get("/doctor")
def doctor(project: str = Query("default")) -> dict:
    """Check archify health."""
    try:
        get_project(project)
    except Exception as exc:
        raise HTTPException(404, f"unknown project: {project}: {exc}")

    svc = ArchifyService(project)
    return svc.doctor()


@router.get("/maps")
def list_maps(project: str = Query("default")) -> dict:
    """List all built maps (meta.json files)."""
    try:
        get_project(project)
    except Exception as exc:
        raise HTTPException(404, f"unknown project: {project}: {exc}")

    svc = ArchifyService(project)
    return {"maps": svc.list_maps()}


@router.post("/maps/{kind}/build")
def build_map(
    kind: str,
    project: str = Query("default"),
    task_id: str | None = Query(None),
) -> dict:
    """Build and render a map.

    Returns meta dict (200 even when ok=false: the receipt is the story).
    400 for unknown kind, 404 for unknown project.
    """
    if kind not in ["code", "concepts", "language", "task"]:
        raise HTTPException(400, f"unknown kind: {kind}")

    if kind == "task" and not task_id:
        raise HTTPException(400, "kind='task' requires task_id")

    try:
        get_project(project)
    except Exception as exc:
        raise HTTPException(404, f"unknown project: {project}: {exc}")

    svc = ArchifyService(project)
    try:
        meta = svc.build(kind, task_id=task_id)
        return meta
    except ValueError as exc:
        # A builder refuses work it cannot do — an unknown task id, say.
        raise HTTPException(400, str(exc))
    except ArchifyBuildError as e:
        # Report THIS build, never the last one that worked. Returning the
        # stored meta here made a failed rebuild read as a fresh success,
        # with the previous map still on screen.
        previous = svc.meta(kind, task_id=task_id) or {}
        return {
            "kind": kind,
            "diagram_type": previous.get("diagram_type", ""),
            "task_id": task_id,
            "title": previous.get("title", kind.capitalize()),
            "built_at": previous.get("built_at", ""),
            "ok": False,
            "components": 0,
            "connections": 0,
            "error": str(e),
            "html_url": previous.get("html_url", ""),
        }


@router.get("/maps/{kind}")
def get_map_meta(
    kind: str,
    project: str = Query("default"),
    task_id: str | None = Query(None),
) -> dict:
    """Get meta.json for a map."""
    if kind not in ["code", "concepts", "language", "task"]:
        raise HTTPException(400, f"unknown kind: {kind}")

    if kind == "task" and not task_id:
        raise HTTPException(400, "kind='task' requires task_id")

    try:
        get_project(project)
    except Exception as exc:
        raise HTTPException(404, f"unknown project: {project}: {exc}")

    svc = ArchifyService(project)
    meta = svc.meta(kind, task_id=task_id)
    if not meta:
        raise HTTPException(404, f"map not found: {kind}" + (f"/{task_id}" if task_id else ""))
    return meta


@router.get("/maps/{kind}/html")
def get_map_html(
    kind: str,
    project: str = Query("default"),
    task_id: str | None = Query(None),
) -> HTMLResponse:
    """Get map.html as an iframe-safe response.

    Must be iframe-able (no X-Frame-Options), Cache-Control: no-cache
    """
    if kind not in ["code", "concepts", "language", "task"]:
        raise HTTPException(400, f"unknown kind: {kind}")

    if kind == "task" and not task_id:
        raise HTTPException(400, "kind='task' requires task_id")

    try:
        get_project(project)
    except Exception as exc:
        raise HTTPException(404, f"unknown project: {project}: {exc}")

    svc = ArchifyService(project)
    html = svc.html(kind, task_id=task_id)
    if not html:
        raise HTTPException(404, f"map not found: {kind}" + (f"/{task_id}" if task_id else ""))

    return HTMLResponse(
        content=html,
        headers={
            "Cache-Control": "no-cache",
            # No X-Frame-Options header — this must be iframe-able
        },
    )


@router.get("/maps/{kind}/ir")
def get_map_ir(
    kind: str,
    project: str = Query("default"),
    task_id: str | None = Query(None),
) -> dict:
    """Get IR JSON for a map."""
    if kind not in ["code", "concepts", "language", "task"]:
        raise HTTPException(400, f"unknown kind: {kind}")

    if kind == "task" and not task_id:
        raise HTTPException(400, "kind='task' requires task_id")

    try:
        get_project(project)
    except Exception as exc:
        raise HTTPException(404, f"unknown project: {project}: {exc}")

    svc = ArchifyService(project)
    ir = svc.ir(kind, task_id=task_id)
    if not ir:
        raise HTTPException(404, f"map not found: {kind}" + (f"/{task_id}" if task_id else ""))
    return ir


@router.get("/maps/{kind}/receipt")
def get_map_receipt(
    kind: str,
    project: str = Query("default"),
    task_id: str | None = Query(None),
) -> dict:
    """Get receipt.json for a map."""
    if kind not in ["code", "concepts", "language", "task"]:
        raise HTTPException(400, f"unknown kind: {kind}")

    if kind == "task" and not task_id:
        raise HTTPException(400, "kind='task' requires task_id")

    try:
        get_project(project)
    except Exception as exc:
        raise HTTPException(404, f"unknown project: {project}: {exc}")

    svc = ArchifyService(project)
    receipt = svc.receipt(kind, task_id=task_id)
    if not receipt:
        raise HTTPException(404, f"receipt not found: {kind}" + (f"/{task_id}" if task_id else ""))
    return receipt
