"""LL-01 tests — learning-loop schema migrations.

Verifies that:
  * New scores.db tables are created fresh
  * Migrations are idempotent (second run is no-op, data preserved)
  * `tasks` gains nullable columns (embedding, merge_sha, merged_at)
  * High-frequency join columns have indexes
  * `memory_meta` sidecar table exists (JSONL is primary, SQL for queryable metadata)

Parent task: 37932f3f-9cd4-40bf-9df3-e9db19fcc88d · Sub-task LL-01
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make ``app`` importable without installing the service as a package.
_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _init_brain(db_dir: Path):
    from app.engines.brain_engine import Brain
    return Brain(
        brain_db=str(db_dir / "brain.db"),
        graph_db=str(db_dir / "graph.db"),
        scores_db=str(db_dir / "scores.db"),
    )


def _init_tasks(db_dir: Path):
    from app.services.task_service import TaskService
    return TaskService(str(db_dir / "tasks.db"))


def _tables(conn) -> set[str]:
    return {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def _columns(conn, table: str) -> dict[str, dict]:
    """Return {column_name: PRAGMA-info-row-as-dict}."""
    out: dict[str, dict] = {}
    for row in conn.execute(f"PRAGMA table_info({table})").fetchall():
        # row: (cid, name, type, notnull, dflt_value, pk)
        out[row[1]] = {
            "type": row[2], "notnull": row[3], "dflt_value": row[4], "pk": row[5],
        }
    return out


def _indexes(conn, table: str) -> list[tuple[str, str]]:
    """Return [(index_name, sql_definition)] for all indexes on a table."""
    return [
        (row[0], row[1] or "")
        for row in conn.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='index' AND tbl_name=?",
            (table,),
        ).fetchall()
    ]


_NEW_SCORES_TABLES = (
    "task_sessions",
    "task_variants",
    "task_quality_rollup",
    "operator_baselines",
    "consolidation_candidates",
    "consolidation_runs",
    "memory_meta",
)


def test_tables_created_on_fresh_db(tmp_path):
    """Every LL-01 table is present after the first Brain init."""
    brain = _init_brain(tmp_path)
    try:
        tables = _tables(brain._scores)
        for t in _NEW_SCORES_TABLES:
            assert t in tables, f"{t} missing from fresh scores.db"
    finally:
        brain._scores.close()
        brain._brain.close()
        brain._graph.close()


def test_migration_idempotent(tmp_path):
    """Re-invoking schema init is a no-op and never raises."""
    brain = _init_brain(tmp_path)
    try:
        brain._init_scores_schema()
        brain._init_scores_schema()
        tables = _tables(brain._scores)
        for t in _NEW_SCORES_TABLES:
            assert t in tables
    finally:
        brain._scores.close()
        brain._brain.close()
        brain._graph.close()


def test_tables_unchanged_on_existing_db(tmp_path):
    """Reopening a Brain pointing at existing DBs preserves rows."""
    brain1 = _init_brain(tmp_path)
    try:
        brain1._scores.execute(
            "INSERT INTO task_quality_rollup(task_id, quality_score) "
            "VALUES (?, ?)",
            ("t-probe", 0.77),
        )
        brain1._scores.commit()
    finally:
        brain1._scores.close()
        brain1._brain.close()
        brain1._graph.close()

    brain2 = _init_brain(tmp_path)
    try:
        row = brain2._scores.execute(
            "SELECT quality_score FROM task_quality_rollup WHERE task_id=?",
            ("t-probe",),
        ).fetchone()
        assert row is not None
        assert abs(row[0] - 0.77) < 1e-9
    finally:
        brain2._scores.close()
        brain2._brain.close()
        brain2._graph.close()


def test_new_columns_nullable_for_backward_compat(tmp_path):
    """New `tasks` columns must be nullable so legacy rows keep working."""
    tasks = _init_tasks(tmp_path)
    try:
        cols = _columns(tasks._db, "tasks")
        for col in ("embedding", "merge_sha", "merged_at"):
            assert col in cols, f"{col} missing from tasks"
            # notnull flag == 0 means the column allows NULL
            assert cols[col]["notnull"] == 0, (
                f"tasks.{col} must be nullable for backward compatibility"
            )
    finally:
        tasks._db.close()


def test_indexes_present_on_task_id_and_session_id(tmp_path):
    """High-frequency join columns have indexes (else scans explode)."""
    brain = _init_brain(tmp_path)
    try:
        ts_idx_sql = " ".join(sql for _, sql in _indexes(brain._scores, "task_sessions"))
        cc_idx_sql = " ".join(sql for _, sql in _indexes(brain._scores, "consolidation_candidates"))
        tv_idx_sql = " ".join(sql for _, sql in _indexes(brain._scores, "task_variants"))

        assert "session_id" in ts_idx_sql.lower(), (
            f"task_sessions needs session_id index; got {ts_idx_sql!r}"
        )
        assert "session_id" in cc_idx_sql.lower(), (
            f"consolidation_candidates needs session_id index; got {cc_idx_sql!r}"
        )
        assert "task_id" in tv_idx_sql.lower(), (
            f"task_variants needs task_id index; got {tv_idx_sql!r}"
        )
    finally:
        brain._scores.close()
        brain._brain.close()
        brain._graph.close()
