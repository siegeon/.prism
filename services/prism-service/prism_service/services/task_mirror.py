"""Make a NEW PRISM task actually reach GitHub (task 27e543e0).

THE BUG THIS CLOSES. Every piece of the mirror already existed and worked: the
GitHub adapter registers at import (api/integrations.py:94), the repo is
tracked, the sync switch is on, and ``push_task_creation`` reports
``eligible=True, "would create"`` for a task the owner made hours ago. Nothing
ever CALLED it. The push was reachable only by hand-POSTing
``/api/integrations/connect/github/push`` with a task_id, so tasks created
through the UI or the MCP tool existed only in PRISM. The feature was not
missing -- it was unwired, which reads identically to a person.

WHY A CREATE OBSERVER AND NOT A SWEEP. The recorded misfire for the push
slices (tasks ae67ed5c, 7cf6a2e5) is an unattended drain that mirrors the whole
backlog the moment it switches on. This is structurally incapable of that: it
is handed exactly ONE task -- the one just created -- and no code path in this
module enumerates tasks. A pre-existing backlog stays local until someone
deliberately pushes it.

WHY IT IS ON BY DEFAULT. Owner doctrine mx-71dc57: a capability that ships
disabled is not shipped. ``PRISM_TASK_MIRROR=off`` exists so a user can turn
the mirror OFF, never so they have to turn it ON. It is also not the only
switch -- ``push_task_creation`` still consults the per-provider sync
preference FIRST, so a user who never connected GitHub gets exactly nothing.

SCOPE. Local mode only. The mirror pushes to the connection owned by the
personal scope the connect UI provisions (``personal-<user_id>``), resolved
through the SAME helper that surface uses so the two can never drift. A
team-mode install has no single "the user" for a background thread to act as,
so there the push stays an explicit, credentialed call.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

MIRROR_ENV = "PRISM_TASK_MIRROR"

# The observers dispatch by this NAMED tuple, never by iterating whatever
# happens to be registered in _adapters (mx-804f8e's discipline, task
# 88a7da0b FR-8) -- a stray "gitlab" test double or a future adapter must
# never silently gain outbound push just by being present in the registry.
MIRROR_PROVIDERS = ("github", "jira")

# Tasks that came FROM a provider already have a counterpart; pushing one back
# would mint a second issue for the same work. ensure_external_intake does not
# route through create(), so this is belt-and-braces rather than the only
# guard, but the invariant is worth stating where a reader will look for it.
IMPORTED_TAGS = frozenset({"external"})


def mirror_enabled() -> bool:
    """Default ON. Only an explicit off-ish value disables the mirror."""
    return os.environ.get(MIRROR_ENV, "on").strip().lower() not in (
        "off", "0", "false", "no", "none")


@dataclass
class MirrorOutcome:
    """What the mirror did, in the words a person would use. Always returned --
    a skip is a normal, reportable answer, never an exception."""

    task_id: str
    attempted: bool = False
    created: bool = False
    reason: str = ""
    url: str = ""
    issue: str = ""
    repo: str = ""


def personal_scope() -> Optional[str]:
    """The scope the connect UI provisions, resolved WITHOUT a request.

    Deliberately delegates to ``api.integrations_connect.personal_scope`` with
    the local owner principal rather than re-deriving ``personal-<user_id>``
    here: two independent spellings of the same scope is precisely how a
    background writer ends up pushing into a workspace the settings page never
    reads. Returns None when no credential-free principal exists (team mode),
    which is the signal to leave the push explicit.
    """
    try:
        from prism_service.api.integrations_connect import (
            personal_scope as _scope)
        from prism_service.services.auth_service import (
            AuthenticationRequired, AuthService)
        from prism_service.services.workspace_service import (
            get_workspace_service)

        auth = AuthService(get_workspace_service())
        if auth.mode != "local":
            return None
        try:
            principal = auth.resolve_principal(None)
        except AuthenticationRequired:
            return None
        return _scope(principal)
    except Exception as exc:  # pragma: no cover - never break task creation
        log.warning("task mirror could not resolve a scope: %s", exc)
        return None


def _record_backlink(task_svc, task_id: str, provider: str, result) -> None:
    """Tag the task with its mirror provider.

    The board's link-out badge is STORE-DERIVED (task 6fbbec35 --
    api/tasks.py's ``_all_mirrors_index`` reads active
    ``work_item_external_links`` rows into ``task.mirrors[]``), one badge
    per active mirror, independent of description text. This function
    used to ALSO append a "Mirrored to {provider} {issue}.\\n{url}" line
    to ``task.description`` as a second, redundant channel (task
    675dd11a); that write is removed here. The raw description text on
    rows written before the store link existed is preserved as LEGACY
    DERIVATION INPUT ONLY -- ``api/tasks.py``'s ``_mirror_url`` still
    regexes it for those legacy rows -- but it is never the live badge
    source going forward, and this function no longer writes prose into
    it at all.
    """
    task = task_svc.get(task_id)
    if task is None or not result.url:
        return
    tags = list(getattr(task, "tags", None) or [])
    if provider not in tags:
        tags.append(provider)
        task_svc.update(task_id, tags=tags)


def mirror_task(project: str, task_id: str, provider: str = "github",
                dry_run: bool = False) -> MirrorOutcome:
    """Push ONE task to its provider counterpart. The synchronous core.

    Every guard that the hand-driven endpoint applies still applies here --
    this calls the SAME ``push_task_creation``, with the SAME sync-preference
    function, so the mirror can never take a route the explicit push would
    have refused.
    """
    outcome = MirrorOutcome(task_id=task_id)
    if not mirror_enabled():
        outcome.reason = f"{MIRROR_ENV} is off"
        return outcome

    scope = personal_scope()
    if scope is None:
        outcome.reason = "no local scope; push stays explicit in team mode"
        return outcome

    from prism_service import project_context
    from prism_service.api.integrations import _adapters
    from prism_service.services.integration_outbox import get_outbox
    from prism_service.services.integration_store import get_integration_store
    from prism_service.services.sync_prefs import get_sync_preferences
    from prism_service.services.work_item_sync import push_task_creation

    task_svc = project_context.get_project(project).task_svc
    task = task_svc.get(task_id)
    if task is None:
        outcome.reason = "task no longer exists"
        return outcome

    tags = {str(t).lower() for t in (getattr(task, "tags", None) or [])}
    if tags & IMPORTED_TAGS:
        outcome.reason = "task was imported from a provider; not pushed back"
        return outcome

    outcome.attempted = True
    result = push_task_creation(
        get_integration_store(), get_outbox(), _adapters,
        get_sync_preferences().enabled,
        scope, task_id, getattr(task, "status", ""),
        title=getattr(task, "title", ""),
        body=getattr(task, "description", "") or "",
        # Unassigned on creation: a brand-new task has no actor yet. The
        # in_progress-assigns-the-connected-account rule (owner 2026-07-29)
        # is the explicit endpoint's decision, not this one's.
        assignee="", provider=provider, dry_run=dry_run)

    outcome.created = bool(result.created)
    outcome.reason = result.reason
    outcome.url = result.url
    outcome.issue = result.issue
    outcome.repo = result.repo

    if result.created:
        _record_backlink(task_svc, task_id, provider, result)
        task_svc.record_history(
            task_id, "mirrored",
            details=f"{provider} {result.issue} {result.url}".strip(),
            actor="task-mirror")
    return outcome


def close_task(project: str, task_id: str, provider: str = "github",
               dry_run: bool = False) -> MirrorOutcome:
    """Close ONE task's provider counterpart. The synchronous core of the
    outbound STATUS leg (task 0a9b511f).

    ``push_task_closure`` already existed and worked; its only caller was the
    hand-POSTed connect/push endpoint, so marking a task done in PRISM left
    GitHub untouched. This is the missing call, not a new capability, and it
    deliberately reuses the SAME guards ``mirror_task`` applies so the two legs
    can never diverge on who is allowed to write.

    ``IMPORTED_TAGS`` is NOT applied here, unlike the create leg. A task that
    came FROM GitHub is exactly the task whose issue should close when it
    completes; refusing those is what would break the round trip.
    """
    outcome = MirrorOutcome(task_id=task_id)
    if not mirror_enabled():
        outcome.reason = f"{MIRROR_ENV} is off"
        return outcome

    scope = personal_scope()
    if scope is None:
        outcome.reason = "no local scope; closure stays explicit in team mode"
        return outcome

    from prism_service import project_context
    from prism_service.api.integrations import _adapters
    from prism_service.services.integration_outbox import get_outbox
    from prism_service.services.integration_store import get_integration_store
    from prism_service.services.sync_prefs import get_sync_preferences
    from prism_service.services.work_item_sync import push_task_closure

    task_svc = project_context.get_project(project).task_svc
    task = task_svc.get(task_id)
    if task is None:
        outcome.reason = "task no longer exists"
        return outcome

    outcome.attempted = True
    result = push_task_closure(
        get_integration_store(), get_outbox(), _adapters,
        get_sync_preferences().enabled,
        scope, task_id, getattr(task, "status", "") == "done",
        provider=provider, dry_run=dry_run)

    outcome.created = bool(getattr(result, "closed", False))
    outcome.reason = result.reason
    outcome.url = result.url
    outcome.issue = result.issue
    outcome.repo = result.repo

    if outcome.created:
        task_svc.record_history(
            task_id, "mirror_closed",
            details=f"{provider} {result.issue} {result.url}".strip(),
            actor=f"{provider}-mirror")
    return outcome


def _on_task_created(project: str, task) -> None:
    """The observer TaskService fires. Never raises, never blocks.

    Off-thread because creating a task must not wait on GitHub: the MCP
    ``task_create`` verb and the SPA's create button both sit on this path, and
    a provider timeout there would look like PRISM hanging.
    """
    if not mirror_enabled():
        return
    task_id = getattr(task, "id", "")
    if not task_id:
        return

    def _run() -> None:
        # FR-8/NFR-6: each provider dispatches by the NAMED tuple, in its
        # OWN try/except -- a github outage must never prevent the jira leg
        # (or vice versa) from running.
        for provider in MIRROR_PROVIDERS:
            try:
                outcome = mirror_task(project, task_id, provider=provider)
            except Exception as exc:  # pragma: no cover - never surface here
                log.warning("task mirror failed for %s (%s): %s",
                           task_id, provider, exc)
                continue
            if outcome.created:
                log.info("mirrored task %s -> %s (%s)", task_id, outcome.url, provider)
            elif outcome.attempted:
                log.info("task %s not mirrored via %s: %s",
                        task_id, provider, outcome.reason)

    threading.Thread(target=_run, name=f"task-mirror-{task_id[:8]}",
                     daemon=True).start()


def _on_task_status_changed(project: str, task, old_status: str) -> None:
    """The status observer TaskService fires. Never raises, never blocks.

    Only a transition INTO done is actionable today; every other move is
    ignored here rather than filtered downstream, so a rename or a priority
    bump can never reach a provider (task 0a9b511f, AC-8).
    """
    if not mirror_enabled():
        return
    task_id = getattr(task, "id", "")
    if not task_id:
        return
    if getattr(task, "status", "") != "done" or old_status == "done":
        return

    def _run() -> None:
        # FR-8/NFR-6: same per-provider isolation as the create leg.
        for provider in MIRROR_PROVIDERS:
            try:
                outcome = close_task(project, task_id, provider=provider)
            except Exception as exc:  # pragma: no cover - never surface here
                log.warning("task mirror close failed for %s (%s): %s",
                           task_id, provider, exc)
                continue
            if outcome.created:
                log.info("closed mirrored issue for task %s -> %s (%s)",
                        task_id, outcome.url, provider)
            elif outcome.attempted:
                log.info("task %s not closed via %s: %s",
                        task_id, provider, outcome.reason)

    threading.Thread(target=_run, name=f"task-mirror-close-{task_id[:8]}",
                     daemon=True).start()


def install() -> bool:
    """Register the create observer with TaskService. Idempotent.

    Called from production startup (main.py), which is the whole point: the
    recorded failure mode for this feature family is a slice whose tests INJECT
    the collaborator, so the suite is green while production wires nothing.
    ``status()`` below reports what THIS function actually did, so the claim
    "it is wired" is checkable instead of assumed.
    """
    from prism_service.services import task_service

    created = task_service.add_create_observer(_on_task_created)
    # BOTH legs, from the one call production already makes (main.py:413-414).
    # Registering only creation is how the mirror ended up able to open an
    # issue and never close one (task 0a9b511f).
    closed = task_service.add_status_observer(_on_task_status_changed)
    return bool(created or closed)


def status() -> dict:
    """Is the mirror actually wired, right now, in THIS process?

    Deliberately reports each link of the chain separately rather than one
    boolean: when a task does not reach GitHub, the answer to "which link is
    broken" has to be readable without attaching a debugger.
    """
    from prism_service.api.integrations import (
        _adapters, adapter_registration_errors)
    from prism_service.services import task_service

    scope = personal_scope()
    info: dict = {
        "enabled": mirror_enabled(),
        "env": MIRROR_ENV,
        "observer_installed": _on_task_created in task_service.create_observers(),
        # Reported SEPARATELY from the create leg, because they can be wired
        # independently and "the mirror is on" was never a fine enough answer
        # to "why did my done task not close its issue" (task 0a9b511f).
        "closure_observer_installed": (
            _on_task_status_changed in task_service.status_observers()),
        "adapters": sorted(_adapters),
        "adapter_errors": dict(adapter_registration_errors),
        "scope": scope or "",
        "sync_enabled": False,
        "tracking": [],
    }
    if scope:
        from prism_service.services.integration_store import (
            get_integration_store)
        from prism_service.services.sync_prefs import get_sync_preferences

        store = get_integration_store()
        info["sync_enabled"] = bool(get_sync_preferences().enabled(scope, "github"))
        info["tracking"] = [
            k.display_key or k.remote_id
            for c in store.list_connections(scope) if c.provider == "github"
            for k in store.list_containers(scope, c.id)]
    info["ready"] = bool(
        info["enabled"] and info["observer_installed"]
        and info["closure_observer_installed"]
        and "github" in info["adapters"] and info["sync_enabled"]
        and info["tracking"])
    return info
