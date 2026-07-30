"""The provider CONNECT front door (task dbbea1d3).

Owner decision mx-639efa: GitHub and Jira are OPTIONAL places a user connects to
SEE work. PRISM's internal tasks are always the work of record — nothing here
may become a prerequisite for any other capability, and a user with zero
connections has a fully working product.

Connecting therefore works SOLO: the connection attaches to a deterministic
per-user scope (``personal-<user_id>``) that is auto-provisioned on demand
through WorkspaceService's existing public methods. No workspace to create, no
team to join, no admin role to hold.

Security: the OAuth state is one-time and consumed BEFORE any token exchange, so
an unknown or replayed callback creates nothing. The callback redirects only to
a FIXED internal path — never a caller-supplied target. No client secret,
access token, or refresh token is ever placed in a response body.
"""

from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict

from prism_service.api.auth import coerce_principal, current_principal
from prism_service.models.workspace import Principal
from prism_service.services import jira_oauth
from prism_service.services.integration_store import get_integration_store
from prism_service.services.sync_prefs import get_sync_preferences
from prism_service.services.workspace_service import get_workspace_service


router = APIRouter(dependencies=[Depends(current_principal)])

PROVIDERS = ("github", "jira")

# Where the callback lands the browser when it is done. A FIXED internal path:
# never a caller-supplied redirect target.
SETTINGS_RETURN_PATH = "/settings/connections"

_transports: dict = {}
_state_db: Optional[str] = None
_state_store: Optional[jira_oauth.OAuthStateStore] = None


def set_transport(provider: str, transport) -> None:
    """Inject the provider HTTP transport (tests drive the round trip offline)."""
    _transports[provider] = transport


def reset_transports() -> None:
    _transports.clear()


def configure_state_db(path: str) -> None:
    """Point the one-time OAuth state store at ``path`` (tests use a tmp db)."""
    global _state_db, _state_store
    _state_db = path
    _state_store = None


def _states() -> jira_oauth.OAuthStateStore:
    global _state_store
    if _state_store is None:
        path = _state_db
        if not path:
            from prism_service.data_dir import resolve_data_dir
            path = str(resolve_data_dir() / "oauth_states.db")
        _state_store = jira_oauth.OAuthStateStore(path)
    return _state_store


def _transport(provider: str):
    transport = _transports.get(provider)
    if transport is None:
        raise HTTPException(503, f"{provider} transport is not configured")
    return transport


def personal_scope(principal: Principal) -> str:
    """The caller's OWN connection scope, provisioned on demand.

    Deterministic (``personal-<user_id>``) so a repeat connect lands in the same
    place. Created through WorkspaceService's existing public API — the solo
    user never sees a workspace, a membership prompt, or an admin check.
    """
    user_id = (getattr(principal, "user_id", "") or "local-user").strip()
    scope = f"personal-{user_id}"
    service = get_workspace_service()
    if service.get_workspace(scope) is None:
        if service.get_user(user_id) is None:
            service.create_user(
                getattr(principal, "email", "") or f"{user_id}@localhost",
                display_name=getattr(principal, "display_name", "") or "You",
                user_id=user_id,
            )
        service.create_workspace("Personal", user_id, workspace_id=scope)
    return scope


def _configured(provider: str) -> bool:
    if provider == "jira":
        return bool(os.environ.get("PRISM_JIRA_CLIENT_ID", "").strip())
    return bool(os.environ.get("PRISM_GITHUB_APP_SLUG", "").strip()
                or os.environ.get("PRISM_GITHUB_CLIENT_ID", "").strip())


def _github_authorize_url(state: str) -> str:
    slug = os.environ.get("PRISM_GITHUB_APP_SLUG", "").strip()
    if slug:
        return f"https://github.com/apps/{slug}/installations/new?state={state}"
    import urllib.parse
    params = {
        "client_id": os.environ.get("PRISM_GITHUB_CLIENT_ID", "").strip(),
        "scope": "repo read:user",
        "state": state,
    }
    return "https://github.com/login/oauth/authorize?" + urllib.parse.urlencode(params)


class ContainerBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str
    remote_id: str
    display_key: str = ""
    display_name: str = ""
    url: str = ""


@router.get("/providers")
def list_providers(principal: Principal = Depends(current_principal)) -> dict:
    """Which providers this instance can offer. Reports only configuredness —
    never a client id/secret. Providers are optional: an unconfigured provider
    simply cannot be connected yet."""
    coerce_principal(principal)
    return {"providers": [{"provider": p, "configured": _configured(p)}
                          for p in PROVIDERS]}


@router.get("/connections")
def list_my_connections(principal: Principal = Depends(current_principal)) -> dict:
    """The caller's own connections — provider + scope label only. Zero
    connections is a perfectly healthy state."""
    principal = coerce_principal(principal)
    scope = personal_scope(principal)
    rows = get_integration_store().list_connections(scope)
    return {"connections": [
        {"id": c.id, "provider": c.provider, "display_name": c.display_name,
         "remote_scope": c.remote_scope}
        for c in rows]}


def _claude_state() -> tuple:
    """Claude reported through the SAME shape as any other connector — it is an
    integration, not the category the others hang beneath (mx-dc7c38)."""
    try:
        from prism_service.services import github_auth  # noqa: F401
        from prism_service.api.auth import _auth
        _auth()
    except Exception:
        pass
    try:
        from prism_service.services import claude_auth_state  # type: ignore
        return ("connected", "Claude CLI is logged in.", "")
    except Exception:
        pass
    from pathlib import Path as _P
    creds = _P.home() / ".claude" / ".credentials.json"
    if creds.is_file():
        return ("connected", "Claude CLI is logged in.", str(creds))
    return ("not_connected", "Sign in with the Claude CLI to enable analyzers.", "")


def _provider_state(provider: str, scope: str) -> dict:
    """ONE honest status per connector, decided SERVER-side.

    Never "a connection row exists" — an expired or unrefreshable credential
    reads needs_attention, so it surfaces here instead of silently failing at
    the next sync (the recorded likely_misfire).
    """
    if provider == "claude":
        state, detail, account = _claude_state()
        return {"provider": "claude", "name": "Claude", "state": state,
                "detail": detail, "account": account, "tracking": []}

    name = "GitHub" if provider == "github" else "Jira"

    # GitHub needs NO setup when this machine's GitHub CLI is already logged
    # in: the credential is right here and github_work.py only ever wanted a
    # token string (owner 2026-07-28, task f4dd3687). This runs BEFORE the
    # env-var check so an unregistered OAuth app is no longer the last word.
    # `connected` is EARNED: the CLI source validates the token and downgrades
    # a rejected one to needs_attention rather than looking healthy until the
    # first sync 401s.
    if provider == "github":
        from prism_service.services.github_cli_auth import get_cli_credentials

        cli = get_cli_credentials().status()
        if cli["state"] in {"connected", "needs_attention"}:
            store = get_integration_store()
            conns = [c for c in store.list_connections(scope)
                     if c.provider == provider]
            tracking = [k.display_key or k.remote_id
                        for c in conns
                        for k in store.list_containers(scope, c.id)]
            return {"provider": provider, "name": name, "state": cli["state"],
                    "detail": cli["detail"], "account": cli["account"],
                    "tracking": tracking}
        # No usable CLI: fall through to the OAuth app path, which still
        # serves server and multi-user installs.

    if not _configured(provider):
        env = ("PRISM_GITHUB_APP_SLUG" if provider == "github"
               else "PRISM_JIRA_CLIENT_ID / PRISM_JIRA_CLIENT_SECRET")
        return {"provider": provider, "name": name, "state": "not_configured",
                "detail": f"No OAuth app registered on this instance yet. Set {env}.",
                "account": "", "tracking": []}

    store = get_integration_store()
    conns = [c for c in store.list_connections(scope) if c.provider == provider]
    if not conns:
        return {"provider": provider, "name": name, "state": "not_connected",
                "detail": f"Connect {name} to see work that lives there.",
                "account": "", "tracking": []}

    conn = conns[0]
    tracking = [k.display_key or k.remote_id
                for k in store.list_containers(scope, conn.id)]
    if provider == "jira":
        # REAL credential health, not row presence.
        try:
            import time
            from prism_service.services.jira_auth import get_jira_auth_store

            def _no_refresh(_rt):
                raise RuntimeError("refresh unavailable")

            get_jira_auth_store().access_token(
                scope, conn.remote_scope, now=int(time.time()), refresh=_no_refresh)
        except Exception:
            return {"provider": provider, "name": name,
                    "state": "needs_attention",
                    "detail": "Access was revoked or expired. Reconnect to re-authorize.",
                    "account": conn.display_name or conn.remote_scope,
                    "tracking": tracking}
    return {"provider": provider, "name": name, "state": "connected",
            "detail": "Connected.", "account": conn.display_name or conn.remote_scope,
            "tracking": tracking}


class SyncBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class TrackBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repo: str


@router.post("/{provider}/track")
def track_repo(provider: str, body: TrackBody,
               principal: Principal = Depends(current_principal)) -> dict:
    """Track a repository, with NO team workspace required.

    The connection attaches to the caller's own scope, provisioned on demand.
    The workspace-scoped route in api/integrations.py needs a membership that
    a local install never has, which is why the picker was unreachable
    (task 900a4fb9).
    """
    principal = coerce_principal(principal)
    if provider not in PROVIDERS:
        raise HTTPException(404, "unknown provider")
    repo = (body.repo or "").strip().strip("/")
    if repo.count("/") != 1 or not all(repo.split("/")):
        raise HTTPException(422, "repository must look like owner/repo")

    scope = personal_scope(principal)
    store = get_integration_store()
    owner = repo.split("/")[0]
    connection = store.ensure_connection(scope, provider, owner,
                                         display_name=owner)
    container = store.ensure_container(
        scope, connection.id, "repository", repo,
        display_key=repo, display_name=repo,
        url=f"https://github.com/{repo}")
    return {"provider": provider, "repo": repo,
            "connection_id": connection.id, "container_id": container.id}


@router.post("/{provider}/sync/run")
def run_sync(provider: str, project: str = Query(...),
             principal: Principal = Depends(current_principal)) -> dict:
    """Pull the tracked containers into PRISM TASKS.

    Same wiring as the workspace route (api/integrations.py:215-224), differing
    only in the scope it authorizes: intake is the project's task service, so
    an imported issue becomes an ordinary PRISM task on /api/tasks, which is
    what the Work page reads. PRISM's tasks are the record; this is a mirror,
    never a second list (owner 2026-07-28).
    """
    principal = coerce_principal(principal)
    if provider not in PROVIDERS:
        raise HTTPException(404, "unknown provider")
    scope = personal_scope(principal)

    # The switch decides FIRST, before any container is resolved or any
    # provider is contacted, so turning it off really stops the sync.
    if not get_sync_preferences().enabled(scope, provider):
        raise HTTPException(
            409, f"syncing with {provider} is turned off. Turn it on from the "
                 f"{provider} connector to sync.")

    from prism_service import project_context
    from prism_service.api.integrations import _adapters
    from prism_service.services.work_item_sync import WorkItemSyncService

    store = get_integration_store()
    intake = project_context.get_project(project).task_svc
    sync = WorkItemSyncService(store, intake=intake, registry=_adapters)

    runs, imported = [], 0
    for connection in store.list_connections(scope):
        if connection.provider != provider:
            continue
        for container in store.list_containers(scope, connection.id):
            run = sync.pull_container(scope, connection, container)
            runs.append({"container": container.display_key or container.remote_id,
                         "status": run.status,
                         "imported": run.items_processed})
            imported += run.items_processed or 0
    if not runs:
        raise HTTPException(
            409, f"no {provider} repository is being tracked yet. Add one to "
                 f"start syncing.")
    return {"provider": provider, "imported": imported, "runs": runs}


class PushBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str
    dry_run: bool = True


@router.post("/{provider}/push")
def push_task(provider: str, body: PushBody, project: str = Query(...),
              principal: Principal = Depends(current_principal)) -> dict:
    """Push the ONE task named by ``task_id`` toward its GitHub counterpart:
    close a done task's mirrored issue, or CREATE one for an active task that
    has none yet (task 7cf6a2e5).

    Explicitly scoped to ONE task_id per call — never a sweep over the board
    (task ae67ed5c: an unattended drain over every done task would close many
    real issues on first activation; the same reasoning applies to creating
    issues for every active task in one call). ``dry_run`` defaults True: a
    caller must deliberately ask for the live write. The switch decides
    FIRST, exactly as it does for the pull direction above, before any link,
    container or connection is resolved and before GitHub is contacted at
    all.

    Assignment (owner decision 2026-07-29): a task already ``in_progress``
    is assigned to the connected account, since a real actor is already
    working it; any other active status goes up unassigned. The one-time
    backfill of tasks that predate this capability (assigned to the
    connected account regardless of status, "because they came from my
    personal state") is a deliberate, explicit, one-off operation — not a
    standing rule this endpoint applies to every future task — and is
    driven by naming an explicit ``assignee`` at the call site, never
    inferred here.
    """
    principal = coerce_principal(principal)
    if provider not in PROVIDERS:
        raise HTTPException(404, "unknown provider")
    scope = personal_scope(principal)

    from prism_service import project_context
    from prism_service.api.integrations import _adapters
    from prism_service.services.integration_outbox import get_outbox
    from prism_service.services.work_item_sync import (
        push_task_closure, push_task_creation)

    task = project_context.get_project(project).task_svc.get(body.task_id)
    task_is_done = bool(task is not None and task.status == "done")

    if task_is_done:
        result = push_task_closure(
            get_integration_store(), get_outbox(), _adapters,
            get_sync_preferences().enabled,
            scope, body.task_id, task_is_done,
            provider=provider, dry_run=body.dry_run)
        return {
            "task_id": result.task_id, "provider": result.provider,
            "dry_run": result.dry_run, "eligible": result.eligible,
            "closed": result.closed, "reason": result.reason,
            "repo": result.repo, "issue": result.issue, "url": result.url,
        }

    status = task.status if task is not None else ""
    account = ""
    if status == "in_progress":
        from prism_service.services.github_cli_auth import get_cli_credentials

        account = get_cli_credentials().status().get("account", "")

    result = push_task_creation(
        get_integration_store(), get_outbox(), _adapters,
        get_sync_preferences().enabled,
        scope, body.task_id, status,
        title=(task.title if task is not None else ""),
        body=(task.description if task is not None else ""),
        assignee=account, provider=provider, dry_run=body.dry_run)
    return {
        "task_id": result.task_id, "provider": result.provider,
        "dry_run": result.dry_run, "eligible": result.eligible,
        "created": result.created, "reason": result.reason,
        "repo": result.repo, "issue": result.issue, "url": result.url,
        "assignee": result.assignee,
    }


@router.put("/{provider}/sync")
def set_sync(provider: str, body: SyncBody,
             principal: Principal = Depends(current_principal)) -> dict:
    """Turn syncing with a provider on or off.

    Separate from connecting on purpose (owner 2026-07-28): a working
    credential must never imply consent to sync. Turning this off leaves the
    connection intact.
    """
    principal = coerce_principal(principal)
    if provider not in PROVIDERS and provider != "claude":
        raise HTTPException(404, "unknown provider")
    scope = personal_scope(principal)
    enabled = get_sync_preferences().set_enabled(scope, provider, body.enabled)
    return {"provider": provider, "sync_enabled": enabled}


@router.get("/status")
def connector_status(principal: Principal = Depends(current_principal)) -> dict:
    """Every connector, connected or not, with ONE server-decided status each.

    Zero connections is a calm, complete answer — never an error and never a
    nag: PRISM's own tasks are the work of record (mx-639efa).
    """
    principal = coerce_principal(principal)
    scope = personal_scope(principal)
    prefs = get_sync_preferences()
    rows = []
    for p in ("claude", "github", "jira"):
        row = _provider_state(p, scope)
        # Computed INDEPENDENTLY of row["state"]: a usable credential is not
        # consent to sync (owner 2026-07-28, task 01118728).
        row["sync_enabled"] = prefs.enabled(scope, p)
        rows.append(row)
    return {"connectors": rows}


@router.get("/{provider}/start")
def start_connect(provider: str,
                  principal: Principal = Depends(current_principal)) -> dict:
    """Begin the OAuth round trip: mint a ONE-TIME state bound to the caller's
    own scope and hand back the provider's authorize URL for the SPA to open."""
    principal = coerce_principal(principal)
    if provider not in PROVIDERS:
        raise HTTPException(404, "unknown provider")
    if not _configured(provider):
        raise HTTPException(
            409, f"{provider} is not configured on this instance yet")
    scope = personal_scope(principal)
    state = _states().issue(scope)
    url = (jira_oauth.build_authorize_url(state) if provider == "jira"
           else _github_authorize_url(state))
    return {"authorize_url": url, "provider": provider}


@router.get("/{provider}/callback")
def oauth_callback(provider: str, code: str = Query(""), state: str = Query("")):
    """Finish the round trip. The one-time state is consumed FIRST, so an
    unknown or replayed callback creates nothing at all."""
    if provider not in PROVIDERS:
        raise HTTPException(404, "unknown provider")
    try:
        scope = _states().consume(state)
    except jira_oauth.OAuthStateError:
        raise HTTPException(400, "invalid or already-used oauth state")
    if not code:
        raise HTTPException(400, "missing authorization code")

    transport = _transport(provider)
    tokens = jira_oauth.exchange_code(code, transport)
    access = str(tokens.get("access_token") or "")
    refresh = str(tokens.get("refresh_token") or "")

    remote_scope, display = provider, provider
    if provider == "jira":
        resources = jira_oauth.discover_cloud_id(access, transport) or []
        site = resources[0] if resources else {}
        remote_scope = str(site.get("id") or "cloud")
        display = str(site.get("name") or site.get("url") or "Jira")
        # Tokens live in the server-side store, never in a response.
        try:
            from prism_service.services.jira_auth import get_jira_auth_store
            get_jira_auth_store().set_connection(
                scope, remote_scope, access_token=access, refresh_token=refresh,
                expires_at=int(tokens.get("expires_in") or 3600),
                site_url=str(site.get("url") or ""))
        except Exception:
            pass
    else:
        remote_scope = str(tokens.get("installation_id") or "installation")
        display = "GitHub"

    get_integration_store().ensure_connection(
        scope, provider, remote_scope, display_name=display)
    # Fixed internal destination only.
    return RedirectResponse(SETTINGS_RETURN_PATH, status_code=302)


@router.post("/connections/{connection_id}/containers")
def add_container(connection_id: str, body: ContainerBody,
                  principal: Principal = Depends(current_principal)) -> dict:
    """Pick WHICH repository / Jira project to track. This is the last wiring
    step: the container it creates is exactly what the already-shipped
    WorkItemSyncService pull path imports."""
    principal = coerce_principal(principal)
    scope = personal_scope(principal)
    store = get_integration_store()
    if store.get_connection(scope, connection_id) is None:
        raise HTTPException(404, "connection not found")
    try:
        container = store.ensure_container(
            scope, connection_id, body.kind, body.remote_id,
            display_key=body.display_key, display_name=body.display_name,
            url=body.url)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return {"container": {"id": container.id, "kind": container.kind,
                          "remote_id": container.remote_id,
                          "display_key": container.display_key}}
