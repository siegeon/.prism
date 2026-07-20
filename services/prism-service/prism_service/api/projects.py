"""/api/projects — list, create, and delete PRISM projects.

POST accepts an optional `remote_url` so the header project picker can
seed a github-tracked project in one shot (v5.1 source-pinning).
DELETE wipes the project's data dir + cached service contexts (v5.1.7).
The 'default' project is protected — every endpoint that doesn't
specify a project falls back to it, so the endpoint refuses to delete.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from prism_service.services import sqlite_db
from threading import Thread
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from prism_service.api.auth import (
    coerce_principal,
    current_principal,
    require_project_role,
    require_workspace_role,
)
from prism_service.api.security import canonical_project_id
from prism_service.config import DEFAULT_PROJECT, PROJECTS_DIR, project_data_dir
from prism_service.engines import understand_engine as ue
from prism_service.models.workspace import Principal
from prism_service.project_context import get_all_projects, release_project
from prism_service.services import source_service as ss
from prism_service.services import trash as trash_svc
from prism_service.services.workspace_service import (
    ProjectOwnershipConflict,
    get_workspace_service,
)

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(current_principal)])


def _count_tasks(name: str) -> int:
    """Cheap per-project task row count — a single COUNT(*) against the
    project's tasks.db, NOT a full TaskService.list() load. Mirrors the
    dashboard.py _count() pattern (best-effort: a missing db / table
    reports 0 so the cold-start resolver always gets a key per project)."""
    db = project_data_dir(name) / "tasks.db"
    if not db.exists():
        return 0
    try:
        c = sqlite_db.connect(str(db), timeout=5.0)
        v = c.execute("SELECT COUNT(*) FROM tasks").fetchone()
        c.close()
        return int(v[0]) if v else 0
    except Exception as exc:
        # Graceful fallback stays (picker must render), but never
        # swallow silently — a locked/corrupt db is an operator signal.
        logger.warning("projects _count_tasks fallback for %s: %s", db, exc)
        return 0


@router.get("")
def list_projects(
    principal: Principal = Depends(current_principal),
) -> dict:
    # `projects` stays the flat list for backward-compat; `task_counts`
    # is additive so the SPA cold-start resolver (lib/project.ts) can pick
    # the busiest non-'default' project on first load instead of landing on
    # the empty 'default' blank state (follow-up to #137 item 3).
    principal = coerce_principal(principal)
    projects = get_all_projects() or []
    if principal.mode == "team":
        allowed = set(
            get_workspace_service().list_projects_for_user(principal.user_id)
        )
        projects = [name for name in projects if name in allowed]
    task_counts = {name: _count_tasks(name) for name in projects}
    return {"projects": projects, "task_counts": task_counts}


_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class CreateBody(BaseModel):
    name: str
    # Required in team mode so ownership is explicit before any project
    # directory is resolved. Optional/ignored by legacy local callers.
    workspace_id: Optional[str] = None
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
def create_project(
    body: CreateBody,
    principal: Principal = Depends(current_principal),
) -> dict:
    principal = coerce_principal(principal)
    if principal.mode == "team":
        try:
            name = canonical_project_id(body.name or "")
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    else:
        name = (body.name or "").strip()
        if not _NAME_RE.match(name):
            raise HTTPException(
                400,
                "name must match [A-Za-z0-9][A-Za-z0-9._-]{0,63} "
                "(no slashes, no spaces)",
            )

    source_path = (body.source_path or "").strip()
    remote_url = (body.remote_url or "").strip()
    if source_path and remote_url:
        raise HTTPException(
            400,
            "pass either source_path (folder mode) or remote_url (clone "
            "mode), not both",
        )

    workspace_id = (body.workspace_id or "").strip()
    workspace_service = get_workspace_service()
    if principal.mode == "team":
        if not workspace_id:
            memberships = workspace_service.list_memberships_for_user(
                principal.user_id
            )
            if len(memberships) != 1:
                raise HTTPException(
                    422,
                    "workspace_id is required when the caller has zero or multiple workspaces",
                )
            workspace_id = memberships[0].workspace_id
        require_workspace_role(principal, workspace_id, "admin")
        owned = workspace_service.project_workspace(name)
        if owned is not None and owned.id != workspace_id:
            raise HTTPException(409, "project is owned by another workspace")
        if owned is None and (PROJECTS_DIR / name).is_dir():
            raise HTTPException(
                409,
                "existing unowned project must be bound by a workspace admin",
            )
        try:
            workspace_service.reserve_project(name, workspace_id)
        except ProjectOwnershipConflict as exc:
            raise HTTPException(409, str(exc))

    try:
        # Ownership is durable before this first filesystem mutation.
        pdir = project_data_dir(name)  # seeds source/, graph/, state.json
        head_sha: Optional[str] = None
        bootstrap = "skipped"
        mode = "empty"

        if source_path:
            # Folder mode — no clone. Just record the path and kick off
            # bootstrap (ingest + analyzer-queue refresh) against the
            # bind-mounted dir.
            resolved = ss.set_source_path(name, source_path)
            head_sha = resolved["head_sha"]
            mode = "folder"
            Thread(target=ss.bootstrap_after_clone, args=(name,), daemon=True).start()
            bootstrap = "started"
        elif remote_url:
            # Clone mode — legacy v5.1 path.
            state = ss.ensure_cloned(name, remote_url, body.tracked_ref)
            head_sha = state.head_sha
            s = ue._read_state(name)
            s["remote_url"] = remote_url
            s["tracked_ref"] = body.tracked_ref
            s["mode"] = "clone"
            ue._write_state(name, s)
            Thread(target=ss.bootstrap_after_clone, args=(name,), daemon=True).start()
            bootstrap = "started"
            mode = "clone"
    except ss.SourceUnavailable as exc:
        raise HTTPException(400, str(exc))
    except Exception:
        raise

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
        "workspace_id": workspace_id or None,
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
def delete_project(
    name: str,
    principal: Principal = Depends(current_principal),
) -> dict:
    """Wipe a project's data dir + cached services. Refuses 'default'."""
    principal = coerce_principal(principal)
    if principal.mode == "team":
        try:
            name = canonical_project_id(name or "")
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    else:
        name = (name or "").strip()
        if not _NAME_RE.match(name):
            raise HTTPException(400, "invalid project name")
    if name == DEFAULT_PROJECT:
        raise HTTPException(
            409,
            f"'{DEFAULT_PROJECT}' is the implicit fallback project and "
            "cannot be deleted",
        )
    require_project_role(principal, name, "admin")
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
