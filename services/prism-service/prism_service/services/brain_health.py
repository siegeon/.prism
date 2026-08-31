"""The conductor pipeline's BRAIN-HEALTH node (task 013c5197).

WHAT IT IS. `land` is the FSM's ship step; `reap` (services/task_reaper.py)
runs right after it, in the same position on the pipeline. This node runs
BESIDE `reap`, same trigger: once a play (a conductor drive on a task) has
really landed on origin/main, two things must happen before the play is
truly finished:

  1. Whatever memories that play wrote get indexed into Brain's FTS store
     -- INCREMENTALLY, one entry at a time, never a full-store rebuild
     (a full pass gets slower as the store grows).
  2. Coverage (how much of the live memory store Brain can actually search)
     is measured and checked against a floor. Below the floor, this module
     RAISES -- it never only logs. `memory_service.py`'s own indexer
     (`MemoryService._index_in_brain`) wraps every write in a bare
     `except Exception: pass`, so a failing index has always been silent;
     this node is the thing that finally notices, on every finished play.

WHY IT IS DETERMINISTIC PYTHON, NO MODEL CALL. Same reasoning as `reap`:
this is SQL plus a Brain index call, run once per finished play from
`ship_worker.py` -- no judgement call belongs here, so none is made.

WHY IT NEVER RE-INDEXES THE WHOLE STORE. The candidates for THIS play are
found by joining `task_sessions` (task -> session, task_service.py) to
`memory_meta` (session -> memory, stamped by the `memory_store` MCP tool
when a caller passes `session_id`) -- both sidecar tables in the project's
own `scores.db`. Only the memory ids that surface from that join are
touched; the rest of the store is never scanned.

THE FLOOR. `DEFAULT_COVERAGE_FLOOR` is a fraction of `entries` that must be
`indexed` (the same `entries`/`indexed`/`ratio` shape `GET /api/brain/health`
already exposes, computed here directly against the service layer since
this runs in-process, not over HTTP). Below it, `CoverageBelowFloor` is
raised, carrying the numbers a caller needs to report them honestly --
reporting a number that nobody acts on is exactly the failure mode that let
the decay in `memory_service.py` go unnoticed for weeks.

FAILS LOUD ON THE COVERAGE CHECK, NEVER ON THE INDEXING ITSELF. A genuine
error from `brain_svc.index_doc` propagates -- it is not caught here, the
same way `task_reaper` never catches an error it cannot itself judge safe
to ignore. The only outcome this module deliberately turns into a typed
signal is "coverage fell below the floor."
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from prism_service.project_context import get_project

# The canvas/FSM node id -- same convention as task_reaper.REAP_NODE.
HEALTH_NODE = "brain-health"

# No such floor existed anywhere in the service before this task. Chosen
# as a conservative "half the store must be searchable" bar; the live
# instance measured 3% the night this task was written; MCP tool wraps
# every failing memory_service index in a bare `except: pass`.
DEFAULT_COVERAGE_FLOOR = 0.5

_ONTOLOGY_KIND = "conductor.brain_health"


class CoverageBelowFloor(RuntimeError):
    """Raised -- never only logged -- when indexed/entries falls under the
    floor. Carries the real numbers so a caller can report them honestly."""

    def __init__(self, entries: int, indexed: int, ratio: float, floor: float) -> None:
        self.entries = entries
        self.indexed = indexed
        self.ratio = ratio
        self.floor = floor
        super().__init__(
            f"brain coverage {indexed}/{entries} ({ratio:.1%}) is below "
            f"the floor of {floor:.1%}"
        )


def _memory_ids_written_by(scores_db: str, task_id: str) -> list[str]:
    """Memory ids this task's own session(s) stamped via `memory_store`.

    Joins task_sessions (task -> session, task_service.py) to memory_meta
    (session -> memory, mcp/tools.py's memory_store handler) in the same
    scores.db -- never a scan of the whole memory store. Both tables are
    created IF NOT EXISTS here, the same defensive idiom
    TaskService._ensure_task_sessions uses, since this can run before
    either sidecar table has been touched in a fresh project.
    """
    try:
        from prism_service.services import sqlite_db

        conn = sqlite_db.connect(scores_db, timeout=5.0)
    except sqlite3.Error:
        return []
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS task_sessions ("
            "task_id TEXT NOT NULL, session_id TEXT NOT NULL, "
            "started_at TEXT, ended_at TEXT, "
            "PRIMARY KEY (task_id, session_id))"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS memory_meta ("
            "memory_id TEXT PRIMARY KEY, session_id TEXT, status TEXT)"
        )
        rows = conn.execute(
            "SELECT DISTINCT mm.memory_id FROM memory_meta mm "
            "JOIN task_sessions ts ON ts.session_id = mm.session_id "
            "WHERE ts.task_id = ?",
            (task_id,),
        ).fetchall()
        return [r[0] for r in rows if r and r[0]]
    finally:
        conn.close()



# ----------------------------------------------------------------------
# The coverage series (task 0ee4dc98)
# ----------------------------------------------------------------------
# A number on its own is not enough. This decay hid for weeks because a
# figure existed that nobody watched, so the Dashboard draws coverage over
# time and a sample lands every time a play ends. The sample is written
# BEFORE the floor check, so a fall is on file exactly like a rise.

_SAMPLES_SCHEMA = """
    CREATE TABLE IF NOT EXISTS brain_coverage_samples (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT,
        entries INTEGER NOT NULL,
        indexed INTEGER NOT NULL,
        ratio REAL NOT NULL,
        measured_at TEXT NOT NULL
    );
"""


def _samples_conn(scores_db: str):
    """Open the samples db through the project's one hardening chokepoint.

    `sqlite_db.connect` applies WAL and a Row factory; a bare
    `sqlite3.connect` skips both, which is why `test_no_bare_connect`
    forbids it outside that helper.
    """
    from prism_service.services import sqlite_db

    conn = sqlite_db.connect(scores_db, timeout=30.0)
    conn.executescript(_SAMPLES_SCHEMA)
    return conn


def _record_sample(scores_db: str, *, task_id: str, entries: int,
                   indexed: int, ratio: float) -> None:
    """Append one coverage sample. Best-effort: a failure here must never
    stop a play, but it must also never silently swallow the sample -- the
    caller's own verdict still carries the live numbers."""
    from datetime import datetime, timezone

    try:
        conn = _samples_conn(scores_db)
        conn.execute(
            "INSERT INTO brain_coverage_samples "
            "(task_id, entries, indexed, ratio, measured_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (task_id, int(entries), int(indexed), float(ratio),
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def coverage_history(scores_db: str, limit: int = 60) -> list[dict]:
    """Coverage samples OLDEST FIRST, so a chart can draw them left to right.

    Never filters by value. A fall is as much a data point as a rise, and
    dropping one would hide the exact event a person needs to act on.
    """
    try:
        conn = _samples_conn(scores_db)
        rows = conn.execute(
            "SELECT task_id, entries, indexed, ratio, measured_at "
            "FROM brain_coverage_samples ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        conn.close()
    except Exception:
        return []
    return [
        {"task_id": r[0], "entries": r[1], "indexed": r[2],
         "ratio": r[3], "measured_at": r[4]}
        for r in reversed(rows)
    ]


def index_finished_play(
    task_id: str,
    project: str = "default",
    *,
    task_svc: object = None,  # accepted for callers that already hold one; unused internally
    floor: float = DEFAULT_COVERAGE_FLOOR,
) -> dict:
    """Run the brain-health node for one finished play.

    Indexes whatever memories `task_id`'s own session(s) wrote (incremental,
    never a full reindex), then checks coverage against `floor`. Returns a
    typed verdict dict on pass; raises `CoverageBelowFloor` when coverage is
    under the floor -- the caller (`ship_worker.py`) decides how that
    surfaces, but it must never be swallowed silently.
    """
    del task_svc  # the join needs only the scores.db path, not the object

    ctx = get_project(project)
    memory_svc = ctx.memory_svc
    brain_svc = ctx.brain_svc
    scores_db = str(ctx._data_dir / "scores.db")

    reindexed = 0
    for memory_id in _memory_ids_written_by(scores_db, task_id):
        entry = memory_svc.get_entry(memory_id)
        if entry is None:
            continue
        content = f"{entry.name}\n{entry.description}"
        brain_svc.index_doc(
            path=f"memory/{entry.domain}/{entry.id}",
            content=content,
            domain="expertise",
        )
        reindexed += 1

    entries = sum(
        len(memory_svc.list_entries(d)) for d in memory_svc.list_domains()
    )
    indexed = brain_svc.expertise_coverage()
    ratio = (indexed / entries) if entries else 0.0

    # RECORD BEFORE THE RAISE. A fall must land in the series exactly like a
    # rise does. If this sat after the raise, every sample below the floor
    # would be swallowed and the chart would only ever show good news --
    # which is precisely the decay this node exists to make visible.
    _record_sample(scores_db, task_id=task_id, entries=entries,
                   indexed=indexed, ratio=ratio)

    if ratio < floor:
        raise CoverageBelowFloor(entries=entries, indexed=indexed,
                                  ratio=ratio, floor=floor)

    return {
        "kind": _ONTOLOGY_KIND,
        "node_id": HEALTH_NODE,
        "task_id": task_id,
        "outcome": "pass",
        "reindexed": reindexed,
        "entries": entries,
        "indexed": indexed,
        "ratio": ratio,
        "floor": floor,
    }
