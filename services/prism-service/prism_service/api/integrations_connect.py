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

import base64
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


class _RealTransport:
    """Production HTTP transport, the seam's DEFAULT (task 64ba4755).

    The owner's first real connect attempt 503'd \"jira transport is not
    configured\": every test injects a transport here and nothing in
    production ever constructed one. Speaks both verbs this seam serves:
    GET for the api-token validation calls, POST for jira_oauth's
    exchange/refresh. Errors bubble raw; every caller already wraps them
    into sanitized HTTP errors that never carry a token.
    """

    def _round_trip(self, req) -> dict:
        import json as _json
        import urllib.request

        with urllib.request.urlopen(req, timeout=30) as resp:
            return _json.loads(resp.read().decode("utf-8"))

    def get(self, url: str, headers: Optional[dict] = None) -> dict:
        import urllib.request

        return self._round_trip(urllib.request.Request(
            url, headers=headers or {}, method="GET"))

    def post(self, url: str, json: Optional[dict] = None,
             headers: Optional[dict] = None) -> dict:
        import json as _json
        import urllib.request

        merged = {"Content-Type": "application/json",
                  "Accept": "application/json", **(headers or {})}
        return self._round_trip(urllib.request.Request(
            url, data=_json.dumps(json or {}).encode("utf-8"),
            headers=merged, method="POST"))


_real_transport: Optional[_RealTransport] = None


def _transport(provider: str):
    transport = _transports.get(provider)
    if transport is None and provider == "jira":
        # Nothing injected -> the real network, lazily. Kept OUT of
        # _transports so an injection made later still wins and
        # reset_transports() semantics are unchanged.
        global _real_transport
        if _real_transport is None:
            _real_transport = _RealTransport()
        return _real_transport
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
        # Provisioning is check-then-act over a shared db, and two callers
        # (two requests, or a request racing a background sync thread) can
        # both pass the None check — the ids are FIXED, so the loser's
        # INSERT hits the uniqueness constraint. The db is the arbiter:
        # losing that race means the row exists, which is the outcome we
        # wanted, so swallow the duplicate and proceed on the winner's row.
        if service.get_user(user_id) is None:
            try:
                service.create_user(
                    getattr(principal, "email", "") or f"{user_id}@localhost",
                    display_name=getattr(principal, "display_name", "") or "You",
                    user_id=user_id,
                )
            except ValueError:
                pass  # a concurrent caller created it first
        try:
            service.create_workspace("Personal", user_id, workspace_id=scope)
        except ValueError:
            pass  # a concurrent caller created it first
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


def _last_sync_for_containers(store, scope: str, provider: str, containers) -> Optional[dict]:
    """Latest sync_runs row across ``containers`` - IntegrationStore.list_runs'
    own ORDER BY started_at DESC ordering (integration_store.py:504-511),
    never re-derived. The durable "what did the sync do" the connector card
    surfaces (task 1c9899d6, owner complaint 2026-08-11). None ("calmly
    absent") when nothing has synced yet - never fabricated.
    """
    latest = None
    latest_container = None
    for container in containers:
        runs = store.list_runs(scope, container.id)
        if not runs:
            continue
        run = runs[0]
        if latest is None or run.started_at > latest.started_at:
            latest, latest_container = run, container
    if latest is None:
        return None
    reason = ""
    if latest.items_processed == 0:
        reason = (
            "no Jira issues matched the in-flow filter: assigned to you "
            "and not Done"
        ) if provider == "jira" else (
            "no open issues or pull requests were found in the tracked repository"
        )
    return {"container": latest_container.display_key or latest_container.remote_id,
            "status": latest.status, "imported": latest.items_processed,
            "reason": reason}


def _tracking(provider: str, scope: str, containers, connection=None) -> list:
    """Each tracked collection as ``{key, url}`` for the connector card's
    clickable header (task a752e76c). GitHub's url is read verbatim off the
    stored container (FR-2, set at track time - see the /track route below).
    Jira's is derived server-side from the connected site (FR-3); an unknown
    or unresolvable site yields url=="" - JiraAuthStore.site_url already
    degrades JiraAuthError -> "" (jira_auth.py:208-213), never a raise.
    """
    if provider == "jira":
        from prism_service.services.jira_auth import get_jira_auth_store

        site = (get_jira_auth_store().site_url(scope, connection.remote_scope)
                if connection is not None else "")
        return [{"key": k.display_key or k.remote_id,
                 "url": f"{site}/jira/software/projects/{k.display_key or k.remote_id}"
                        if site else ""}
                for k in containers]
    return [{"key": k.display_key or k.remote_id, "url": k.url} for k in containers]


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
            containers = [k for c in conns
                          for k in store.list_containers(scope, c.id)]
            tracking = _tracking(provider, scope, containers)
            last_sync = _last_sync_for_containers(store, scope, provider, containers)
            return {"provider": provider, "name": name, "state": cli["state"],
                    "detail": cli["detail"], "account": cli["account"],
                    "tracking": tracking, "last_sync": last_sync}
        # No usable CLI: fall through to the OAuth app path, which still
        # serves server and multi-user installs.

    # Jira needs NO OAuth app registered when an api-token connection
    # already exists on this instance (task 64ba4755, FR-1) - mirrors
    # github's pre-_configured CLI-credential branch above, for the same
    # reason: an unregistered OAuth app must never be the last word once a
    # working credential is already on file. api_token rows never expire
    # by this store's clock (JiraAuthStore.access_token short-circuits
    # them), so no separate health probe is needed here.
    if provider == "jira":
        from prism_service.services.jira_auth import get_jira_auth_store

        store = get_integration_store()
        auth_store = get_jira_auth_store()

        def _is_api_token(c) -> bool:
            try:
                return auth_store.auth_kind(scope, c.remote_scope) == "api_token"
            except Exception:
                return False

        conns = [c for c in store.list_connections(scope) if c.provider == "jira"]
        api_token_conn = next((c for c in conns if _is_api_token(c)), None)
        if api_token_conn is not None:
            containers = store.list_containers(scope, api_token_conn.id)
            tracking = _tracking(provider, scope, containers, connection=api_token_conn)
            last_sync = _last_sync_for_containers(store, scope, provider, containers)
            return {"provider": provider, "name": name, "state": "connected",
                    "detail": "Connected.",
                    "account": api_token_conn.display_name or api_token_conn.remote_scope,
                    "tracking": tracking, "last_sync": last_sync}

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
    containers = store.list_containers(scope, conn.id)
    tracking = _tracking(provider, scope, containers, connection=conn)
    last_sync = _last_sync_for_containers(store, scope, provider, containers)
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
                    "tracking": tracking, "last_sync": last_sync}
    return {"provider": provider, "name": name, "state": "connected",
            "detail": "Connected.", "account": conn.display_name or conn.remote_scope,
            "tracking": tracking, "last_sync": last_sync}


class SyncBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class TrackBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repo: str


class JiraApiTokenBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    site_url: str
    email: str
    api_token: str


@router.post("/jira/api-token")
def connect_jira_api_token(
    body: JiraApiTokenBody, principal: Principal = Depends(current_principal),
) -> dict:
    """Connect Jira with a site URL + email + Atlassian API token (task
    64ba4755, FR-1) — no OAuth app needed on this instance.

    Validated via GET <site>/rest/api/3/myself over HTTP Basic auth; a
    rejected credential persists nothing. The cloud id is resolved
    best-effort from <site>/_edge/tenant_info (an UNDOCUMENTED endpoint, so
    its failure must never fail the whole connect) and falls back to the
    site URL itself as a stable, unique scope.
    """
    principal = coerce_principal(principal)
    site_url = (body.site_url or "").strip().rstrip("/")
    email = (body.email or "").strip()
    token = body.api_token or ""
    if not site_url or not email or not token:
        raise HTTPException(422, "site_url, email and api_token are required")

    basic = base64.b64encode(f"{email}:{token}".encode()).decode()
    auth_header = f"Basic {basic}"
    transport = _transport("jira")
    try:
        myself = transport.get(f"{site_url}/rest/api/3/myself",
                               headers={"Authorization": auth_header,
                                        "Accept": "application/json"})
    except Exception as exc:  # noqa: BLE001 — sanitize, never leak the token
        raise HTTPException(
            401, f"could not validate the Jira credential: {type(exc).__name__}") from None
    if not isinstance(myself, dict) or not myself.get("accountId"):
        raise HTTPException(401, "could not validate the Jira credential")

    account_name = myself.get("displayName") or myself.get("emailAddress") or email
    account_email = myself.get("emailAddress") or email

    # Best-effort only (plan research rung: tenant_info is not a documented,
    # supported Atlassian API) — a failure here must never fail the connect.
    cloud_id = ""
    try:
        tenant = transport.get(f"{site_url}/_edge/tenant_info",
                               headers={"Authorization": auth_header})
        if isinstance(tenant, dict):
            cloud_id = str(tenant.get("cloudId") or "")
    except Exception:
        cloud_id = ""
    if not cloud_id:
        cloud_id = site_url  # deterministic, unique-per-site fallback scope

    scope = personal_scope(principal)
    from prism_service.services.jira_auth import get_jira_auth_store

    get_jira_auth_store().set_connection(
        scope, cloud_id, access_token=token, refresh_token="", expires_at=0,
        site_url=site_url, auth_kind="api_token",
        account_email=account_email, account_name=account_name)
    connection = get_integration_store().ensure_connection(
        scope, "jira", cloud_id, display_name=account_name)
    return {"connected": True, "account": account_name,
            "connection_id": connection.id}


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
    scope = personal_scope(principal)
    store = get_integration_store()

    if provider == "jira":
        # A bare project key (task 64ba4755, FR-4) — attaches to the
        # connection the connect step already created, never mints a
        # second, credential-less one. github's owner/repo validation
        # below is untouched.
        key = (body.repo or "").strip()
        if not key:
            raise HTTPException(422, "a project key is required")
        conns = [c for c in store.list_connections(scope) if c.provider == "jira"]
        if not conns:
            raise HTTPException(409, "connect Jira before tracking a project")
        connection = conns[0]
        container = store.ensure_container(
            scope, connection.id, "jira_project", key,
            display_key=key, display_name=key)
        return {"provider": provider, "repo": key,
                "connection_id": connection.id, "container_id": container.id}

    repo = (body.repo or "").strip().strip("/")
    if repo.count("/") != 1 or not all(repo.split("/")):
        raise HTTPException(422, "repository must look like owner/repo")

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


class PushBacklogBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dry_run: bool = True
    task_ids: list[str] = []
    assignee: str = ""


def _empty_skip_buckets() -> dict:
    return {"done": [], "cancelled": [], "already_linked": [],
            "other_status": [], "imported": []}


@router.post("/{provider}/push-backlog")
def push_backlog(provider: str, body: PushBacklogBody,
                 project: str = Query(...),
                 principal: Principal = Depends(current_principal)) -> dict:
    """Preview, then explicitly confirm, pushing the PRE-EXISTING active
    backlog toward GitHub (task 733af05f - walking skeleton for epic
    02672417).

    TWO-STEP BY DESIGN (shared-contract clause 5): ``dry_run=True`` (the
    default) calls only ``scan_active_tasks`` - a pure classifier that
    cannot reach GitHub - and creates nothing. ``dry_run=False`` calls the
    existing single-task ``push_task_creation`` once per would-create task,
    never a bulk board scan by itself; this route is the ONLY caller that
    may name more than one task_id, and it still goes through the exact
    same idempotence/sync-switch guards ``push_task`` above already relies
    on. Nothing here is reachable from ``set_sync`` - that endpoint still
    only ever writes the preference.
    """
    principal = coerce_principal(principal)
    if provider not in PROVIDERS:
        raise HTTPException(404, "unknown provider")
    scope = personal_scope(principal)

    if not get_sync_preferences().enabled(scope, provider):
        return {"provider": provider, "dry_run": body.dry_run, "scanned": 0,
                "would_create": [], "created": [],
                "skipped": _empty_skip_buckets(),
                "reason": f"syncing with {provider} is turned off"}

    from prism_service import project_context
    from prism_service.api.integrations import _adapters
    from prism_service.services.integration_outbox import get_outbox
    from prism_service.services.work_item_sync import (
        _provider_active_links, push_task_creation, scan_active_tasks)

    store = get_integration_store()
    task_svc = project_context.get_project(project).task_svc
    tasks = task_svc.list()

    from prism_service.services.task_mirror import IMPORTED_TAGS

    by_id = {t.id: t for t in tasks}
    rows, skipped_imported = [], []
    for t in tasks:
        # ORIGIN-SCOPED (owner 2026-08-12, task f66fc383): an imported task
        # must never export back to the provider it came FROM, but must
        # still preview/push to the OTHER provider. The origin is recorded
        # as its own tag alongside "external" (e.g. ["external", "github"]).
        task_tags = set(t.tags or [])
        if task_tags & IMPORTED_TAGS and provider in task_tags:
            skipped_imported.append(t.id)
            continue
        # PROVIDER-SCOPED, matching push_task_creation's own eligibility
        # check (FR-9, task 88a7da0b) -- a task linked only to another
        # provider must still preview as create-eligible for THIS one,
        # never undercounted as already_linked.
        has_link = bool(_provider_active_links(store, scope, t.id, provider))
        rows.append((t.id, t.status, has_link))

    report = scan_active_tasks(rows)
    skipped = {
        "done": report.skipped_done,
        "cancelled": report.skipped_cancelled,
        "already_linked": report.skipped_already_linked,
        "other_status": report.skipped_other_status,
        "imported": skipped_imported,
    }

    if body.dry_run:
        return {"provider": provider, "dry_run": True,
                "scanned": len(rows), "would_create": report.would_create,
                "created": [], "skipped": skipped, "reason": ""}

    account = body.assignee
    if not account and provider == "github":
        # GitHub-CLI-specific identity source; meaningless for other
        # providers (jira has no equivalent here) so it must never be
        # consulted outside the github push (task 56074410).
        from prism_service.services.github_cli_auth import get_cli_credentials

        account = get_cli_credentials().status().get("account", "")

    targets = [tid for tid in (body.task_ids or report.would_create)
              if tid in report.would_create]

    created = []
    for task_id in targets:
        task = by_id.get(task_id)
        if task is None:
            continue
        result = push_task_creation(
            store, get_outbox(), _adapters, get_sync_preferences().enabled,
            scope, task_id, task.status,
            title=task.title, body=task.description,
            assignee=account, provider=provider, dry_run=False)
        if result.created:
            created.append({"task_id": task_id, "issue": result.issue,
                            "url": result.url})

    return {"provider": provider, "dry_run": False, "scanned": len(rows),
            "would_create": report.would_create, "created": created,
            "skipped": skipped, "reason": ""}


@router.put("/{provider}/sync")
def set_sync(provider: str, body: SyncBody, project: str = Query("prism"),
             principal: Principal = Depends(current_principal)) -> dict:
    """Turn syncing with a provider on or off.

    Separate from connecting on purpose (owner 2026-07-28): a working
    credential must never imply consent to sync. Turning this off leaves the
    connection intact.

    Task 02672417 (AC-2/AC-4): flipping GitHub's switch False->True is what
    starts the outbound push of the pre-existing ACTIVE backlog, ASSIGNED to
    the connected account. Edge-triggered only (AC-7): an ON->ON write (the
    common case — re-loading Settings re-PUTs the current value) must never
    re-fire it. github-only by literal provider check (AC-11): jira is
    unaffected even though it shares this same endpoint shape.
    """
    principal = coerce_principal(principal)
    if provider not in PROVIDERS and provider != "claude":
        raise HTTPException(404, "unknown provider")
    scope = personal_scope(principal)
    prefs = get_sync_preferences()
    was_enabled = prefs.enabled(scope, provider)
    enabled = prefs.set_enabled(scope, provider, body.enabled)

    if provider == "github" and not was_enabled and enabled:
        _fire_backlog_sweep(scope, project)

    return {"provider": provider, "sync_enabled": enabled}


def _fire_backlog_sweep(scope: str, project: str) -> None:
    """push_active_backlog (work_item_sync.py) does the real work; this only
    supplies the candidate tasks and the assignee.

    The TaskService lookup and task snapshot are resolved HERE, synchronously
    on the request thread — the same, already-safe pattern every other route
    on this router uses (push_task, run_sync). Deferring that resolution
    into the spawned thread raced project_context's process-global cache
    against a concurrent foreground caller (e.g. /sync/run) creating the
    SAME sqlite-backed TaskService at the same moment: two threads issuing
    CREATE TABLE on a fresh db file at once, "database is locked"
    (regression caught by test_ac9_neighbouring_suites_stay_green_together
    on test_pull_issues_into_tasks.py). Only the actual GitHub network I/O
    (push_active_backlog) now runs off the request thread, so the switch's
    own click still never blocks on a slow provider; never raises into the
    caller, mirroring task_mirror.py's "never raises, never blocks"
    discipline."""
    import threading

    from prism_service import project_context
    from prism_service.api.integrations import _adapters
    from prism_service.services.github_cli_auth import get_cli_credentials
    from prism_service.services.integration_outbox import get_outbox
    from prism_service.services.work_item_sync import push_active_backlog

    svc = project_context.get_project(project).task_svc
    tasks = svc.list()
    account = get_cli_credentials().status().get("account", "")
    # Bind EVERY collaborator here, on the request thread — never inside
    # the spawned thread. A daemon thread that resolves process-global
    # singletons lazily can outlive the configuration it was fired under
    # and operate on whatever the globals point to by the time it runs
    # (the next test's stores under pytest; a reconfigured registry in a
    # long-lived daemon). The thread below owns only network I/O.
    store = get_integration_store()
    outbox = get_outbox()
    adapters = _adapters
    sync_enabled = get_sync_preferences().enabled

    def _run() -> None:
        try:
            push_active_backlog(
                store, outbox, adapters, sync_enabled, scope, tasks,
                provider="github", assignee=account)
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True).start()


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


@router.get("/mirror")
def mirror_status(principal: Principal = Depends(current_principal)) -> dict:
    """Is a new task actually reaching GitHub? (task 27e543e0)

    Reports the wiring LINK BY LINK — mirror switch, observer registered by
    startup, adapters the process really holds, sync consent, tracked repos —
    rather than one green boolean. Three tasks previously reached DONE on this
    capability while nothing was wired in production, because every check that
    existed could be satisfied with an injected collaborator. This endpoint
    reads the LIVE process, so "it is wired" stops being a claim and becomes
    something a person (or a gate) can look at.
    """
    coerce_principal(principal)
    from prism_service.services import task_mirror

    return task_mirror.status()


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
