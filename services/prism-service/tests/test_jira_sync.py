"""Slice D — bidirectional task<->Jira sync — TDD red suite.

Pins the Slice D seams (all monkeypatched so no test ever hits real Jira):

  jira_client   — a stdlib-urllib client whose create_issue / get_issue /
    list_updated_since are module-level + MONKEYPATCHABLE.
  Outbound      — a mapped PRISM task (Jira connected + a project key)
    calls jira_client.create_issue and stores the returned key on
    task.jira_issue_key; disconnected => untouched (existing flows safe).
  Receipts      — every sync event records a {direction, task_id,
    jira_issue_key, ok, ts} receipt, readable back (+ over the API).
  Inbound       — poll_once upserts a mapped task from a changed issue and
    records an IN receipt.
  Poller gate   — start_jira_sync is a no-op (returns None, never loops or
    raises) when PRISM_JIRA_SYNC_INTERVAL<=0 — the DEFAULT.

ALL FAIL today: services/jira_client.py + services/jira_sync.py don't
exist, task_service has no outbound hook, and /api/jira/sync/receipts is
unrouted.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _svc(tmp_path, name="tasks.db"):
    from prism_service.services.task_service import TaskService
    return TaskService(str(tmp_path / name))


# ----------------------------------------------------------------------
# Seam 1 — jira_client exists + methods are monkeypatchable
# ----------------------------------------------------------------------

def test_jira_client_methods_exist():
    from prism_service.services import jira_client
    for m in ("create_issue", "get_issue", "list_updated_since"):
        assert callable(getattr(jira_client, m)), f"jira_client.{m} missing"


def test_jira_client_create_issue_is_monkeypatchable(monkeypatch):
    """The whole point: tests swap the network method so no real Jira is hit."""
    from prism_service.services import jira_client
    monkeypatch.setattr(jira_client, "create_issue", lambda pk, summary: "PLAT-99")
    assert jira_client.create_issue("PLAT", "anything") == "PLAT-99"


# ----------------------------------------------------------------------
# Seam 2 — outbound: a mapped task creates a Jira issue + stores the key
# ----------------------------------------------------------------------

def test_outbound_create_calls_client_and_stores_key(tmp_path, monkeypatch):
    from prism_service.services import jira_client, jira_sync, jira_auth
    monkeypatch.setattr(jira_auth, "is_authenticated", lambda: True)
    calls = []
    monkeypatch.setattr(jira_client, "create_issue",
                        lambda pk, summary: calls.append((pk, summary)) or "PLAT-7")
    jira_sync.reset_receipts()
    svc = _svc(tmp_path)
    t = svc.create(title="Do a thing")
    key = jira_sync.outbound_create(svc, t, project_key="PLAT")
    assert key == "PLAT-7"
    assert calls == [("PLAT", "Do a thing")]
    # Stored on the object AND persisted to the row (fresh instance reads it).
    assert t.jira_issue_key == "PLAT-7"
    assert _svc(tmp_path).get(t.id).jira_issue_key == "PLAT-7"


def test_outbound_noop_when_disconnected(tmp_path, monkeypatch):
    from prism_service.services import jira_client, jira_sync, jira_auth
    monkeypatch.setattr(jira_auth, "is_authenticated", lambda: False)
    called = []
    monkeypatch.setattr(jira_client, "create_issue",
                        lambda *a, **k: called.append(1) or "X-1")
    svc = _svc(tmp_path)
    t = svc.create(title="x")
    assert jira_sync.outbound_create(svc, t, project_key="PLAT") is None
    assert not called
    assert t.jira_issue_key == ""


# ----------------------------------------------------------------------
# Seam 3 — the create()/update() hook fires only when Jira is connected
# ----------------------------------------------------------------------

def test_task_create_hook_syncs_when_connected(tmp_path, monkeypatch):
    from prism_service.services import jira_client, jira_sync, jira_auth
    monkeypatch.setattr(jira_auth, "is_authenticated", lambda: True)
    monkeypatch.setenv("PRISM_JIRA_PROJECT_KEY", "PLAT")
    monkeypatch.setattr(jira_client, "create_issue", lambda pk, summary: f"{pk}-100")
    jira_sync.reset_receipts()
    t = _svc(tmp_path).create(title="hook task")
    assert t.jira_issue_key == "PLAT-100"
    outs = [r for r in jira_sync.recent_receipts() if r["direction"] == "OUT"]
    assert any(r["ok"] and r["jira_issue_key"] == "PLAT-100" for r in outs)


def test_task_create_hook_untouched_when_disconnected(tmp_path, monkeypatch):
    """Existing task flow is byte-for-byte unaffected when Jira is off."""
    from prism_service.services import jira_client, jira_auth
    monkeypatch.setattr(jira_auth, "is_authenticated", lambda: False)
    monkeypatch.setenv("PRISM_JIRA_PROJECT_KEY", "PLAT")
    called = []
    monkeypatch.setattr(jira_client, "create_issue",
                        lambda *a, **k: called.append(1) or "X-1")
    t = _svc(tmp_path).create(title="offline task")
    assert t.jira_issue_key == ""
    assert not called


# ----------------------------------------------------------------------
# Seam 4 — sync receipts are recorded per event and readable back
# ----------------------------------------------------------------------

def test_receipts_record_and_read_back():
    from prism_service.services import jira_sync
    jira_sync.reset_receipts()
    jira_sync.record_receipt("OUT", "task-1", "PLAT-1", True)
    jira_sync.record_receipt("IN", "task-2", "PLAT-2", False)
    rows = jira_sync.recent_receipts()
    assert len(rows) == 2
    assert {r["direction"] for r in rows} == {"IN", "OUT"}
    assert {r["jira_issue_key"] for r in rows} == {"PLAT-1", "PLAT-2"}
    by_key = {r["jira_issue_key"]: r for r in rows}
    assert by_key["PLAT-1"]["ok"] is True and by_key["PLAT-2"]["ok"] is False
    assert all(r.get("ts") for r in rows)  # every receipt is timestamped


# ----------------------------------------------------------------------
# Seam 5 — inbound: poll_once upserts a mapped task + records an IN receipt
# ----------------------------------------------------------------------

def test_poll_once_reconciles_changed_issue_onto_mapped_task(tmp_path, monkeypatch):
    from prism_service.services import jira_client, jira_sync
    svc = _svc(tmp_path)
    t = svc.create(title="orig title", jira_issue_key="PLAT-5")
    monkeypatch.setattr(
        jira_client, "list_updated_since",
        lambda ts: [{"key": "PLAT-5", "fields": {"summary": "updated title"}}],
    )
    jira_sync.reset_receipts()
    n = jira_sync.poll_once(svc, since_ts=0)
    assert n >= 1
    assert svc.get(t.id).title == "updated title"
    ins = [r for r in jira_sync.recent_receipts() if r["direction"] == "IN"]
    assert any(r["jira_issue_key"] == "PLAT-5" and r["ok"] for r in ins)


# ----------------------------------------------------------------------
# Seam 6 — the poller is OFF by default and a no-op when interval<=0
# ----------------------------------------------------------------------

def test_start_jira_sync_disabled_by_default(monkeypatch):
    """No env set => default interval 0 => start_jira_sync is a no-op (None),
    never loops, never raises. The daemon must boot with sync OFF."""
    from prism_service.services import jira_sync
    monkeypatch.delenv("PRISM_JIRA_SYNC_INTERVAL", raising=False)
    assert jira_sync.start_jira_sync() is None


def test_start_jira_sync_zero_interval_is_noop(monkeypatch):
    from prism_service.services import jira_sync
    monkeypatch.setenv("PRISM_JIRA_SYNC_INTERVAL", "0")
    assert jira_sync.start_jira_sync() is None


def test_start_jira_sync_negative_interval_is_noop(monkeypatch):
    from prism_service.services import jira_sync
    monkeypatch.setenv("PRISM_JIRA_SYNC_INTERVAL", "-5")
    assert jira_sync.start_jira_sync() is None


# ----------------------------------------------------------------------
# Seam 7 — GET /api/jira/sync/receipts surfaces recent receipts (no secrets)
# ----------------------------------------------------------------------

def test_sync_receipts_route_returns_recent():
    from fastapi.testclient import TestClient
    from prism_service.main import app
    from prism_service.services import jira_sync
    jira_sync.reset_receipts()
    jira_sync.record_receipt("OUT", "task-9", "PLAT-9", True)
    resp = TestClient(app).get("/api/jira/sync/receipts")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "receipts" in body
    assert any(r["jira_issue_key"] == "PLAT-9" for r in body["receipts"])
