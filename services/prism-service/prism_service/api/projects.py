"""/api/projects — list, create, and delete PRISM projects.

POST accepts an optional `remote_url` so the header project picker can
seed a github-tracked project in one shot (v5.1 source-pinning).
DELETE wipes the project's data dir + cached service contexts (v5.1.7).
The 'default' project is protected — every endpoint that doesn't
specify a project falls back to it, so the endpoint refuses to delete.
"""

from __future__ import annotations

import re
from threading import Thread
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from prism_service.config import DEFAULT_PROJECT, PROJECTS_DIR, project_data_dir
from prism_service.engines import understand_engine as ue
from prism_service.project_context import get_all_projects, release_project
from prism_service.services import source_service as ss
from prism_service.services import trash as trash_svc

router = APIRouter()


@router.get("")
def list_projects() -> dict:
    projects = get_all_projects() or []
    return {"projects": projects}


_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class CreateBody(BaseModel):
    name: str
    # v5.2.0 primary path: point PRISM at a bind-mounted folder on the
    # host. This is the simple flow for developer audiences — clone the
    # repo on your host with your existing git auth, mount it into the
    # container, paste the path here.
    source_path: Optional[str] = None
    # v5.1 legacy: PRISM clones the repo server-side. Kept for backward
    # compatibility but no longer the recommended path.
    remote_url: Optional[str] = None
    tracked_ref: str = "origin/main"


@router.post("")
def create_project(body: CreateBody) -> dict:
    name = (body.name or "").strip()
    if not _NAME_RE.match(name):
        raise HTTPException(
            400,
            "name must match [A-Za-z0-9][A-Za-z0-9._-]{0,63} "
            "(no slashes, no spaces)",
        )

    pdir = project_data_dir(name)  # seeds source/, graph/, state.json
    head_sha: Optional[str] = None
    bootstrap = "skipped"
    mode = "empty"
    source_path = (body.source_path or "").strip()
    remote_url = (body.remote_url or "").strip()

    if source_path and remote_url:
        raise HTTPException(
            400,
            "pass either source_path (folder mode) or remote_url (clone "
            "mode), not both",
        )

    if source_path:
        # Folder mode — no clone. Just record the path and kick off
        # bootstrap (ingest + analyzer-queue refresh) against the
        # bind-mounted dir.
        try:
            resolved = ss.set_source_path(name, source_path)
        except ss.SourceUnavailable as e:
            raise HTTPException(400, str(e))
        head_sha = resolved["head_sha"]
        mode = "folder"
        Thread(target=ss.bootstrap_after_clone, args=(name,), daemon=True).start()
        bootstrap = "started"
    elif remote_url:
        # Clone mode — legacy v5.1 path.
        try:
            state = ss.ensure_cloned(name, remote_url, body.tracked_ref)
        except ss.SourceUnavailable as e:
            raise HTTPException(400, str(e))
        head_sha = state.head_sha
        s = ue._read_state(name)
        s["remote_url"] = remote_url
        s["tracked_ref"] = body.tracked_ref
        s["mode"] = "clone"
        ue._write_state(name, s)
        Thread(target=ss.bootstrap_after_clone, args=(name,), daemon=True).start()
        bootstrap = "started"
        mode = "clone"

    return {
        "created": True,
        "name": name,
        "path": str(pdir),
        "mode": mode,
        "source_path": source_path or None,
        "remote_url": remote_url or None,
        "tracked_ref": body.tracked_ref if remote_url else None,
        "head_sha": head_sha,
        "bootstrap": bootstrap,
    }


def _dir_size_bytes(root) -> int:
    """Best-effort recursive size, ignoring unreadable entries."""
    total = 0
    try:
        for p in root.rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                continue
    except OSError:
        return 0
    return total


@router.delete("/{name}")
def delete_project(name: str) -> dict:
    """Wipe a project's data dir + cached services. Refuses 'default'."""
    name = (name or "").strip()
    if not _NAME_RE.match(name):
        raise HTTPException(400, "invalid project name")
    if name == DEFAULT_PROJECT:
        raise HTTPException(
            409,
            f"'{DEFAULT_PROJECT}' is the implicit fallback project and "
            "cannot be deleted",
        )
    pdir = PROJECTS_DIR / name
    if not pdir.is_dir():
        raise HTTPException(404, f"project {name!r} not found")

    freed = _dir_size_bytes(pdir)
    # Drop request-path cache so future GETs on the same name re-init.
    # Timer-thread Brain handles (drift/governance/quality/drainer) hold
    # their own connections — they release on next iteration once
    # `list_projects()` filters this entry out via the `.deleted` marker.
    release_project(name)
    try:
        trash_svc.mark_deleted(pdir)
    except OSError as e:
        raise HTTPException(
            500,
            f"failed to mark {pdir} deleted: {e}",
        )
    return {
        "deleted": True,
        "name": name,
        "freed_bytes": freed,
        "sweep_pending": True,
    }
