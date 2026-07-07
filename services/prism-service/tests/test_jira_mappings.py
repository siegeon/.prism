"""Per-PRISM-project -> Jira-project mapping store + outbound resolution.

Replaces the single global PRISM_JIRA_PROJECT_KEY env with a real mapping:

  Seam 1 (store)    — JiraMappingStore.set/get/list/delete round-trip over a
    real sqlite file (DATA_DIR-level, clones the UserService shape).
  Seam 2 (resolve)  — jira_sync.outbound_create resolves a task's Jira project
    from the mapping store (keyed by the task_svc's project_id) OVER the
    PRISM_JIRA_PROJECT_KEY env fallback; env still wins when unmapped.

All monkeypatched so no test ever hits real Jira.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _store(tmp_path, name="jira_mappings.db"):
    from prism_service.services.jira_mappings import JiraMappingStore
    return JiraMappingStore(str(tmp_path / name))


# ----------------------------------------------------------------------
# Seam 1 — set/get/list/delete round-trip
# ----------------------------------------------------------------------

def test_get_unmapped_returns_empty(tmp_path):
    assert _store(tmp_path).get("prism") == ""


def test_set_then_get_round_trips(tmp_path):
    s = _store(tmp_path)
    s.set("prism", "PLAT")
    assert s.get("prism") == "PLAT"
    # ...and a SEPARATE store over the same file reads it back (on-disk).
    assert _store(tmp_path).get("prism") == "PLAT"


def test_set_is_upsert(tmp_path):
    s = _store(tmp_path)
    s.set("prism", "PLAT")
    s.set("prism", "OPS")
    assert s.get("prism") == "OPS"
    assert len(s.list()) == 1  # rebind, not a duplicate row


def test_list_returns_all_mappings(tmp_path):
    s = _store(tmp_path)
    s.set("prism", "PLAT")
    s.set("devtools", "DEV")
    rows = s.list()
    assert {r["project_id"] for r in rows} == {"prism", "devtools"}
    by_pid = {r["project_id"]: r["jira_project_key"] for r in rows}
    assert by_pid == {"prism": "PLAT", "devtools": "DEV"}
    assert all(r.get("updated_at") for r in rows)


def test_delete_removes_mapping(tmp_path):
    s = _store(tmp_path)
    s.set("prism", "PLAT")
    assert s.delete("prism") is True
    assert s.get("prism") == ""
    assert s.list() == []
    # Idempotent — deleting a missing mapping reports False, never raises.
    assert s.delete("prism") is False


# ----------------------------------------------------------------------
# Seam 2 — outbound resolves the mapping OVER the env fallback
# ----------------------------------------------------------------------

def _tasksvc(tmp_path, project_id, name="tasks.db"):
    from prism_service.services.task_service import TaskService
    return TaskService(str(tmp_path / name), project_id=project_id)


def test_outbound_uses_mapping_over_env_fallback(tmp_path, monkeypatch):
    from prism_service.services import jira_client, jira_sync, jira_auth, jira_mappings
    monkeypatch.setattr(jira_auth, "is_authenticated", lambda: True)
    # A mapping store bound to THIS project; the env points somewhere else.
    store = _store(tmp_path)
    store.set("myproj", "MAP")
    monkeypatch.setattr(jira_mappings, "default_service", lambda: store)
    monkeypatch.setenv("PRISM_JIRA_PROJECT_KEY", "ENVKEY")
    seen = []
    monkeypatch.setattr(jira_client, "create_issue",
                        lambda pk, summary: seen.append(pk) or f"{pk}-1")
    jira_sync.reset_receipts()

    svc = _tasksvc(tmp_path, project_id="myproj")
    t = svc.create(title="mapped task")
    # The mapping (MAP) is used, NOT the env fallback (ENVKEY).
    assert seen == ["MAP"]
    assert t.jira_issue_key == "MAP-1"


def test_outbound_falls_back_to_env_when_unmapped(tmp_path, monkeypatch):
    from prism_service.services import jira_client, jira_sync, jira_auth, jira_mappings
    monkeypatch.setattr(jira_auth, "is_authenticated", lambda: True)
    store = _store(tmp_path)  # empty — no mapping for this project
    monkeypatch.setattr(jira_mappings, "default_service", lambda: store)
    monkeypatch.setenv("PRISM_JIRA_PROJECT_KEY", "ENVKEY")
    seen = []
    monkeypatch.setattr(jira_client, "create_issue",
                        lambda pk, summary: seen.append(pk) or f"{pk}-9")
    jira_sync.reset_receipts()

    svc = _tasksvc(tmp_path, project_id="unmapped-proj")
    t = svc.create(title="unmapped task")
    assert seen == ["ENVKEY"]  # env fallback used only when unmapped
    assert t.jira_issue_key == "ENVKEY-9"


# ----------------------------------------------------------------------
# Seam 3 — the /api/jira/mapping surface (list / set / delete)
# ----------------------------------------------------------------------

def test_mapping_api_round_trip(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import jira as jira_api
    from prism_service.services.jira_mappings import JiraMappingStore

    store = JiraMappingStore(str(tmp_path / "jira_mappings.db"))
    monkeypatch.setattr(jira_api.jira_mappings, "default_service", lambda: store)
    app = FastAPI()
    app.include_router(jira_api.router, prefix="/api/jira")
    client = TestClient(app)

    assert client.get("/api/jira/mappings").json()["mappings"] == []
    r = client.post("/api/jira/mapping",
                    json={"project_id": "prism", "jira_project_key": "PLAT"})
    assert r.status_code == 200, r.text
    assert store.get("prism") == "PLAT"
    assert any(m["project_id"] == "prism"
               for m in client.get("/api/jira/mappings").json()["mappings"])

    d = client.delete("/api/jira/mapping?project_id=prism")
    assert d.status_code == 200, d.text
    assert d.json()["deleted"] is True
    assert store.get("prism") == ""


def test_mapping_api_rejects_blank(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import jira as jira_api
    from prism_service.services.jira_mappings import JiraMappingStore

    store = JiraMappingStore(str(tmp_path / "jira_mappings.db"))
    monkeypatch.setattr(jira_api.jira_mappings, "default_service", lambda: store)
    app = FastAPI()
    app.include_router(jira_api.router, prefix="/api/jira")
    client = TestClient(app)

    assert client.post("/api/jira/mapping",
                       json={"project_id": "", "jira_project_key": "PLAT"}).status_code == 400
    assert client.request("DELETE", "/api/jira/mapping").status_code == 400
