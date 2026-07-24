"""RED scaffold — opt-in outbox + field-ownership reconciliation (task c16cb8e3).

Pins the not-yet-built IntegrationOutbox and IntegrationEventStore field-owner
policy: outbound is disabled by default, enabled per connection, durable across
restart, and loop-safe (echo suppression); an inbound update to a locally-owned
field records a conflict instead of overwriting.

Prism modules import INSIDE the tests so the file collects and fails at runtime
(red = rc 1) before the modules exist.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

WS_A = "workspace-a"
CONN = "conn-1"


def _outbox(tmp_path):
    from prism_service.services.integration_outbox import IntegrationOutbox

    return IntegrationOutbox(str(tmp_path / "integration_outbox.db"))


def _events(tmp_path):
    from prism_service.services.integration_events import IntegrationEventStore

    return IntegrationEventStore(str(tmp_path / "integration_events.db"))


# ── AC-5: outbound is opt-in and loop-safe ─────────────────────────────

def test_outbound_is_disabled_by_default(tmp_path):
    ob = _outbox(tmp_path)
    assert ob.is_enabled(WS_A, CONN) is False
    item = ob.enqueue(WS_A, CONN, "entity-1", "title", "new title")
    assert item is None
    assert ob.pending_items() == []


def test_enabled_outbound_enqueues_and_survives_restart(tmp_path):
    ob = _outbox(tmp_path)
    ob.enable_outbound(WS_A, CONN)
    assert ob.is_enabled(WS_A, CONN) is True
    item = ob.enqueue(WS_A, CONN, "entity-1", "title", "new title")
    assert item is not None and item.status == "pending"

    reopened = _outbox(tmp_path)  # restart
    pending = reopened.pending_items()
    assert [i.entity_id for i in pending] == ["entity-1"]
    assert reopened.is_enabled(WS_A, CONN) is True  # policy is durable too


def test_outbound_echo_does_not_loop(tmp_path):
    ob = _outbox(tmp_path)
    ob.enable_outbound(WS_A, CONN)
    item = ob.enqueue(WS_A, CONN, "entity-1", "title", "new title")
    ob.mark_sent(item.id, marker="marker-abc")

    # an inbound event carrying our own marker is an echo → suppressed
    assert ob.is_echo(WS_A, "marker-abc") is True
    assert ob.is_echo(WS_A, "some-other-marker") is False


# ── AC-4: field ownership recorded, not silently overwritten ───────────

def test_local_owned_field_update_records_conflict(tmp_path):
    ev = _events(tmp_path)
    # provider-owned field applies
    assert ev.apply_field_update(WS_A, "entity-1", "remote_status", "closed",
                                 owner="provider") == "applied"
    # locally-owned field is NOT overwritten — a conflict is recorded
    assert ev.apply_field_update(WS_A, "entity-1", "title", "remote title",
                                 owner="local") == "conflict"
    conflicts = ev.list_conflicts(WS_A)
    assert len(conflicts) == 1
    assert conflicts[0].field == "title"
    assert conflicts[0].incoming_value == "remote title"


def test_conflicts_survive_restart(tmp_path):
    ev = _events(tmp_path)
    ev.apply_field_update(WS_A, "entity-1", "title", "remote title", owner="local")
    reopened = _events(tmp_path)
    assert len(reopened.list_conflicts(WS_A)) == 1
