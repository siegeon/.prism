"""GET /api/sessions/{id} must not 404 a session that is linked to a task
before its transcript is ever parsed (task page defect observed live on
task a65c66e5-b8a7-44b4-a223-f1342cfaaa14, 2026-09-12).

POST /api/tasks/{task_id}/sessions upserts a task_sessions row the moment a
background job links a session id — long before (or even if never) a Claude
transcript for that session lands under ~/.claude/projects and gets parsed
into session_outcomes. The old detail() route only ever looked at
session_outcomes, so a linked-but-unscored session 404d: a failed network
request on every task-page load, and a broken session link for the user to
click through to. This pins the fix: 200 with has_transcript=false for a
session_id present in task_sessions with no session_outcomes row, and 404
reserved for a session_id that is not linked anywhere at all.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from prism_service.api import sessions as sessions_api


def _make_client(tmp_path, monkeypatch):
    class _Project:
        _data_dir = tmp_path

    def _get_project(p):
        if p != "default":
            raise ValueError(f"unknown project: {p}")
        return _Project()

    monkeypatch.setattr(sessions_api, "get_project", _get_project)

    app = FastAPI()
    app.include_router(sessions_api.router, prefix="/api/sessions")
    return TestClient(app)


def _scores_db(tmp_path) -> Path:
    return tmp_path / "scores.db"


def _link_session_only(tmp_path, task_id: str, session_id: str) -> None:
    """Write a bare task_sessions row with NO session_outcomes table at
    all -- the state right after link_session() runs before Brain has ever
    opened this scores.db (task_service._ensure_task_sessions creates only
    task_sessions, never session_outcomes)."""
    conn = sqlite3.connect(_scores_db(tmp_path))
    try:
        conn.execute(
            "CREATE TABLE task_sessions (task_id TEXT NOT NULL, "
            "session_id TEXT NOT NULL, started_at TEXT, ended_at TEXT, "
            "PRIMARY KEY (task_id, session_id))"
        )
        conn.execute(
            "INSERT INTO task_sessions (task_id, session_id, started_at, ended_at) "
            "VALUES (?, ?, '2026-09-12T00:00:00+00:00', NULL)",
            (task_id, session_id),
        )
        conn.commit()
    finally:
        conn.close()


def test_linked_session_with_no_transcript_answers_200_not_404(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    sid = "e04033d1-2860-4ae4-b080-d5976b690535"
    _link_session_only(tmp_path, "a65c66e5-b8a7-44b4-a223-f1342cfaaa14", sid)

    r = client.get(f"/api/sessions/{sid}?project=default")
    assert r.status_code == 200, r.text
    session = r.json()["session"]
    assert session["session_id"] == sid
    assert session["has_transcript"] is False
    assert session["files_read_paths"] == []
    assert session["files_modified_paths"] == []


def test_truly_unknown_session_still_404s(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    # No task_sessions table at all -- a scores.db that has never seen a
    # session link or a transcript import.
    conn = sqlite3.connect(_scores_db(tmp_path))
    conn.close()

    r = client.get("/api/sessions/00000000-0000-0000-0000-000000000000?project=default")
    assert r.status_code == 404


def test_scored_session_still_returns_its_real_outcome(tmp_path, monkeypatch):
    """A session WITH a session_outcomes row keeps returning its real
    metrics and now also reports has_transcript=true, so the SPA can tell
    the two states apart."""
    client = _make_client(tmp_path, monkeypatch)
    sid = "11111111-1111-1111-1111-111111111111"
    conn = sqlite3.connect(_scores_db(tmp_path))
    try:
        conn.execute(
            "CREATE TABLE session_outcomes (session_id TEXT PRIMARY KEY, "
            "duration_s INTEGER DEFAULT 0, tokens_used INTEGER DEFAULT 0, "
            "files_read INTEGER DEFAULT 0, files_modified INTEGER DEFAULT 0, "
            "skills_invoked INTEGER DEFAULT 0, timestamp TEXT, "
            "files_read_paths TEXT, files_modified_paths TEXT)"
        )
        conn.execute(
            "INSERT INTO session_outcomes (session_id, duration_s, tokens_used) "
            "VALUES (?, 42, 1000)",
            (sid,),
        )
        conn.commit()
    finally:
        conn.close()

    r = client.get(f"/api/sessions/{sid}?project=default")
    assert r.status_code == 200, r.text
    session = r.json()["session"]
    assert session["has_transcript"] is True
    assert session["duration_s"] == 42
    assert session["tokens_used"] == 1000
