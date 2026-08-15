"""CONTESTED-SESSION burn attribution (task a91f1d11).

THE DEFECT: ConductorService.phase_progress attributed a linked session's
ENTIRE lifetime live-transcript burn to EVERY task that session is linked
to, unconditionally — so a session linked to two unrelated tasks (e.g. via
a locate/context sweep on one task, and a genuine drive on another) painted
byte-identical tok_total/turns on both tiles (session daffe20c on both
170991cc and 2419bff2, mx-56d049).

THE FIX (contested-session ownership): a session linked to exactly ONE task
is UNCONTESTED and always attributable, byte-for-byte the pre-existing
behavior (test_conductor_token_source.py). A session linked to TWO OR MORE
tasks is CONTESTED — it counts toward a task only when agent_runs carries
objective evidence that task was actually DRIVEN under that session (a real
(task_id, session_id) row, the same spine ConductorService._record_agent_run
writes on every advance_task). A task whose only claim to a contested
session is a bare task_sessions link (no agent_runs row) must not inherit
that session's burn.

Pure data-access, mirrors agent_runs_data.py's shape: functions take the
collaborators they need (task_svc, scores_db) rather than importing a
singleton, so this stays trivially unit-testable against an isolated
scores.db.
"""

from __future__ import annotations


def contested_session_task_ids(task_svc, session_id: str) -> list[str]:
    """All DISTINCT task ids ``session_id`` is linked to via task_sessions
    (or the in-memory fallback link map when no scores_db is bound, mirroring
    TaskService.link_session's own no-scores_db fallback)."""
    if not session_id:
        return []
    scores_db = getattr(task_svc, "_scores_db", None)
    if not scores_db:
        return [tid for tid, sess in
                getattr(task_svc, "_session_links", {}).items()
                if session_id in sess]
    from prism_service.services import sqlite_db

    conn = sqlite_db.connect(scores_db, timeout=5.0)
    try:
        task_svc._ensure_task_sessions(conn)
        rows = conn.execute(
            "SELECT DISTINCT task_id FROM task_sessions WHERE session_id=?",
            (session_id,),
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def session_drove_task(scores_db: str, task_id: str, session_id: str) -> bool:
    """True when agent_runs has at least one row for (task_id, session_id) —
    objective evidence this task was actually DRIVEN under this session, as
    opposed to a bare task_sessions link (e.g. a locate/context sweep on an
    unrelated task calling task_link_session)."""
    if not scores_db or not task_id or not session_id:
        return False
    try:
        from prism_service.services import agent_runs_data

        rows = agent_runs_data.get_agent_runs(
            scores_db, limit=1, task_id=task_id, session_id=session_id)
    except Exception:
        return False
    return bool(rows)


def session_is_attributable(task_svc, scores_db: str, task_id: str,
                             session_id: str) -> bool:
    """True when ``task_id`` may charge ``session_id``'s live-transcript burn
    to its own tile: always true when the session is UNCONTESTED (linked to
    exactly one task); when CONTESTED (2+ tasks), only when agent_runs
    evidences ``task_id`` actually drove it."""
    if not session_id:
        return True
    linked_tasks = contested_session_task_ids(task_svc, session_id)
    if len(linked_tasks) <= 1:
        return True
    return session_drove_task(scores_db, task_id, session_id)
