"""Workspace-scoped integration routes (task fddfd75a).

Generic, provider-neutral surface under
``/api/workspaces/{workspace_id}/integrations``. Every route authorizes the
caller's workspace role; the pull route additionally validates that the target
project belongs to THIS workspace BEFORE resolving a ProjectContext or invoking
an adapter, so a cross-workspace id fails without any filesystem or provider
side effect. Request bodies forbid unknown extras so a credential-like field
can never be silently accepted. Pull-only this slice: no outbound route.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from prism_service.models.workspace import Principal
from prism_service.api.auth import (
    coerce_principal,
    current_principal,
    require_workspace_role,
)
from prism_service.services.integration_store import get_integration_store
from prism_service.services.work_item_sync import WorkItemSyncService
from prism_service.services.workspace_service import get_workspace_service


router = APIRouter(dependencies=[Depends(current_principal)])

# Provider adapters (GitHub, Jira, …) register here at import time in later
# slices; tests inject a scripted adapter. Pull-only, no network in this slice.
_adapters: dict = {}


def register_adapter(adapter) -> None:
    _adapters[adapter.provider] = adapter


def reset_adapters() -> None:
    _adapters.clear()


class ConnectionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str
    remote_scope: str
    display_name: str = ""


class ContainerBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connection_id: str
    kind: str
    remote_id: str
    display_key: str = ""
    display_name: str = ""
    url: str = ""


def _authorize(principal, workspace_id: str, minimum_role: str):
    principal = coerce_principal(principal)
    require_workspace_role(principal, workspace_id, minimum_role)
    return principal


@router.get("/{workspace_id}/integrations/connections")
def list_connections(
    workspace_id: str,
    provider: Optional[str] = None,
    principal: Principal = Depends(current_principal),
) -> dict:
    _authorize(principal, workspace_id, "viewer")
    return {"connections": get_integration_store().list_connections(workspace_id, provider)}


@router.post("/{workspace_id}/integrations/connections")
def create_connection(
    workspace_id: str,
    body: ConnectionBody,
    principal: Principal = Depends(current_principal),
) -> dict:
    _authorize(principal, workspace_id, "admin")
    try:
        connection = get_integration_store().ensure_connection(
            workspace_id, body.provider, body.remote_scope, body.display_name)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return {"connection": connection}


@router.get("/{workspace_id}/integrations/containers")
def list_containers(
    workspace_id: str,
    connection_id: Optional[str] = None,
    principal: Principal = Depends(current_principal),
) -> dict:
    _authorize(principal, workspace_id, "viewer")
    return {"containers": get_integration_store().list_containers(workspace_id, connection_id)}


@router.post("/{workspace_id}/integrations/containers")
def create_container(
    workspace_id: str,
    body: ContainerBody,
    principal: Principal = Depends(current_principal),
) -> dict:
    _authorize(principal, workspace_id, "member")
    store = get_integration_store()
    if store.get_connection(workspace_id, body.connection_id) is None:
        raise HTTPException(404, "connection not found in this workspace")
    try:
        container = store.ensure_container(
            workspace_id, body.connection_id, body.kind, body.remote_id,
            display_key=body.display_key, display_name=body.display_name, url=body.url)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return {"container": container}


@router.get("/{workspace_id}/integrations/entities")
def list_entities(
    workspace_id: str,
    container_id: Optional[str] = None,
    connection_id: Optional[str] = None,
    task_id: Optional[str] = None,
    principal: Principal = Depends(current_principal),
) -> dict:
    _authorize(principal, workspace_id, "viewer")
    return {"entities": get_integration_store().list_entities(
        workspace_id, container_id=container_id, connection_id=connection_id, task_id=task_id)}


@router.get("/{workspace_id}/integrations/runs")
def list_runs(
    workspace_id: str,
    container_id: Optional[str] = None,
    principal: Principal = Depends(current_principal),
) -> dict:
    _authorize(principal, workspace_id, "viewer")
    return {"runs": get_integration_store().list_runs(workspace_id, container_id)}


@router.get("/{workspace_id}/integrations/receipts")
def list_receipts(
    workspace_id: str,
    run_id: Optional[str] = None,
    principal: Principal = Depends(current_principal),
) -> dict:
    _authorize(principal, workspace_id, "viewer")
    return {"receipts": get_integration_store().list_receipts(workspace_id, run_id)}


@router.post("/{workspace_id}/integrations/containers/{container_id}/pull")
def pull_container(
    workspace_id: str,
    container_id: str,
    project: str = Query(...),
    principal: Principal = Depends(current_principal),
) -> dict:
    principal = _authorize(principal, workspace_id, "member")

    # The target project must belong to THIS workspace — checked before any
    # ProjectContext resolution or adapter invocation so a cross-workspace id
    # fails without a filesystem or provider side effect.
    if principal.mode == "team":
        owner = get_workspace_service().project_workspace(project)
        if owner is None or owner.id != workspace_id:
            raise HTTPException(403, "project does not belong to this workspace")

    store = get_integration_store()
    container = store.get_container(workspace_id, container_id)
    if container is None:
        raise HTTPException(404, "container not found in this workspace")
    connection = store.get_connection(workspace_id, container.connection_id)
    if connection is None:
        raise HTTPException(404, "connection not found in this workspace")

    from prism_service import project_context

    intake = project_context.get_project(project).task_svc
    sync = WorkItemSyncService(store, intake=intake, registry=_adapters)
    run = sync.pull_container(workspace_id, connection, container)
    return {
        "run": run,
        "imported": run.items_processed,
        "status": run.status,
    }
