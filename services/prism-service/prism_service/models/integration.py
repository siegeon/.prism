"""Provider-neutral external-work domain models (task fddfd75a).

The integration substrate every provider (GitHub, Jira, future) shares. An
external issue/PR/Jira-issue is a first-class linked record, never a column on
`Task`. Identity is the opaque tuple (workspace, connection, entity_kind,
stable remote id) — display keys, URLs, and titles are display-only and never
dedupe. Because integrations.db and tasks.db cannot share a transaction, the
row identifiers are deterministic UUIDv5 of the canonical tuple so a retry
after a crash between the two databases converges on the same entity/link/task.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field


# ── Canonical error / outcome codes (sanitized; never a raw provider msg) ──
ADAPTER_ERROR = "adapter_error"
RATE_LIMITED = "rate_limited"
PAGINATION_CYCLE = "pagination_cycle"
ERROR_CODES = frozenset({ADAPTER_ERROR, RATE_LIMITED, PAGINATION_CYCLE})

OUTCOME_CREATED = "created"
OUTCOME_UPDATED = "updated"
OUTCOME_FAILED = "failed"

# Local mode still needs a concrete, explicit scope so integrations.db rows are
# never unscoped — it is a real workspace id, not a team-mode bypass.
LOCAL_WORKSPACE_ID = "local"


# ── Deterministic identity ────────────────────────────────────────────────
# One fixed namespace; a per-kind prefix keeps entity/link/task/container ids
# distinct even when derived from the very same canonical tuple.
_NAMESPACE = uuid.UUID("6f8b5d2e-1a3c-5e7f-9b1d-2c4e6a8c0f11")


def _uuid5(prefix: str, *parts: str) -> str:
    name = prefix + "|" + "|".join(str(p) for p in parts)
    return str(uuid.uuid5(_NAMESPACE, name))


def connection_id_for(workspace_id: str, provider: str, remote_scope: str) -> str:
    return _uuid5("connection", workspace_id, provider, remote_scope)


def container_id_for(workspace_id: str, connection_id: str, kind: str, remote_id: str) -> str:
    return _uuid5("container", workspace_id, connection_id, kind, remote_id)


def entity_id_for(workspace_id: str, connection_id: str, entity_kind: str, remote_id: str) -> str:
    return _uuid5("entity", workspace_id, connection_id, entity_kind, remote_id)


def link_id_for(workspace_id: str, entity_id: str) -> str:
    # One partial-unique import link per external entity.
    return _uuid5("link", workspace_id, entity_id)


def task_id_for(workspace_id: str, connection_id: str, entity_kind: str, remote_id: str) -> str:
    # Same tuple as the entity, distinct namespace prefix → a distinct id.
    return _uuid5("task", workspace_id, connection_id, entity_kind, remote_id)


def message_id_for(workspace_id: str, connection_id: str, provider: str, remote_id: str) -> str:
    # A message's own namespace prefix. Deliberately never entity_id_for: a
    # message is not a WorkItemAdapter entity and must never collide with one
    # (task 33798164).
    return _uuid5("message", workspace_id, connection_id, provider, remote_id)


# ── Status normalization (raw remote status is always preserved too) ───────
_STATUS_MAP = {
    "open": "open", "opened": "open", "new": "open", "todo": "open",
    "reopened": "open", "backlog": "open",
    "in_progress": "in_progress", "in progress": "in_progress",
    "doing": "in_progress", "review": "in_progress", "in review": "in_progress",
    "closed": "done", "done": "done", "merged": "done", "resolved": "done",
    "completed": "done",
}


def normalize_status_category(raw_status: str) -> str:
    """Map a raw provider status onto open|in_progress|done|unknown.

    The raw value is stored separately. This feeds filtering, and since task
    0a9b511f a ``done`` category also completes the mirrored task on import
    (owner 2026-08-02, both directions). It still never enters the conductor
    STATE MACHINE: workflow_step and gate_state are never driven from remote,
    so a closed issue cannot manufacture a passed gate.
    """
    return _STATUS_MAP.get((raw_status or "").strip().casefold(), "unknown")


# ── Records ────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class IntegrationConnection:
    id: str
    workspace_id: str
    provider: str
    remote_scope: str
    display_name: str = ""
    created_at: str = ""


@dataclass(frozen=True)
class ExternalContainer:
    id: str
    workspace_id: str
    connection_id: str
    kind: str
    remote_id: str
    display_key: str = ""
    display_name: str = ""
    url: str = ""
    created_at: str = ""


@dataclass(frozen=True)
class ExternalEntity:
    id: str
    workspace_id: str
    connection_id: str
    container_id: str
    entity_kind: str
    remote_id: str
    display_key: str = ""
    title: str = ""
    body: str = ""
    url: str = ""
    remote_status: str = ""
    status_category: str = ""
    assignees: list[str] = field(default_factory=list)
    revision: str = ""
    remote_updated_at: str = ""
    last_seen_at: str = ""
    created_at: str = ""


@dataclass(frozen=True)
class ExternalMessage:
    """A provider-neutral inbound MESSAGE (mail, chat) — a sibling of
    ExternalEntity, never a member of it (task 33798164).

    A message has no status, no assignees, and no lifecycle to reconcile; it
    is content someone can read, not work someone tracks. It deliberately
    carries no path into the pull-orchestration/intake machinery in
    work_item_sync.py, so importing one can never mint a local task —
    repeating that for a second shape is exactly the 0a9b511f mistake this
    task exists to avoid.
    """

    id: str
    workspace_id: str
    connection_id: str
    provider: str
    remote_id: str
    thread_id: str = ""
    sender: str = ""
    subject: str = ""
    body: str = ""
    remote_created_at: str = ""
    last_seen_at: str = ""
    created_at: str = ""


@dataclass(frozen=True)
class ExternalMessageInput:
    """One normalized message a future mail/channel importer hands back.

    Sanitized, display/identity fields only — never a raw provider payload,
    credential, header, or response body (same discipline as
    ExternalEntityInput).
    """

    remote_id: str
    thread_id: str = ""
    sender: str = ""
    subject: str = ""
    body: str = ""
    remote_created_at: str = ""


@dataclass(frozen=True)
class WorkItemExternalLink:
    id: str
    workspace_id: str
    entity_id: str
    task_id: str
    link_type: str = "import"
    state: str = "provisioning"  # provisioning | active
    created_at: str = ""


@dataclass(frozen=True)
class SyncRun:
    id: str
    workspace_id: str
    connection_id: str
    container_id: str
    status: str = "running"  # running | succeeded | failed
    error_code: str = ""
    items_processed: int = 0
    started_at: str = ""
    finished_at: str = ""


@dataclass(frozen=True)
class SyncReceipt:
    id: str
    workspace_id: str
    run_id: str
    entity_id: str
    outcome: str
    error_code: str = ""
    created_at: str = ""


@dataclass(frozen=True)
class ExternalEntityInput:
    """One normalized work object handed back by an adapter's pull_page.

    Deliberately carries only sanitized, display/identity fields — never a raw
    provider payload, credential, header, or response body.
    """

    entity_kind: str
    remote_id: str
    display_key: str = ""
    title: str = ""
    body: str = ""
    url: str = ""
    remote_status: str = ""
    status_category: str = ""
    assignees: tuple[str, ...] = ()
    revision: str = ""
    remote_updated_at: str = ""
