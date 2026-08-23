"""dashboard.py opens ONE sqlite connection per DB per request, not one per
query (task: found live auditing a hung /api/dashboard/activity call on a
slow disk -- the endpoint was opening ~18 short-lived connections across 3
DBs for a single response, each its own open/query/close round trip).

Isolation mirrors test_projects_task_counts.py: tmp_path + monkeypatched
PROJECTS_DIR, tables seeded directly since dashboard.py reads raw SQLite.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from prism_service import config, project_context
from prism_service.api import dashboard as dashboard_api
from prism_service.services import sqlite_db


@pytest.fixture
def app(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    monkeypatch.setattr(config, "PROJECTS_DIR", root)
    project_context._contexts.clear()

    pdir = config.project_data_dir("acme")

    tasks_db = pdir / "tasks.db"
    c = sqlite3.connect(str(tasks_db))
    c.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, "
              "created_at TEXT, completed_at TEXT DEFAULT '', "
              "gate_state TEXT DEFAULT 'none')")
    c.execute("CREATE TABLE task_history (task_id TEXT, action TEXT, timestamp TEXT)")
    c.execute("INSERT INTO tasks VALUES ('t1', 'done', '2026-08-20T00:00:00Z', "
              "'2026-08-21T00:00:00Z', 'passed')")
    c.execute("INSERT INTO task_history VALUES ('t1', 'created', '2026-08-20T00:00:00Z')")
    c.commit(); c.close()

    brain_db = pdir / "brain.db"
    c = sqlite3.connect(str(brain_db))
    c.execute("CREATE TABLE docs (id TEXT PRIMARY KEY, indexed_at TEXT)")
    c.execute("CREATE TABLE searches (ts TEXT, query TEXT, n_results INT, latency_ms INT)")
    c.execute("INSERT INTO searches VALUES ('2026-08-20T00:00:00Z', 'q', 3, 42)")
    c.commit(); c.close()

    scores_db = pdir / "scores.db"
    c = sqlite3.connect(str(scores_db))
    c.execute("CREATE TABLE session_outcomes (timestamp TEXT, tokens_used INT)")
    c.execute("INSERT INTO session_outcomes VALUES ('2026-08-20T00:00:00Z', 100)")
    c.commit(); c.close()

    api = FastAPI()
    api.include_router(dashboard_api.router, prefix="/api/dashboard")
    with TestClient(api) as client:
        yield client


def _count_connections(monkeypatch):
    """Wrap sqlite_db.connect to count real opens, return the counter list."""
    calls = []
    real_connect = sqlite_db.connect

    def wrapped(path, **kw):
        calls.append(path)
        return real_connect(path, **kw)

    monkeypatch.setattr(dashboard_api.sqlite_db, "connect", wrapped)
    return calls


def test_activity_opens_exactly_one_connection_per_db_not_per_query(app, monkeypatch):
    calls = _count_connections(monkeypatch)
    r = app.get("/api/dashboard/activity?project=acme&days=14")
    assert r.status_code == 200, r.text
    # 3 DBs read (brain, tasks, scores) -- NOT ~18, one per _count/_rows call
    # the old per-query-connection version would have opened.
    assert len(calls) == 3, (
        f"expected exactly 3 connections (brain/tasks/scores), got {len(calls)}: {calls}")


def test_state_route_reuses_one_dashboard_connection_per_db(app, monkeypatch):
    r = app.get("/api/dashboard/state?project=acme")
    assert r.status_code == 200, r.text
    calls = _count_connections(monkeypatch)
    # A second call, with a fresh counter, isolates dashboard.py's OWN
    # opens from get_project()'s one-time lazy bootstrap on the first
    # call (mulch/recall_log.db, etc.) -- the thing under test is that
    # _count/_rows/_bucket no longer each open their own connection.
    r = app.get("/api/dashboard/state?project=acme")
    assert r.status_code == 200, r.text
    by_db = {}
    for path in calls:
        by_db[path] = by_db.get(path, 0) + 1
    assert all(n == 1 for n in by_db.values()), (
        f"each DB must be opened exactly once per request, not once per "
        f"query: {by_db}")


def test_activity_still_returns_correct_values_after_the_refactor(app):
    r = app.get("/api/dashboard/activity?project=acme&days=14")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["queries"]["total"] == 1
    assert body["queries"]["recent"][0]["q"] == "q"
    assert body["flow"]["gate_passed"] == 1
    assert body["flow"]["gate_failed"] == 0
    assert body["tokens"]["total"] == 100


def test_state_still_returns_correct_values_after_the_refactor(app):
    r = app.get("/api/dashboard/state?project=acme")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kpis"]["brain_docs"] == 0
    assert body["kpis"]["tasks_active"] == 0
    # graph.db/mulch.db absent in this fixture -- must degrade to 0, not raise
    assert body["kpis"]["entities"] == 0
    assert body["kpis"]["memories"] == 0
