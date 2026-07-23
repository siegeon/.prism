"""RED scaffold — provider-neutral external-work store (task fddfd75a).

Pins the workspace-scoped integration substrate every provider shares:
IntegrationStore (connections/containers/entities/links/runs/receipts/
cursors), the UUIDv5 canonical identity, and TaskService.ensure_external_intake.

Identity is (workspace, connection, entity_kind, opaque remote ID) — display
keys never dedupe. Imports create a local `pending` intake task and never
clobber a user-edited local row. No token/secret column touches disk.

Everything imports the not-yet-built modules INSIDE the test bodies so the
file COLLECTS and each assertion fails at runtime (red = rc 1), rather than
erroring at collection (rc 2) before the implementation exists.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

WS_A = "workspace-a"
WS_B = "workspace-b"


def _store(tmp_path, name="integrations.db"):
    from prism_service.services.integration_store import IntegrationStore

    return IntegrationStore(str(tmp_path / name))


def _entity_input(remote_id, *, kind="issue", display_key="", title="",
                  remote_status="open", status_category="open"):
    from prism_service.models.integration import ExternalEntityInput

    return ExternalEntityInput(
        entity_kind=kind,
        remote_id=remote_id,
        display_key=display_key,
        title=title,
        remote_status=remote_status,
        status_category=status_category,
    )


# ── AC-1: identity is the opaque tuple; display keys never dedupe ───────

def test_same_display_key_across_two_connections_stays_distinct(tmp_path):
    store = _store(tmp_path)
    c1 = store.ensure_connection(WS_A, "github", "install-1", "acme")
    c2 = store.ensure_connection(WS_A, "github", "install-2", "beta")
    assert c1.id != c2.id

    k1 = store.ensure_container(WS_A, c1.id, "repository", "repo-1", display_key="acme/app")
    k2 = store.ensure_container(WS_A, c2.id, "repository", "repo-2", display_key="beta/app")

    # Same display_key "#1" under two different connections — must NOT collide.
    e1, _ = store.upsert_entity(WS_A, c1.id, k1.id, _entity_input("gh-100", display_key="#1"))
    e2, _ = store.upsert_entity(WS_A, c2.id, k2.id, _entity_input("gh-200", display_key="#1"))
    assert e1.id != e2.id
    assert store.get_entity(WS_A, e1.id).display_key == "#1"
    assert store.get_entity(WS_A, e2.id).display_key == "#1"
    assert len(store.list_entities(WS_A)) == 2


def test_repeated_exact_remote_id_upsert_updates_one_entity(tmp_path):
    store = _store(tmp_path)
    c = store.ensure_connection(WS_A, "github", "install-1")
    k = store.ensure_container(WS_A, c.id, "repository", "repo-1")

    e1, created1 = store.upsert_entity(WS_A, c.id, k.id, _entity_input("gh-1", title="first"))
    e2, created2 = store.upsert_entity(WS_A, c.id, k.id, _entity_input("gh-1", title="second"))
    assert created1 is True and created2 is False
    assert e1.id == e2.id
    assert store.get_entity(WS_A, e1.id).title == "second"
    assert len(store.list_entities(WS_A)) == 1


def test_display_key_collision_within_one_connection_is_two_entities(tmp_path):
    store = _store(tmp_path)
    c = store.ensure_connection(WS_A, "github", "install-1")
    k = store.ensure_container(WS_A, c.id, "repository", "repo-1")
    a, _ = store.upsert_entity(WS_A, c.id, k.id, _entity_input("gh-1", display_key="dup"))
    b, _ = store.upsert_entity(WS_A, c.id, k.id, _entity_input("gh-2", display_key="dup"))
    assert a.id != b.id


# ── AC-6: identity is deterministic UUIDv5 of the canonical tuple ───────

def test_identity_is_deterministic_uuid5(tmp_path):
    from prism_service.models import integration as integ

    a = integ.entity_id_for(WS_A, "conn-1", "issue", "gh-1")
    b = integ.entity_id_for(WS_A, "conn-1", "issue", "gh-1")
    assert a == b  # stable
    assert uuid.UUID(a).version == 5
    # Different tuple element => different id.
    assert integ.entity_id_for(WS_A, "conn-1", "issue", "gh-2") != a
    assert integ.entity_id_for(WS_B, "conn-1", "issue", "gh-1") != a
    # task id is derived from the same tuple but is a DISTINCT namespace.
    assert integ.task_id_for(WS_A, "conn-1", "issue", "gh-1") != a
    assert uuid.UUID(integ.task_id_for(WS_A, "conn-1", "issue", "gh-1")).version == 5


# ── AC-2: intake creates ONE pending task, never clobbers local edits ───

def test_ensure_external_intake_is_idempotent_and_pending(tmp_path):
    from prism_service.services.task_service import TaskService
    from prism_service.models import integration as integ

    svc = TaskService(str(tmp_path / "tasks.db"))
    tid = integ.task_id_for(WS_A, "conn-1", "issue", "gh-1")

    t1 = svc.ensure_external_intake(tid, title="Imported issue", tags=["github"])
    t2 = svc.ensure_external_intake(tid, title="Imported issue", tags=["github"])
    assert t1.id == t2.id == tid
    assert t1.status == "pending"
    assert t1.workflow_step == ""
    assert t1.gate_state == "none"
    # exactly one row
    assert len([t for t in svc.list() if t.id == tid]) == 1


def test_later_intake_never_clobbers_a_user_edit(tmp_path):
    from prism_service.services.task_service import TaskService
    from prism_service.models import integration as integ

    svc = TaskService(str(tmp_path / "tasks.db"))
    tid = integ.task_id_for(WS_A, "conn-1", "issue", "gh-1")
    svc.ensure_external_intake(tid, title="Imported issue")
    # user renames + advances the local task
    svc.update(tid, title="My triaged title", status="in_progress", priority=7)
    # a later pull re-imports the same entity with a changed remote title
    again = svc.ensure_external_intake(tid, title="Remote changed the title")
    assert again.title == "My triaged title"
    assert again.status == "in_progress"
    assert again.priority == 7


# ── AC-3: runs / receipts / cursors are durable across reconstruction ───

def test_run_receipt_cursor_survive_store_reconstruction(tmp_path):
    store = _store(tmp_path)
    c = store.ensure_connection(WS_A, "github", "install-1")
    k = store.ensure_container(WS_A, c.id, "repository", "repo-1")
    e, _ = store.upsert_entity(WS_A, c.id, k.id, _entity_input("gh-1"))
    run = store.start_run(WS_A, c.id, k.id)
    store.append_receipt(WS_A, run.id, e.id, "created")
    store.finish_run(WS_A, run.id, "succeeded", items_processed=1)
    store.set_cursor(WS_A, c.id, k.id, "default", "cursor-v1")

    reopened = _store(tmp_path)  # same file, fresh object/connection
    got = reopened.get_run(WS_A, run.id)
    assert got is not None and got.status == "succeeded" and got.items_processed == 1
    assert [r.outcome for r in reopened.list_receipts(WS_A, run_id=run.id)] == ["created"]
    assert reopened.get_cursor(WS_A, c.id, k.id, "default") == "cursor-v1"


def test_failed_run_keeps_prior_cursor_and_canonical_code(tmp_path):
    store = _store(tmp_path)
    c = store.ensure_connection(WS_A, "github", "install-1")
    k = store.ensure_container(WS_A, c.id, "repository", "repo-1")
    store.set_cursor(WS_A, c.id, k.id, "default", "cursor-v1")
    run = store.start_run(WS_A, c.id, k.id)
    store.finish_run(WS_A, run.id, "failed", error_code="rate_limited")
    assert store.get_cursor(WS_A, c.id, k.id, "default") == "cursor-v1"
    got = store.get_run(WS_A, run.id)
    assert got.status == "failed" and got.error_code == "rate_limited"


# ── AC-5: every lookup is workspace-scoped (A cannot read B) ────────────

def test_all_reads_are_workspace_scoped(tmp_path):
    store = _store(tmp_path)
    c = store.ensure_connection(WS_A, "github", "install-1")
    k = store.ensure_container(WS_A, c.id, "repository", "repo-1")
    e, _ = store.upsert_entity(WS_A, c.id, k.id, _entity_input("gh-1"))

    # Workspace B cannot see A's rows even with the exact ids.
    assert store.get_connection(WS_B, c.id) is None
    assert store.get_container(WS_B, k.id) is None
    assert store.get_entity(WS_B, e.id) is None
    assert store.list_connections(WS_B) == []
    assert store.list_containers(WS_B) == []
    assert store.list_entities(WS_B) == []


# ── AC-4: no secret/token/payload column ever touches disk ──────────────

def test_no_secret_or_raw_payload_column_on_disk(tmp_path):
    store = _store(tmp_path)
    store.ensure_connection(WS_A, "github", "install-1", "acme")
    raw = sqlite3.connect(str(tmp_path / "integrations.db"))
    try:
        tables = [r[0] for r in raw.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        for table in tables:
            cols = [r[1].lower() for r in raw.execute(f"PRAGMA table_info({table})")]
            for banned in ("token", "secret", "password", "credential",
                           "access_token", "refresh_token", "payload", "raw"):
                assert not any(banned in col for col in cols), (
                    f"{table}.{cols} exposes a {banned!r} column — provider "
                    "secrets/payloads must never be persisted by this store")
    finally:
        raw.close()
