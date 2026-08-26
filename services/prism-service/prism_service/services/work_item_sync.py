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


# Entity kinds an adapter may hand back that are NEVER their own local task.
# A pull request is a link target for an EXISTING task (github_work.
# link_pull_request matches its [task:xxxx] trailer), not new work of its
# own (task 0665c829); pull_page() still returns them so that linkage has
# data to match against.
NON_INTAKE_ENTITY_KINDS = frozenset({"pull_request"})

# Providers whose remote status reconciles into the local task on import
# (task 0a9b511f, widened by task 64ba4755 FR-7). GitHub reconciles `done`
# only. Jira's own read-mostly round trip is now built (FR-1..FR-6), so
# reconciling it is this slice's own deliberate, evidenced widening — not
# speculation on a provider nobody has connected, which is what the
# original github-only comment (superseded here, owner 2026-08-10) guarded
# against. Add a provider here only once its round trip is actually
# exercised, exactly as this one was.
STATUS_RECONCILE_PROVIDERS = frozenset({"github", "jira"})

# Board-wins name map (task 64ba4755, FR-7): statusCategory 'done' wins
# first (see _board_wins_status below); this is the FALLBACK for jira only,
# keyed on the raw board status name, casefolded. An unmapped name writes
# nothing — never guessed. github stays done-only, unaffected by this map.
_JIRA_BOARD_STATUS_MAP = {
    "blocked": "blocked",
    "to do": "pending", "open": "pending", "backlog": "pending",
    "reopened": "pending",
    "dev": "in_progress", "in progress": "in_progress",
    "code review": "in_progress", "in review": "in_progress",
    "qa": "in_progress", "validation": "in_progress",
}


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

    # get/update already exist on TaskService (task_service.py:444, :503) and
    # are used ONLY to backfill an already-mirrored row's description (task
    # 82223365) — never to weaken ensure_external_intake's own no-clobber
    # early return, which stays untouched.
    def get(self, task_id: str) -> object:
        ...

    def update(self, task_id: str, **kwargs: object) -> object:
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

        if entity_input.entity_kind in NON_INTAKE_ENTITY_KINDS:
            # The entity is upserted above (so link_pull_request has data to
            # match against later) but it never gets its own task. task_id_for
            # is deterministic, so this recomputes the SAME id a pre-fix sync
            # used to mistakenly create one (task 0665c829) -- retract it, but
            # ONLY while it is still untouched. A row a human already picked
            # up (status moved off pending, or a workflow_step is set) is left
            # exactly alone, the same no-clobber invariant
            # ensure_external_intake already keeps for a local edit.
            stale = self._intake.get(task_id)
            if (stale is not None and stale.status == "pending"
                    and not stale.workflow_step):
                self._intake.update(task_id, status="cancelled")
            return 0

        link = store.claim_import_link(workspace_id, entity.id, task_id)
        # claim_import_link's id is keyed on entity_id alone (INSERT OR
        # IGNORE), so when this entity already carries a link from an
        # earlier outbound push (push_task_creation), the insert is a no-op
        # and the returned link still points at that ORIGINAL task_id --
        # never the freshly computed deterministic one. Every downstream
        # write in this method must target THAT task, or an outbound-
        # mirrored entity gets a second, duplicate local task on its next
        # inbound pull (task 4fa13457).
        task_id = link.task_id
        title = entity_input.title or entity_input.display_key or entity_input.remote_id
        # Create a LOCAL pending intake, and a later pull never clobbers a
        # user-edited local row. Remote status reconciles ONE way only, into
        # `done`, attributed, via _reconcile_remote_status below (task
        # 0a9b511f, owner 2026-08-02) - it still never drives workflow_step or
        # gate_state, so a closed issue cannot manufacture a passed gate.
        # Record WHERE it came from, on the task itself. The deterministic id
        # already links the two, but a person reading the Work page could not
        # see it, and the push slice needs the counterpart to be legible rather
        # than recomputable-only (task 900a4fb9).
        origin = entity_input.display_key or entity_input.remote_id
        source = f"{container.display_key or container.remote_id} {origin}".strip()
        url = getattr(entity_input, "url", "") or ""
        # Kept ONLY for the pre-4db228ec stub backfill below, which still
        # matches on this exact string — a NEW import no longer writes it
        # into description (task fb7edc46): channel=provider + channel_ref
        # =url (set below) already carry the provenance, so the body alone
        # is the description now, and mirror_url/_mirror_entry read
        # channel_ref, not a description regex, for anything imported here.
        provenance = (f"Mirrored from {connection.provider} {source}."
                      + (f"\n{url}" if url else ""))
        body = (getattr(entity_input, "body", "") or "").strip()
        # The real issue body IS the description now; the mirror pointer
        # lives in channel/channel_ref, not appended prose (task fb7edc46,
        # superseding the task 4db228ec trailer this replaced). This only
        # ever runs on the FIRST import of a given task_id - the
        # ensure_external_intake call below is idempotent (a later pull
        # never clobbers a user-edited local row), so a re-sync neither
        # duplicates the body nor overwrites a hand-edited description.
        description = body
        pre_existing = self._intake.get(task_id)
        # Channel provenance (task b480eb15, retired trailer task fb7edc46):
        # the persisted channel is the provider and the ref is the entity's
        # own URL — that alone carries provenance for new imports now.
        self._intake.ensure_external_intake(
            task_id,
            title=title,
            description=description,
            tags=[connection.provider, "external"],
            channel=connection.provider,
            channel_ref=url,
        )
        # STE + lexicon ALIGN the freshly imported body (task 683e65eb,
        # epic df0eed4a). ensure_external_intake writes description with a
        # raw INSERT (task_service.py) and never runs the align pass, so a
        # first import would otherwise store the raw upstream text and skip
        # the ste_normalise history row entirely — the original text would
        # be lost with no `before` record of it. Route the CREATE path
        # through TaskService.update() once: it re-runs the SAME normaliser
        # every other task field already goes through, and its own
        # ste_before/after bookkeeping records the untouched original body
        # under `before` on the ste_normalise row. Only pre_existing is
        # None (a genuinely NEW row) ever reaches this — an existing row
        # stays covered by the no-clobber contract above, and must never be
        # rewritten here: a later remote edit must not clobber a
        # hand-edited local description (task 4db228ec AC-3,
        # test_human_edited_task_survives_a_resync).
        if pre_existing is None and body:
            self._intake.update(task_id, description=body)
        # Backfill task 82223365: a row imported BEFORE 4db228ec keeps its
        # bare mirror-pointer stub forever, because the ensure_external_intake
        # call above is a no-op once the row exists. Converge it exactly
        # once, and ONLY when the row's description is still that EXACT
        # stub (never a loose/contains match) - anything else, including
        # empty, a hand-edited note, or an already-backfilled description,
        # is left alone. Title is never touched here; PRISM stays the
        # record of the title, never GitHub.
        if (pre_existing is not None
                and pre_existing.description == provenance
                and body):
            self._intake.update(task_id, description=f"{body}\n\n{provenance}")
        # INBOUND STATUS RECONCILE (task 0a9b511f). Owner 2026-08-02: "when we
        # mark a task done then github should mark it done and such, both ways.
        # otherwise this sync to github feature does not do much."
        #
        # Narrow on purpose, and the narrowness IS the safety property:
        #   - only the done direction, never re-opening a locally finished task;
        #   - only status, so a hand-edited title or description is untouched
        #     and the no-clobber contract above still holds;
        #   - NEVER workflow_step or gate_state, so a closed issue can still
        #     not manufacture a passed gate. That was the real invariant inside
        #     the assertion this task retired, and it survives intact.
        #   - ATTRIBUTED, because an anonymous auto-done is indistinguishable
        #     from a human decision (the defect behind task 5025e97a).
        self._reconcile_remote_status(task_id, entity_input, connection.provider,
                                      origin)
        store.activate_link(workspace_id, link.id)
        store.append_receipt(
            workspace_id, run_id, entity.id,
            OUTCOME_CREATED if created else OUTCOME_UPDATED)
        return 1

    @staticmethod
    def _board_wins_status(provider: str, entity_input) -> Optional[str]:
        """The board-wins target status, or None to write nothing.

        statusCategory 'done' wins first for every reconciled provider;
        jira then falls back to its own raw-name board map (task 64ba4755,
        FR-7). github stays done-only, exactly as it always has.
        """
        if getattr(entity_input, "status_category", "") == "done":
            return "done"
        if provider != "jira":
            return None
        raw = (getattr(entity_input, "remote_status", "") or "").strip().casefold()
        return _JIRA_BOARD_STATUS_MAP.get(raw)

    def _reconcile_remote_status(self, task_id: str, entity_input,
                                 provider: str, origin: str) -> None:
        """Move a task to its board-wins status when its remote counterpart
        moves.

        Split out of _import_one so the ONE state transition this sync is
        allowed to make is readable in isolation. Returns silently on every
        path that must not act, rather than nesting the import in conditions.
        """
        if provider not in STATUS_RECONCILE_PROVIDERS:
            return
        target = self._board_wins_status(provider, entity_input)
        if target is None:
            return
        task = self._intake.get(task_id)
        if task is None:
            return
        if getattr(task, "workflow_step", ""):
            # A task a driver already picked up is never clobbered by
            # remote status, even when the mirror keeps seeing it move
            # (task 64ba4755, AC-8).
            return
        if getattr(task, "status", "") == target:
            return  # idempotent: a second identical pull writes nothing

        self._intake.update(task_id, status=target)
        record = getattr(self._intake, "record_history", None)
        if record is not None:
            action = "remote_closed" if target == "done" else "remote_status_synced"
            record(task_id, action,
                   details=(f"{provider} {origin} moved to {target} upstream; "
                            f"task synced by the mirror"),
                   actor=f"{provider}-mirror")


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

    # Resolve the link BELONGING TO the requested provider, never merely the
    # first one: a task mirrored to several providers (github AND jira) has
    # several active links, and taking active_links[0] made the jira leg
    # refuse on the github entity (live miss on PRIS-8, task 88a7da0b).
    entity = container = connection = None
    for link in active_links:
        cand = store.get_entity(workspace_id, link.entity_id)
        if cand is None:
            continue
        conn = store.get_connection(workspace_id, cand.connection_id)
        if conn is None or conn.provider != provider:
            continue
        cont = store.get_container(workspace_id, cand.container_id)
        if cont is None:
            continue
        entity, container, connection = cand, cont, conn
        break
    if entity is None:
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

    try:
        closed = adapter.close(connection, container, entity)
    except AdapterError as exc:
        # A provider-specific refusal (task 88a7da0b, FR-7: jira raises when
        # no done-category transition exists) is a normal, reportable
        # outcome here -- never an exception a caller must catch. The
        # message is already sanitized by the adapter (NFR-1).
        result.reason = str(exc)
        return result
    remote_updated = ""
    if isinstance(closed, dict):
        remote_updated = str(closed.get("updated_at") or "")
    marker = f"{entity.remote_id}:{remote_updated}"
    outbox.mark_sent(item.id, marker)

    result.eligible = True
    result.closed = True
    result.reason = "closed"
    return result


# ── push: the CREATE half (task 7cf6a2e5) ───────────────────────────────
#
# The mirror image of push_task_closure, one task_id per call, never a
# sweep. Owner decision 2026-07-29: only ACTIVE tasks (pending/in_progress/
# blocked) ever get an issue — done/cancelled history stays local, deferred
# not forgotten. Idempotence is structural: an existing active link refuses
# the call before any GitHub contact, so a re-run of the SAME task_id is a
# no-op and the deterministic (workspace, entity) key can never produce a
# second issue.

ACTIVE_STATUSES = frozenset({"pending", "in_progress", "blocked"})


@dataclass
class PushCreateResult:
    """What the create push either would do (``dry_run``) or did."""

    task_id: str
    provider: str = ""
    dry_run: bool = False
    eligible: bool = False
    created: bool = False
    reason: str = ""
    repo: str = ""
    issue: str = ""
    url: str = ""
    assignee: str = ""


def _provider_active_links(store, workspace_id: str, task_id: str, provider: str) -> list:
    """Active links for ``task_id`` whose entity's OWN connection matches
    ``provider`` (task 88a7da0b, FR-9). Reuses the same entity->connection
    resolution ``push_task_closure`` already does above, so a task linked to
    one provider stays eligible for another rather than being refused by an
    unrelated link."""
    matches = []
    for link in store.list_links(workspace_id, task_id=task_id):
        if link.state != "active":
            continue
        entity = store.get_entity(workspace_id, link.entity_id)
        if entity is None:
            continue
        connection = store.get_connection(workspace_id, entity.connection_id)
        if connection is not None and connection.provider == provider:
            matches.append(link)
    return matches


def push_task_creation(
    store,
    outbox,
    registry: dict,
    sync_enabled_fn,
    workspace_id: str,
    task_id: str,
    task_status: str,
    title: str,
    body: str = "",
    assignee: str = "",
    provider: str = "github",
    dry_run: bool = False,
) -> PushCreateResult:
    """Create ONE external issue mirroring ``task_id``, iff it is active,
    not already linked, and the connector's sync switch is on.

    ``assignee`` is taken VERBATIM from the caller — this function does not
    decide policy about who should be assigned. The owner's rule ("assigned
    to the connected account for the pre-existing backlog; unassigned for a
    new task until it starts") is a caller-side decision made once, at the
    call site, not a status-driven computation baked in here; passing an
    explicit ``assignee`` always wins, passing none means unassigned.
    """
    result = PushCreateResult(task_id=task_id, provider=provider,
                              dry_run=dry_run, assignee=assignee)

    if not sync_enabled_fn(workspace_id, provider):
        result.reason = f"syncing with {provider} is turned off"
        return result

    if task_status not in ACTIVE_STATUSES:
        result.reason = (
            f"task is not active (status={task_status!r}); history stays "
            "local (owner 2026-07-29)")
        return result

    # FR-9 (task 88a7da0b): PROVIDER-SCOPED, not "any active link" -- with
    # both github and jira connected at once, a github-linked task must
    # still be eligible for its own jira issue. The refusal string stays
    # byte-identical to before this change.
    active_links = _provider_active_links(store, workspace_id, task_id, provider)
    if active_links:
        result.reason = "task already has an active external link; not created again"
        return result

    connections = [c for c in store.list_connections(workspace_id)
                  if c.provider == provider]
    if not connections:
        result.reason = f"no {provider} repository is tracked yet"
        return result
    connection = connections[0]
    containers = store.list_containers(workspace_id, connection.id)
    if not containers:
        result.reason = f"no {provider} repository is tracked yet"
        return result
    container = containers[0]

    result.repo = container.remote_id or container.display_key

    if dry_run:
        result.eligible = True
        result.reason = "would create"
        return result

    adapter = registry.get(provider)
    if adapter is None or not hasattr(adapter, "create"):
        result.reason = f"no push-capable adapter registered for {provider}"
        return result

    # Same one user-facing switch as the closure path enables outbound
    # routing as a byproduct, rather than a second undocumented toggle.
    outbox.enable_outbound(workspace_id, connection.id)
    item = outbox.enqueue(workspace_id, connection.id, task_id, "create", title)
    if item is None:
        result.reason = "outbound is disabled for this connection, or this is an echo"
        return result

    try:
        entity_input = adapter.create(connection, container, title, body, assignee)
    except AdapterError as exc:
        # Same discipline as the closure leg above: a provider-specific
        # create failure (task 88a7da0b) is a normal, reportable outcome,
        # never an exception a caller must catch. Sanitized by the adapter
        # (NFR-1) before it ever reaches here.
        result.reason = str(exc)
        return result
    entity, _created = store.upsert_entity(
        workspace_id, connection.id, container.id, entity_input)
    marker = f"{entity_input.remote_id}:{entity_input.remote_updated_at}"
    outbox.mark_sent(item.id, marker)

    link = store.claim_import_link(workspace_id, entity.id, task_id)
    store.activate_link(workspace_id, link.id)

    result.eligible = True
    result.created = True
    result.reason = "created"
    result.issue = entity.display_key or entity.remote_id
    result.url = entity.url
    return result


@dataclass
class PushScanReport:
    """A pure classification of a candidate task set — never touches a
    store, an outbox, or a registry, so it structurally cannot reach GitHub.
    This is the dry-run VISIBILITY the oracle needs: proof the active-only
    filter and the idempotence check are both exercised, not merely assumed
    true of whatever query fed them in."""

    would_create: list = field(default_factory=list)
    skipped_done: list = field(default_factory=list)
    skipped_cancelled: list = field(default_factory=list)
    skipped_already_linked: list = field(default_factory=list)
    skipped_other_status: list = field(default_factory=list)


def scan_active_tasks(rows) -> PushScanReport:
    """Classify ``(task_id, status, has_active_link)`` rows. Read-only by
    construction: no store/outbox/registry parameter exists to reach
    GitHub with, so this can never become the unattended sweep this task's
    likely_misfire warns against — it can only report."""
    report = PushScanReport()
    for task_id, status, has_active_link in rows:
        if has_active_link:
            report.skipped_already_linked.append(task_id)
        elif status == "done":
            report.skipped_done.append(task_id)
        elif status == "cancelled":
            report.skipped_cancelled.append(task_id)
        elif status in ACTIVE_STATUSES:
            report.would_create.append(task_id)
        else:
            report.skipped_other_status.append(task_id)
    return report


# The sweep itself (task 02672417, GAP-A): the ONLY caller allowed to reach
# GitHub for more than one task at a time. Classifies with scan_active_tasks
# (no new eligibility logic), then pushes each would-create id through
# push_task_creation one at a time — every guard (sync switch, ACTIVE_
# STATUSES, active_links, adapter, outbox) already lives there. The caller
# (integrations_connect.set_sync) is what makes this edge-triggered
# (False->True only); ``limit`` is this function's own structural cap so a
# flip can never balloon the repo (AC-9), independent of the caller.
BACKLOG_SWEEP_LIMIT = 25


def push_active_backlog(
    store, outbox, registry, sync_enabled_fn,
    workspace_id: str, tasks, provider: str = "github",
    assignee: str = "", limit: int = BACKLOG_SWEEP_LIMIT,
) -> list:
    """Push pre-existing ACTIVE ``tasks`` (Task objects with .id/.status/
    .title/.description) toward ``provider``, capped at ``limit``. Returns
    the list of PushCreateResult, one per task actually attempted."""
    links = store.list_links(workspace_id)
    linked_ids = {l.task_id for l in links if l.state == "active"}
    rows = [(t.id, t.status, t.id in linked_ids) for t in tasks]
    report = scan_active_tasks(rows)

    by_id = {t.id: t for t in tasks}
    results = []
    for task_id in report.would_create[:limit]:
        task = by_id[task_id]
        results.append(push_task_creation(
            store, outbox, registry, sync_enabled_fn, workspace_id,
            task_id, task.status, title=task.title,
            body=task.description or "", assignee=assignee,
            provider=provider))
    return results
