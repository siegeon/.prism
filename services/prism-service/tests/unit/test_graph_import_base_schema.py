"""Regression for issue #136 — fresh folder-mode project never indexes.

On a brand-new folder-mode project, bootstrap ingest calls
GraphService._import_graph_json against a graph.db whose base
entities/relationships tables were never created by
BrainEngine._init_graph_schema. _graph_schema_migrations only ALTERs
existing tables, so the `DELETE FROM relationships` at the head of the
import raises `OperationalError: no such table: relationships` and the
DB ends up with only ['communities','graph_annotations'], brain.db
stays empty, and doc_count is stuck at 0 forever.

This test pins the fixed behavior: importing into a FRESH empty
graph.db must succeed with entities+relationships present and a
non-zero node/entity count. It FAILS on pre-fix code (raises
OperationalError).
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _fresh_result() -> dict:
    """Counters the import path does `+=` on must be pre-seeded."""
    return {
        "imported_entities": 0,
        "imported_relationships": 0,
        "nodes": 0,
        "edges": 0,
        "communities": 0,
    }


def test_import_into_fresh_empty_graph_db_creates_base_schema(tmp_path: Path):
    """Importing into a never-initialized graph.db must not raise and
    must leave entities+relationships present with non-zero counts."""
    from prism_service.services.graph_service import GraphService

    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    db = tmp_path / "graph.db"
    # FRESH, empty graph.db — base schema was never created. This is the
    # exact state of a fresh folder-mode project at bootstrap-ingest time.
    sqlite3.connect(str(db)).close()

    svc = GraphService(project_data_dir=str(proj_dir), graph_db_path=str(db))
    data = {
        "nodes": [
            {"id": "n1", "label": "alpha"},
            {"id": "n2", "label": "beta"},
        ],
        "links": [{"source": "n1", "target": "n2", "relation": "calls"}],
    }
    result = _fresh_result()

    # Pre-fix: raises OperationalError: no such table: relationships.
    svc._import_graph_json(data, result)

    conn = sqlite3.connect(str(db))
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "entities" in tables, tables
        assert "relationships" in tables, tables
        ent_count = conn.execute("SELECT count(*) FROM entities").fetchone()[0]
        rel_count = conn.execute(
            "SELECT count(*) FROM relationships"
        ).fetchone()[0]
    finally:
        conn.close()

    assert ent_count > 0, ent_count
    assert rel_count > 0, rel_count
    assert result["imported_entities"] > 0, result
    assert result["imported_relationships"] > 0, result


def test_reimport_is_idempotent_on_initialized_db(tmp_path: Path):
    """Re-running the import (second ingest) on an already-initialized
    graph.db is safe — CREATE TABLE IF NOT EXISTS + DELETE/re-import
    snapshot semantics preserved, no error, no corruption."""
    from prism_service.services.graph_service import GraphService

    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    db = tmp_path / "graph.db"
    sqlite3.connect(str(db)).close()

    svc = GraphService(project_data_dir=str(proj_dir), graph_db_path=str(db))
    data = {
        "nodes": [
            {"id": "n1", "label": "alpha"},
            {"id": "n2", "label": "beta"},
        ],
        "links": [{"source": "n1", "target": "n2", "relation": "calls"}],
    }

    svc._import_graph_json(data, _fresh_result())
    # Second ingest against the now-initialized DB must not raise and the
    # snapshot count stays stable (DELETE + re-import, not accumulation).
    svc._import_graph_json(data, _fresh_result())

    conn = sqlite3.connect(str(db))
    try:
        ent_count = conn.execute("SELECT count(*) FROM entities").fetchone()[0]
        rel_count = conn.execute(
            "SELECT count(*) FROM relationships"
        ).fetchone()[0]
    finally:
        conn.close()

    assert ent_count == 2, ent_count
    assert rel_count == 1, rel_count
