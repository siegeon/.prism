"""Task<->session association (LL task a1bed6bb) — red scaffold.

Pins the USER-FACING seams of activating the dormant `task_sessions`
table, end-to-end through the real dispatcher / API route / source
surfaces — not just unit contracts:

  * FORCED WRITER — task_link_session reachable through the MCP
    DISPATCHER (handle_tool), registered in the task_* family +
    profile, upserting a row that SURVIVES across a separate call.
  * READER — sessions_for_task(task_id) JOINs session_outcomes and
    returns the metric columns.
  * CONDUCTOR WRITER — advance_task stamps a task_sessions row from
    its carried task_id + session.
  * API — GET /api/tasks/{id} returns a `sessions` field on the
    existing route (no parallel route).
  * UI — TaskDetailPage source renders "Sessions (N)" + an explicit
    empty state, consuming the new field (no raw <pre>JSON).

These FAIL today: task_sessions is schema-only (no writer/reader/verb/
field/UI). Source-structure asserts mirror tests/unit/test_ui_learning.py
(pages verified via the running deploy, helpers/contracts in Python).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_WEB_SRC = _SERVICE_ROOT / "prism_service" / "web" / "src"


def _isolated_project(tmp_path, pid="test-task-sessions"):
    from prism_service import config as cfg
    original = cfg.PROJECTS_DIR
    cfg.PROJECTS_DIR = tmp_path / "projects"
    cfg.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    from prism_service import project_context as pc
    pc._contexts.clear()
    yield pid
    cfg.PROJECTS_DIR = original
    pc._contexts.clear()


@pytest.fixture
def project(tmp_path):
    yield from _isolated_project(tmp_path)


def _call(tool_name, arguments=None, project_id="test-task-sessions"):
    from prism_service.mcp.tools import handle_tool
    return asyncio.run(handle_tool(tool_name, arguments or {}, project_id=project_id))


def _text(result):
    assert len(result) == 1
    return result[0].text


# ----------------------------------------------------------------------
# FORCED WRITER — task_link_session in the task_* family + dispatcher
# ----------------------------------------------------------------------


def test_task_link_session_registered_in_task_family():
    """New verb is in TOOLS (single-route convention) and in the
    interactive profile alongside the other task_* verbs."""
    from prism_service.mcp.tools import TOOLS, tools_for_profile

    names = {t.name for t in TOOLS}
    assert "task_link_session" in names, "task_link_session not registered in TOOLS"

    interactive = {t.name for t in tools_for_profile("interactive")}
    assert "task_link_session" in interactive, (
        "task_link_session missing from interactive profile — "
        "must ride the task_* family, not a parallel route"
    )


def test_task_link_session_upsert_survives_separate_call(project, tmp_path):
    """Force-link via the DISPATCHER, then read the row back through a
    SEPARATE dispatcher call to a different verb. A row that only lived
    in one in-memory instance would not survive this."""
    from prism_service.project_context import get_project

    t = json.loads(_text(_call("task_create", {"title": "wire sessions"})))
    tid = t["id"]

    linked = json.loads(_text(_call("task_link_session", {
        "task_id": tid, "session_id": "S-forced",
    })))
    assert linked.get("ok") is True or linked.get("task_id") == tid

    # Persisted to the project's scores.db (not a throwaway handle).
    ctx = get_project(project)
    conn = sqlite3.connect(str(ctx._data_dir / "scores.db"))
    row = conn.execute(
        "SELECT task_id, session_id, started_at FROM task_sessions "
        "WHERE task_id=? AND session_id=?",
        (tid, "S-forced"),
    ).fetchone()
    conn.close()
    assert row is not None, "task_link_session did not upsert a task_sessions row"
    assert row[2], "started_at must be stamped (defined semantics)"


# ----------------------------------------------------------------------
# READER — sessions_for_task JOINs session_outcomes metrics
# ----------------------------------------------------------------------


def test_sessions_for_task_joins_outcome_metrics(project):
    """sessions_for_task(task_id) returns [{session_id, started_at,
    ended_at, duration_s, tokens_used, files_read, files_modified,
    skills_invoked}] — the row JOINed against session_outcomes."""
    from prism_service.project_context import get_project

    t = json.loads(_text(_call("task_create", {"title": "reader task"})))
    tid = t["id"]

    # Record session metrics, then force the association.
    _call("record_session_outcome", {
        "session_id": "S-read", "duration_s": 120, "tokens_used": 4096,
        "files_read": 7, "files_modified": 3, "skills_invoked": 2,
    })
    _call("task_link_session", {"task_id": tid, "session_id": "S-read"})

    ctx = get_project(project)
    svc = ctx.task_svc
    assert hasattr(svc, "sessions_for_task"), (
        "TaskService.sessions_for_task accessor missing"
    )
    sessions = svc.sessions_for_task(tid)
    assert isinstance(sessions, list) and len(sessions) == 1
    s = sessions[0]
    assert s["session_id"] == "S-read"
    assert "started_at" in s and "ended_at" in s
    # JOINed metrics from session_outcomes
    assert s["duration_s"] == 120
    assert s["tokens_used"] == 4096
    assert s["files_read"] == 7
    assert s["files_modified"] == 3
    assert s["skills_invoked"] == 2


def test_sessions_for_task_empty_when_no_link(project):
    """A task with zero linked sessions returns []. Backs the UI empty
    state."""
    from prism_service.project_context import get_project

    t = json.loads(_text(_call("task_create", {"title": "lonely task"})))
    ctx = get_project(project)
    assert ctx.task_svc.sessions_for_task(t["id"]) == []


# ----------------------------------------------------------------------
# CONDUCTOR WRITER — advance_task stamps task_sessions
# ----------------------------------------------------------------------


def test_conductor_advance_stamps_task_session(project):
    """conductor advance carries task_id + a session; advancing must
    stamp/refresh a task_sessions row so association is captured even if
    a session never reaches the Stop hook."""
    from prism_service.project_context import get_project

    t = json.loads(_text(_call("task_create", {"title": "conductor task"})))
    tid = t["id"]

    # Advance once with a session attached.
    _call("conductor_advance", {"id": tid, "session_id": "S-cond"})

    ctx = get_project(project)
    conn = sqlite3.connect(str(ctx._data_dir / "scores.db"))
    row = conn.execute(
        "SELECT task_id, session_id FROM task_sessions "
        "WHERE task_id=? AND session_id=?",
        (tid, "S-cond"),
    ).fetchone()
    conn.close()
    assert row is not None, (
        "conductor advance_task did not stamp a task_sessions row from "
        "its carried task_id + session"
    )


# ----------------------------------------------------------------------
# AUTO WRITER (Stop path) — record_session_outcome links in_progress tasks
# ----------------------------------------------------------------------


def test_record_session_outcome_links_in_progress_task(project):
    """When a session ends (the Stop hook POSTs record_session_outcome
    through this very dispatcher) while a task is status='in_progress',
    the server-side writer must upsert a task_sessions row tying that
    session_id to the in_progress task — without any explicit
    task_link_session call from the hook."""
    from prism_service.project_context import get_project

    t = json.loads(_text(_call("task_create", {"title": "active task"})))
    tid = t["id"]
    # Put the task in_progress — the precondition for the auto-writer.
    _call("task_update", {"id": tid, "status": "in_progress"})

    # Session ends: the Stop hook's record_session_outcome lands here.
    _call("record_session_outcome", {
        "session_id": "S-stop", "duration_s": 90, "tokens_used": 2048,
        "files_read": 4, "files_modified": 2, "skills_invoked": 1,
    })

    ctx = get_project(project)
    linked = ctx.task_svc.sessions_for_task(tid)
    assert any(s["session_id"] == "S-stop" for s in linked), (
        "record_session_outcome did not auto-link the ending session to "
        "the in_progress task (AUTO WRITER / Stop path criterion)"
    )


# ----------------------------------------------------------------------
# API — GET /api/tasks/{id} extended with a `sessions` field
# ----------------------------------------------------------------------


def test_api_task_detail_returns_sessions_field(project, tmp_path):
    """The existing GET /api/tasks/{id} route returns a `sessions`
    field (reusing the route — no new top-level route)."""
    from fastapi.testclient import TestClient
    from prism_service.api.tasks import router as tasks_router
    from fastapi import FastAPI

    t = json.loads(_text(_call("task_create", {"title": "api task"})))
    tid = t["id"]
    _call("record_session_outcome", {
        "session_id": "S-api", "duration_s": 60, "tokens_used": 100,
        "files_read": 1, "files_modified": 1, "skills_invoked": 0,
    })
    _call("task_link_session", {"task_id": tid, "session_id": "S-api"})

    app = FastAPI()
    app.include_router(tasks_router, prefix="/api/tasks")
    client = TestClient(app)
    resp = client.get(f"/api/tasks/{tid}?project=test-task-sessions")
    assert resp.status_code == 200
    body = resp.json()
    assert "sessions" in body, "GET /api/tasks/{id} missing `sessions` field"
    assert any(s["session_id"] == "S-api" for s in body["sessions"])


# ----------------------------------------------------------------------
# UI — TaskDetailPage renders Sessions (N) + explicit empty state
# (page render is verified via the running deploy; here we pin that the
#  source actually consumes the new field — no raw <pre>JSON. Mirrors
#  tests/unit/test_ui_learning.py's "pages verified via deploy" note.)
# ----------------------------------------------------------------------


def test_task_detail_page_renders_sessions_section():
    src = (_WEB_SRC / "pages" / "TaskDetailPage.tsx").read_text(encoding="utf-8")
    assert "sessions" in src, (
        "TaskDetailPage.tsx does not consume the `sessions` field"
    )
    assert "Sessions (" in src, (
        "TaskDetailPage.tsx must render a 'Sessions (N)' section header"
    )


def test_task_detail_page_has_session_empty_state():
    src = (_WEB_SRC / "pages" / "TaskDetailPage.tsx").read_text(encoding="utf-8")
    # An explicit 0-session branch (e.g. sessions.length === 0 ? <empty>).
    assert "sessions.length === 0" in src, (
        "TaskDetailPage.tsx must render an explicit 0-session empty state"
    )
