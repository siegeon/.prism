"""Authenticated workspace and membership management routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from prism_service.models.workspace import Principal
from prism_service.api.auth import (
    coerce_principal,
    current_principal,
    require_workspace_role,
)
from prism_service.services.workspace_service import get_workspace_service


router = APIRouter(dependencies=[Depends(current_principal)])


class WorkspaceBody(BaseModel):
    name: str


class MembershipBody(BaseModel):
    email: str
    display_name: str = ""
    role: str = "member"


@router.get("")
def list_workspaces(principal: Principal = Depends(current_principal)) -> dict:
    principal = coerce_principal(principal)
    if principal.mode == "local":
        return {"workspaces": []}
    return {
        "workspaces": get_workspace_service().list_workspaces_for_user(
            principal.user_id
        )
    }


@router.post("")
def create_workspace(
    body: WorkspaceBody,
    principal: Principal = Depends(current_principal),
) -> dict:
    principal = coerce_principal(principal)
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(422, "name is required")
    service = get_workspace_service()
    if service.get_user(principal.user_id) is None:
        if principal.mode != "local":
            raise HTTPException(401, "authenticated user no longer exists")
        service.create_user(
            principal.email or "local@localhost",
            display_name=principal.display_name or "Local user",
            user_id=principal.user_id,
        )
    return {"workspace": service.create_workspace(name, principal.user_id)}


@router.get("/{workspace_id}/members")
def list_members(
    workspace_id: str,
    principal: Principal = Depends(current_principal),
) -> dict:
    principal = coerce_principal(principal)
    require_workspace_role(principal, workspace_id, "viewer")
    return {"members": get_workspace_service().list_memberships(workspace_id)}


@router.post("/{workspace_id}/members")
def add_member(
    workspace_id: str,
    body: MembershipBody,
    principal: Principal = Depends(current_principal),
) -> dict:
    principal = coerce_principal(principal)
    actor_membership = require_workspace_role(principal, workspace_id, "admin")
    if (
        principal.mode == "team"
        and body.role == "owner"
        and getattr(actor_membership, "role", "") != "owner"
    ):
        raise HTTPException(403, "only an owner can grant the owner role")
    service = get_workspace_service()
    user = service.get_user_by_email((body.email or "").strip())
    if user is None:
        user = service.create_user(
            body.email, display_name=(body.display_name or "").strip()
        )
    try:
        membership = service.add_membership(workspace_id, user.id, body.role)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return {"user": user, "membership": membership}


@router.post("/{workspace_id}/projects/{project_id}")
def bind_project(
    workspace_id: str,
    project_id: str,
    principal: Principal = Depends(current_principal),
) -> dict:
    principal = coerce_principal(principal)
    require_workspace_role(principal, workspace_id, "admin")
    try:
        ownership = get_workspace_service().bind_project(project_id, workspace_id)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(409, str(exc))
    return {"ownership": ownership}
