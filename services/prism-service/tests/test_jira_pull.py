"""Deterministic Jira PULL-IN — TDD suite.

Jira is a PULL-IN SOURCE: pull_from_jira lists a project's issues and, for
EACH one, UPSERTS a PRISM task keyed on jira_issue_key (key equality only —
NO inference, NO fuzzy matching). Running twice creates no duplicates.

  * a NEW key  -> CREATE a task {source='jira', jira_issue_key=key, title=
    summary, status mapped from the Jira status category}.
  * a KNOWN key -> UPDATE the linked task's title from the summary.
  * disconnected / no project resolves -> no-op {pulled:0, reason}.

Plus: the additive `source` field round-trips create/update/serialize.

Everything monkeypatches jira_client.search_project so NO real Jira is hit.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _svc(tmp_path, project_id="proj", name="tasks.db"):
    from prism_service.services.task_service import TaskService
    return TaskService(str(tmp_path / name), project_id=project_id)


def _issue(key, summary, cat="new"):
    return {"key": key, "fields": {
        "summary": summary, "status": {"statusCategory": {"key": cat}}}}


# ----------------------------------------------------------------------
# Seam 1 — the `source` field round-trips create / update / serialize
# ----------------------------------------------------------------------

def test_task_source_defaults_to_prism_and_round_trips(tmp_path):
    import dataclasses

    svc = _svc(tmp_path)
    t = svc.create(title="native task")
    assert t.source == "prism"                       # DEFAULT
    # persisted + read back from a fresh instance (on-disk migration)
    assert _svc(tmp_path).get(t.id).source == "prism"

    j = svc.create(title="jira task", source="jira", jira_issue_key="PLAT-1")
    assert j.source == "jira"
    assert _svc(tmp_path).get(j.id).source == "jira"

    # update flips it; serializes as a plain field
    svc.update(j.id, source="github")
    assert svc.get(j.id).source == "github"
    assert dataclasses.asdict(svc.get(j.id))["source"] == "github"


# ----------------------------------------------------------------------
# Seam 2 — pulling issues CREATES tasks (source='jira', correct key)
# ----------------------------------------------------------------------

def test_pull_creates_tasks_from_issues(tmp_path, monkeypatch):
    from prism_service.services import jira_auth, jira_client, jira_sync
    monkeypatch.setattr(jira_auth, "is_authenticated", lambda: True)
    monkeypatch.setattr(jira_client, "search_project", lambda pk: [
        _issue("PLAT-1", "First issue", "new"),
        _issue("PLAT-2", "Second issue", "done"),
    ])
    jira_sync.reset_receipts()
    svc = _svc(tmp_path)

    res = jira_sync.pull_from_jira(svc, jira_project_key="PLAT")
    assert res["created"] == 2 and res["updated"] == 0 and res["pulled"] == 2

    tasks = {t.jira_issue_key: t for t in svc.list()}
    assert set(tasks) == {"PLAT-1", "PLAT-2"}
    assert all(t.source == "jira" for t in tasks.values())
    assert tasks["PLAT-1"].title == "First issue"
    # status category -> prism status
    assert tasks["PLAT-1"].status == "pending"       # new
    assert tasks["PLAT-2"].status == "done"          # done
    ins = [r for r in jira_sync.recent_receipts() if r["direction"] == "IN"]
    assert {r["jira_issue_key"] for r in ins} == {"PLAT-1", "PLAT-2"}


# ----------------------------------------------------------------------
# Seam 3 — pulling AGAIN is idempotent (upsert by key, no duplicates)
# ----------------------------------------------------------------------

def test_pull_twice_is_idempotent(tmp_path, monkeypatch):
    from prism_service.services import jira_auth, jira_client, jira_sync
    monkeypatch.setattr(jira_auth, "is_authenticated", lambda: True)
    monkeypatch.setattr(jira_client, "search_project", lambda pk: [
        _issue("PLAT-1", "First issue"),
        _issue("PLAT-2", "Second issue"),
    ])
    svc = _svc(tmp_path)

    jira_sync.pull_from_jira(svc, jira_project_key="PLAT")
    res2 = jira_sync.pull_from_jira(svc, jira_project_key="PLAT")
    # second pass touches the SAME rows — 0 created, still exactly 2 tasks
    assert res2["created"] == 0 and res2["updated"] == 2
    assert len(svc.list()) == 2


# ----------------------------------------------------------------------
# Seam 4 — a title change in Jira UPDATES the linked task (matched by key)
# ----------------------------------------------------------------------

def test_pull_updates_title_on_change(tmp_path, monkeypatch):
    from prism_service.services import jira_auth, jira_client, jira_sync
    monkeypatch.setattr(jira_auth, "is_authenticated", lambda: True)
    monkeypatch.setattr(jira_client, "search_project",
                        lambda pk: [_issue("PLAT-1", "original")])
    svc = _svc(tmp_path)
    jira_sync.pull_from_jira(svc, jira_project_key="PLAT")
    tid = svc.list()[0].id

    monkeypatch.setattr(jira_client, "search_project",
                        lambda pk: [_issue("PLAT-1", "renamed in jira")])
    jira_sync.pull_from_jira(svc, jira_project_key="PLAT")
    assert svc.get(tid).title == "renamed in jira"
    assert len(svc.list()) == 1                       # still no duplicate


# ----------------------------------------------------------------------
# Seam 5 — disconnected / unresolvable project -> no-op
# ----------------------------------------------------------------------

def test_pull_noop_when_disconnected(tmp_path, monkeypatch):
    from prism_service.services import jira_auth, jira_client, jira_sync
    monkeypatch.setattr(jira_auth, "is_authenticated", lambda: False)
    called = []
    monkeypatch.setattr(jira_client, "search_project",
                        lambda pk: called.append(1) or [_issue("X-1", "x")])
    svc = _svc(tmp_path)
    res = jira_sync.pull_from_jira(svc, jira_project_key="PLAT")
    assert res["pulled"] == 0 and res.get("reason")
    assert not called and svc.list() == []


def test_pull_noop_when_no_project_resolves(tmp_path, monkeypatch):
    from prism_service.services import jira_auth, jira_client, jira_mappings, jira_sync
    monkeypatch.setattr(jira_auth, "is_authenticated", lambda: True)
    monkeypatch.delenv("PRISM_JIRA_PROJECT_KEY", raising=False)
    from prism_service.services.jira_mappings import JiraMappingStore
    monkeypatch.setattr(jira_mappings, "default_service",
                        lambda: JiraMappingStore(str(tmp_path / "m.db")))
    called = []
    monkeypatch.setattr(jira_client, "search_project",
                        lambda pk: called.append(1) or [])
    svc = _svc(tmp_path, project_id="unmapped")
    res = jira_sync.pull_from_jira(svc)               # no key arg, none resolves
    assert res["pulled"] == 0 and res.get("reason")
    assert not called


# ----------------------------------------------------------------------
# Seam 6 — POST /api/jira/pull returns {created, updated, pulled, receipts}
# ----------------------------------------------------------------------

def test_pull_api_shape(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import jira as jira_api
    from prism_service.services import jira_auth, jira_client, jira_sync

    monkeypatch.setattr(jira_auth, "is_authenticated", lambda: True)
    monkeypatch.setattr(jira_client, "search_project",
                        lambda pk: [_issue("PLAT-1", "api issue")])
    jira_sync.reset_receipts()
    svc = _svc(tmp_path)
    # Route the API's project resolution at our isolated task_svc.
    monkeypatch.setattr(jira_api, "_pull_task_svc", lambda project: svc,
                        raising=False)

    app = FastAPI()
    app.include_router(jira_api.router, prefix="/api/jira")
    r = TestClient(app).post("/api/jira/pull", json={"project_key": "PLAT"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] == 1 and body["pulled"] == 1
    assert "receipts" in body
    assert svc.list()[0].source == "jira"
