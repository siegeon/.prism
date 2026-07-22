"""Concept recalls attribute the asking session — red→green pins for task
fc258f15-0538-494f-a398-82627e19ee6b.

  AC-1  recall_log migrates in place: a legacy db WITHOUT session_id keeps
        its rows readable and new rows carry the column (ALTER-if-missing).
  AC-2  recall(query, session_id=...) stamps the REAL session onto the log
        row; without one it stamps "" — never a fabricated uuid (the task's
        pre-declared misfire).
  AC-3  sessions_that_recalled(entry_id) surfaces distinct recalling
        sessions with counts; the OKF concept payload carries
        recalled_by_sessions.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def _svc(tmp_path):
    from prism_service.services.memory_service import MemoryService
    return MemoryService(str(tmp_path / "mulch" / "expertise"))


def _store(svc, name="session-attr-fact"):
    return svc.store(
        type="convention", name=name,
        description="recalls must attribute the asking session",
        domain="testing", classification="tactical")


def _recall_rows(svc):
    return svc._recall_db.execute(
        "SELECT entry_id, task_id, session_id FROM recall_log").fetchall()


# ---------------------------------------------------------------------------
# AC-2 — the writer stamps the real session, never a fabricated one
# ---------------------------------------------------------------------------


def test_recall_stamps_the_supplied_session(tmp_path):
    svc = _svc(tmp_path)
    entry = _store(svc)
    got = svc.recall("asking session", domain="testing",
                     session_id="sess-real-1")
    assert got, "recall found nothing"
    rows = _recall_rows(svc)
    assert rows, "no recall_log rows written"
    assert any(r[2] == "sess-real-1" for r in rows), rows


def test_recall_without_session_stamps_empty_not_fabricated(tmp_path):
    svc = _svc(tmp_path)
    _store(svc)
    svc.recall("asking session", domain="testing")
    rows = _recall_rows(svc)
    assert rows
    for r in rows:
        assert r[2] == "", f"fabricated session id stamped: {r[2]!r}"


# ---------------------------------------------------------------------------
# AC-1 — legacy db migrates in place
# ---------------------------------------------------------------------------


def test_legacy_recall_db_migrates_in_place(tmp_path):
    mulch = tmp_path / "mulch" / "expertise"
    mulch.mkdir(parents=True)
    legacy = mulch / "recall_log.db"
    conn = sqlite3.connect(str(legacy))
    conn.executescript("""
        CREATE TABLE recall_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_id TEXT NOT NULL,
            entry_domain TEXT DEFAULT '',
            query TEXT DEFAULT '',
            recalled_at TEXT NOT NULL,
            task_id TEXT DEFAULT '',
            outcome TEXT DEFAULT ''
        );
        INSERT INTO recall_log (entry_id, entry_domain, query, recalled_at,
                                task_id)
        VALUES ('mx-legacy', 'testing', 'old query',
                '2026-07-01T00:00:00+00:00', 'T-legacy');
    """)
    conn.commit()
    conn.close()

    svc = _svc(tmp_path)
    # legacy row still readable through the task reader
    tasks = svc.tasks_that_recalled("mx-legacy")
    assert tasks and tasks[0]["task_id"] == "T-legacy", tasks
    # and NEW rows carry the migrated column
    entry = _store(svc)
    svc.recall("asking session", domain="testing", session_id="sess-mig-1")
    rows = _recall_rows(svc)  # would raise if session_id column is absent
    assert any(r[2] == "sess-mig-1" for r in rows), rows


# ---------------------------------------------------------------------------
# AC-3 — the read side surfaces sessions
# ---------------------------------------------------------------------------


def test_sessions_that_recalled_groups_distinct_sessions(tmp_path):
    svc = _svc(tmp_path)
    entry = _store(svc)
    svc.recall("asking session", domain="testing", session_id="sess-a")
    svc.recall("asking session", domain="testing", session_id="sess-a")
    svc.recall("asking session", domain="testing", session_id="sess-b")
    got = svc.sessions_that_recalled(entry.id)
    by_id = {r["session_id"]: r for r in got}
    assert set(by_id) == {"sess-a", "sess-b"}, got
    assert by_id["sess-a"]["recall_count"] == 2
    assert by_id["sess-b"]["recall_count"] == 1


def test_okf_concept_payload_carries_recalling_sessions(tmp_path):
    from prism_service.services.okf_host import OkfHost
    svc = _svc(tmp_path)
    entry = _store(svc)
    svc.recall("asking session", domain="testing", session_id="sess-okf-1")
    host = OkfHost(svc, None)
    idx = host.index()
    paths = [c["path"] for c in idx.get("concepts", [])
             if entry.id in str(c)]
    # resolve the concept page by its id (index shape tolerant)
    page = None
    for c in idx.get("concepts", []):
        got = host.get(c["path"])
        if got and str(got.get("frontmatter", {}).get("id")) == entry.id:
            page = got
            break
    assert page is not None, "concept page not found in OKF bundle"
    sessions = page.get("recalled_by_sessions")
    assert sessions and sessions[0]["session_id"] == "sess-okf-1", page.get(
        "recalled_by_sessions")
