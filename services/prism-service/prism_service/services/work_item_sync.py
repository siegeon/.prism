"""Generic pull orchestration + adapter contract (task fddfd75a).

Pull-only this slice: an injected, synchronous ``WorkItemAdapter`` hands back
normalized pages; the orchestrator upserts each external entity by exact opaque
identity, claims a deterministic import link, materializes exactly one local
``pending`` intake task, activates the link, and appends a sanitized receipt.
Cursor and page token are kept distinct; a repeated page token is a bounded
``pagination_cycle``; adapter failures become fixed codes and leave the prior
durable cursor untouched. No raw payload, credential, or exception string is
ever persisted — only canonical codes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable

from prism_service.models.integration import (
    ADAPTER_ERROR,
    ERROR_CODES,
    OUTCOME_CREATED,
    OUTCOME_UPDATED,
    PAGINATION_CYCLE,
    ExternalContainer,
    ExternalEntityInput,
    IntegrationConnection,
    SyncRun,
    task_id_for,
)


@dataclass
class PulledPage:
    """One normalized page from an adapter.

    ``next_page_token`` is EPHEMERAL (drives the in-memory paging loop);
    ``next_cursor`` is the DURABLE checkpoint persisted after the page fully
    imports. Keeping them distinct is what lets a resumed sync continue without
    replaying a token it has already consumed.
    """

    entities: list[ExternalEntityInput] = field(default_factory=list)
    next_page_token: Optional[str] = None
    next_cursor: Optional[str] = None


class AdapterError(Exception):
    """A provider adapter failure carrying a CANONICAL code.

    The message may contain provider detail for the adapter's own logging, but
    the orchestrator persists only ``code`` — never the message — so a secret
    embedded upstream can never reach the store.
    """

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code if code in ERROR_CODES else ADAPTER_ERROR


@runtime_checkable
class WorkItemAdapter(Protocol):
    """A provider adapter. Pull-only; no outbound mutation in this slice."""

    provider: str

    def pull_page(
        self,
        connection: IntegrationConnection,
        container: ExternalContainer,
        cursor: Optional[str],
        page_token: Optional[str],
    ) -> PulledPage:
        ...


class IntakeProtocol(Protocol):
    def ensure_external_intake(self, task_id: str, title: str, **kwargs) -> object:
        ...


class WorkItemSyncService:
    """Deterministic pull orchestration over an injected adapter registry."""

    def __init__(
        self,
        store,
        intake: IntakeProtocol,
        registry: Optional[dict] = None,
        max_pages: int = 50,
        max_items: int = 1000,
    ) -> None:
        self._store = store
        self._intake = intake
        self._registry: dict = dict(registry or {})
        self._max_pages = max_pages
        self._max_items = max_items

    def register(self, adapter: WorkItemAdapter) -> None:
        self._registry[adapter.provider] = adapter

    def adapter_for(self, provider: str):
        return self._registry.get(provider)

    def pull_container(
        self,
        workspace_id: str,
        connection: IntegrationConnection,
        container: ExternalContainer,
        stream: str = "default",
    ) -> SyncRun:
        store = self._store
        run = store.start_run(workspace_id, connection.id, container.id)
        adapter = self._registry.get(connection.provider)
        if adapter is None:
            return store.finish_run(
                workspace_id, run.id, "failed", error_code=ADAPTER_ERROR)

        cursor = store.get_cursor(workspace_id, connection.id, container.id, stream)
        page_token: Optional[str] = None
        seen_tokens: set = set()
        items = 0
        pages = 0

        while pages < self._max_pages:
            if page_token in seen_tokens:
                return store.finish_run(
                    workspace_id, run.id, "failed",
                    error_code=PAGINATION_CYCLE, items_processed=items)
            seen_tokens.add(page_token)

            try:
                page = adapter.pull_page(connection, container, cursor, page_token)
            except AdapterError as exc:
                # Persist ONLY the canonical code; leave the prior cursor intact.
                return store.finish_run(
                    workspace_id, run.id, "failed",
                    error_code=exc.code, items_processed=items)

            for entity_input in page.entities:
                if items >= self._max_items:
                    break
                items += self._import_one(
                    workspace_id, connection, container, entity_input, run.id)

            pages += 1
            # Advance the durable cursor only after a page fully imports.
            if page.next_cursor is not None:
                store.set_cursor(
                    workspace_id, connection.id, container.id, stream, page.next_cursor)
                cursor = page.next_cursor
            if page.next_page_token is None:
                break
            page_token = page.next_page_token

        return store.finish_run(
            workspace_id, run.id, "succeeded", items_processed=items)

    def _import_one(
        self, workspace_id, connection, container, entity_input, run_id,
    ) -> int:
        store = self._store
        entity, created = store.upsert_entity(
            workspace_id, connection.id, container.id, entity_input)
        task_id = task_id_for(
            workspace_id, connection.id, entity_input.entity_kind, entity_input.remote_id)
        link = store.claim_import_link(workspace_id, entity.id, task_id)
        title = entity_input.title or entity_input.display_key or entity_input.remote_id
        # Create only a LOCAL pending intake; remote status never enters the
        # conductor and a later pull never clobbers a user-edited local row.
        # Record WHERE it came from, on the task itself. The deterministic id
        # already links the two, but a person reading the Work page could not
        # see it, and the push slice needs the counterpart to be legible rather
        # than recomputable-only (task 900a4fb9).
        origin = entity_input.display_key or entity_input.remote_id
        source = f"{container.display_key or container.remote_id} {origin}".strip()
        url = getattr(entity_input, "url", "") or ""
        self._intake.ensure_external_intake(
            task_id,
            title=title,
            description=(f"Mirrored from {connection.provider} {source}."
                         + (f"\n{url}" if url else "")),
            tags=[connection.provider, "external"],
        )
        store.activate_link(workspace_id, link.id)
        store.append_receipt(
            workspace_id, run_id, entity.id,
            OUTCOME_CREATED if created else OUTCOME_UPDATED)
        return 1


# ── push: the walking skeleton for the OTHER direction (task ae67ed5c) ─────
#
# Deliberately the mirror image of pull_container's shape, and deliberately
# NOT a drain: this takes exactly one task_id per call, never a collection,
# and never touches ``outbox.pending_items()`` — the pre-declared misfire for
# this task is an unattended sweep that closes many real issues on first
# activation. A dry-run performs every lookup and none of the writes.


@dataclass
class PushResult:
    """What the push either would do (``dry_run``) or did."""

    task_id: str
    provider: str = ""
    dry_run: bool = False
    eligible: bool = False
    closed: bool = False
    reason: str = ""
    repo: str = ""
    issue: str = ""
    url: str = ""


def push_task_closure(
    store,
    outbox,
    registry: dict,
    sync_enabled_fn,
    workspace_id: str,
    task_id: str,
    task_is_done: bool,
    provider: str = "github",
    dry_run: bool = False,
) -> PushResult:
    """Close the ONE external issue mirroring ``task_id``, iff it is done,
    linked, and the connector's sync switch is on.

    ``sync_enabled_fn(workspace_id, provider) -> bool`` decides FIRST, exactly
    like the pull path (api/integrations_connect.py:312) — before any link,
    container or connection is resolved and before GitHub is contacted at all,
    so turning the switch off really stops the push. The write itself is
    routed through ``outbox.enqueue``/``mark_sent`` (not a direct call at the
    mutation site) so a later inbound pull recognizes the marker as our own
    echo rather than fighting itself.
    """
    result = PushResult(task_id=task_id, provider=provider, dry_run=dry_run)

    if not sync_enabled_fn(workspace_id, provider):
        result.reason = f"syncing with {provider} is turned off"
        return result

    if not task_is_done:
        result.reason = "task is not done; nothing to push"
        return result

    active_links = [l for l in store.list_links(workspace_id, task_id=task_id)
                    if l.state == "active"]
    if not active_links:
        result.reason = "task has no active external link; never pushed"
        return result
    link = active_links[0]

    entity = store.get_entity(workspace_id, link.entity_id)
    if entity is None:
        result.reason = "linked entity no longer exists"
        return result
    container = store.get_container(workspace_id, entity.container_id)
    connection = store.get_connection(workspace_id, entity.connection_id)
    if container is None or connection is None or connection.provider != provider:
        result.reason = f"linked entity is not a {provider} issue"
        return result

    result.repo = container.remote_id or container.display_key
    result.issue = entity.display_key or entity.remote_id
    result.url = entity.url

    if dry_run:
        result.eligible = True
        result.reason = "would close"
        return result

    adapter = registry.get(provider)
    if adapter is None or not hasattr(adapter, "close"):
        result.reason = f"no push-capable adapter registered for {provider}"
        return result

    # Enable the connection's outbound routing as a byproduct of the ONE
    # user-facing switch already checked above, rather than asking the
    # operator to manage a second, undocumented consent toggle.
    outbox.enable_outbound(workspace_id, connection.id)
    item = outbox.enqueue(workspace_id, connection.id, entity.id, "status", "closed")
    if item is None:
        result.reason = "outbound is disabled for this connection, or this is an echo"
        return result

    closed = adapter.close(connection, container, entity)
    remote_updated = ""
    if isinstance(closed, dict):
        remote_updated = str(closed.get("updated_at") or "")
    marker = f"{entity.remote_id}:{remote_updated}"
    outbox.mark_sent(item.id, marker)

    result.eligible = True
    result.closed = True
    result.reason = "closed"
    return result
