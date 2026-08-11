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

import logging
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

_adapters: dict = {}

# Why a built-in provider failed to register, if it did. Read by diagnostics
# rather than left to a silent except (task f4dd3687).
adapter_registration_errors: dict = {}


def register_adapter(adapter) -> None:
    _adapters[adapter.provider] = adapter


def reset_adapters() -> None:
    _adapters.clear()


def adapters_snapshot() -> dict:
    """Point-in-time copy of the registry (task c23e2e7b). Tests that
    mutate the registry (reset_adapters/register_adapter) restore this
    afterward instead of leaving it in whatever state their own teardown
    happened to reach - see tests/conftest.py's autouse isolation fixture."""
    return dict(_adapters)


def restore_adapters(snapshot: dict) -> None:
    """Put the registry back to a prior `adapters_snapshot()` (task
    c23e2e7b)."""
    _adapters.clear()
    _adapters.update(snapshot)


def register_builtin_adapters() -> None:
    """Wire the providers PRISM ships with, at import time.

    This used to say adapters would register "in later slices"; nothing ever
    did, so `_adapters` was empty in production and EVERY sync failed with no
    adapter for provider github while the tests stayed green by injecting one
    (task f4dd3687). Registration is deliberately best-effort: a provider whose
    module cannot import must never take the whole API down with it, because
    connecting is optional (mx-639efa).
    """
    try:
        from prism_service.services.github_cli_auth import get_cli_credentials
        from prism_service.services.github_rest import GithubRestClient
        from prism_service.services.github_work import GitHubWorkAdapter

        register_adapter(GitHubWorkAdapter(
            client=GithubRestClient(),
            credentials=get_cli_credentials(),
            # A CLI token is not installation-scoped. The adapter only passes
            # this back to credentials.installation_token(), which ignores it.
            installation="github-cli",
        ))
    except Exception as exc:  # pragma: no cover - a broken provider is not fatal
        # RECORD the reason. Swallowing it silently is how this hole stayed
        # open: the surface looks alive, every sync dies, and nothing anywhere
        # says why. Non-fatal, but never invisible.
        adapter_registration_errors["github"] = f"{type(exc).__name__}: {exc}"
        logging.getLogger(__name__).warning(
            "github adapter not registered, syncs will fail: %s", exc)

    try:
        from prism_service.services.jira_auth import get_jira_auth_store
        from prism_service.services.jira_client import JiraClient
        from prism_service.services.jira_rest import JiraRestClient
        from prism_service.services.jira_work import JiraWorkAdapter

        def _jira_access_token(connection):
            import time

            store = get_jira_auth_store()
            # api_token connections (task 64ba4755, FR-1/FR-3) carry no
            # refresh grant at all - hand JiraClient the full credential so
            # it can Basic-auth the site directly, never the oauth refresh
            # loop below.
            if store.auth_kind(connection.workspace_id, connection.remote_scope) == "api_token":
                return store.credential(connection.workspace_id, connection.remote_scope)

            def _refresh(_refresh_token: str) -> dict:
                # No OAuth network transport is wired in production yet (the
                # same gap integrations_connect.py's own health-check
                # `_no_refresh` accepts today) - an expired token surfaces as
                # a clean adapter error via jira_work.pull_page rather than a
                # silent hang or a raw exception (task 33798164).
                raise RuntimeError(
                    "jira token refresh is not configured on this instance")

            return store.access_token(
                connection.workspace_id, connection.remote_scope,
                now=int(time.time()), refresh=_refresh)

        def _jira_site_url(connection):
            return get_jira_auth_store().site_url(
                connection.workspace_id, connection.remote_scope)

        register_adapter(JiraWorkAdapter(
            client=JiraClient(),
            access_token_provider=_jira_access_token,
            site_url_provider=_jira_site_url,
            # Task 88a7da0b, FR-10: a SEPARATE write client so create/close
            # is reachable in the running process, not only in tests --
            # the recorded failure mode (mx-43374a/mx-14edf5) is green
            # write verbs nothing in production calls.
            write_client=JiraRestClient(),
        ))
    except Exception as exc:  # pragma: no cover - a broken provider is not fatal
        adapter_registration_errors["jira"] = f"{type(exc).__name__}: {exc}"
        logging.getLogger(__name__).warning(
            "jira adapter not registered, syncs will fail: %s", exc)


register_builtin_adapters()


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
