"""Regression for issue #136 — fresh folder-mode project never indexes.

Bug: on a brand-new folder-mode project, bootstrap ingest aborts with
`OperationalError: no such table: relationships` from graph.rebuild. The
graphify step produces graph.json fine, but GraphService._import_graph_json
opens its OWN sqlite connection to graph.db and at graph_service.py:824-827
calls `_graph_schema_migrations(conn)` then `DELETE FROM relationships` /
`DELETE FROM entities` — WITHOUT first creating the base entities /
relationships tables (those only ever get created by
BrainEngine._init_graph_schema, which never ran against this fresh graph.db).
graph.db ends up with only ['communities','graph_annotations'], brain.db
stays empty, and doc_count is stuck at 0 forever.

These tests open a FRESH, EMPTY graph.db (mirroring the bootstrap-ingest
seam) and assert _import_graph_json completes with both base tables present
and a non-zero node/edge count. They FAIL on pre-fix code (raise
OperationalError at the DELETE) and pass once the base-schema DDL runs first.
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
    """The counter dict _import_graph_json does `+=` on must be pre-seeded."""
    return {
        "imported_entities": 0,
        "imported_relationships": 0,
    }


def _make_service(tmp_path: Path):
    from prism_service.services.graph_service import GraphService

    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    graph_db = tmp_path / "graph.db"
    # NOTE: do NOT pre-create graph.db schema — this is the fresh
    # folder-mode bootstrap seam where _init_graph_schema never ran.
    svc = GraphService(
        project_data_dir=str(proj_dir),
        graph_db_path=str(graph_db),
    )
    return svc, graph_db


def test_import_into_fresh_graph_db_creates_base_tables(tmp_path: Path):
    """A fresh empty graph.db imports without OperationalError.

    Pins issue #136: pre-fix this raises
    `sqlite3.OperationalError: no such table: relationships`.
    """
    svc, graph_db = _make_service(tmp_path)

    data = {
        "nodes": [
            {"id": "n1", "label": "alpha"},
            {"id": "n2", "label": "beta"},
        ],
        "links": [
            {"source": "n1", "target": "n2", "relation": "calls"},
        ],
    }
    result = _fresh_result()

    # Must NOT raise — this is the regression.
    out = svc._import_graph_json(data, result, None)

    # Base schema present after import.
    conn = sqlite3.connect(str(graph_db))
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "entities" in tables, tables
        assert "relationships" in tables, tables

        ent_count = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        rel_count = conn.execute(
            "SELECT COUNT(*) FROM relationships"
        ).fetchone()[0]
    finally:
        conn.close()

    # Non-zero node/edge count — the graph actually populated (doc_count
    # proxy: a fresh project that aborts leaves these at 0).
    assert ent_count > 0, "entities table is empty — import did not populate"
    assert rel_count > 0, "relationships table is empty — import did not populate"
    assert out["imported_entities"] > 0, out
    assert out["imported_relationships"] > 0, out


def test_import_into_fresh_graph_db_is_idempotent(tmp_path: Path):
    """Re-running the import on an already-initialized graph.db is safe.

    CREATE TABLE IF NOT EXISTS + the existing DELETE/re-import snapshot
    semantics must not error or corrupt the second time around.
    """
    svc, graph_db = _make_service(tmp_path)

    data = {
        "nodes": [
            {"id": "n1", "label": "alpha"},
            {"id": "n2", "label": "beta"},
            {"id": "n3", "label": "gamma"},
        ],
        "links": [
            {"source": "n1", "target": "n2", "relation": "calls"},
            {"source": "n2", "target": "n3", "relation": "calls"},
        ],
    }

    # First import (fresh) then a SECOND import (already initialized).
    svc._import_graph_json(data, _fresh_result(), None)
    out2 = svc._import_graph_json(data, _fresh_result(), None)

    conn = sqlite3.connect(str(graph_db))
    try:
        ent_count = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        rel_count = conn.execute(
            "SELECT COUNT(*) FROM relationships"
        ).fetchone()[0]
    finally:
        conn.close()

    # Snapshot semantics preserved: a re-import of the same graph yields
    # the same populated counts (no duplication, no loss, no error).
    assert ent_count == 3, ent_count
    assert rel_count == 2, rel_count
    assert out2["imported_entities"] == 3, out2
    assert out2["imported_relationships"] == 2, out2
