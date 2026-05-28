"""Tests for v6.1.5 PageRank centrality on entities (Ultimate Graph slice 3)."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def test_pagerank_ordering_hub_beats_leaf():
    """Hub with three in-edges should out-rank a leaf with one.

    Topology: a, b, c all point at hub; a also points at leaf. Hub
    therefore accumulates b's and c's mass plus half of a's; leaf only
    sees half of a's. Hub > leaf > {a, b, c}.
    """
    from prism_service.services.graph_service import _compute_pagerank

    nodes = [{"id": "hub"}, {"id": "a"}, {"id": "b"}, {"id": "c"}, {"id": "leaf"}]
    links = [
        {"source": "a", "target": "hub"},
        {"source": "a", "target": "leaf"},
        {"source": "b", "target": "hub"},
        {"source": "c", "target": "hub"},
    ]
    ranks = _compute_pagerank(nodes, links)
    assert set(ranks.keys()) == {"hub", "a", "b", "c", "leaf"}
    assert ranks["hub"] > ranks["leaf"], ranks
    assert ranks["leaf"] > ranks["a"], ranks
    total = sum(ranks.values())
    assert abs(total - 1.0) < 1e-6, total


def test_pagerank_empty_graph_returns_empty():
    from prism_service.services.graph_service import _compute_pagerank
    assert _compute_pagerank([], []) == {}
    assert _compute_pagerank([{"id": ""}], []) == {}


def test_pagerank_dangling_redistributes_mass():
    """All nodes dangling (no edges) — uniform 1/n score."""
    from prism_service.services.graph_service import _compute_pagerank

    nodes = [{"id": "x"}, {"id": "y"}, {"id": "z"}]
    ranks = _compute_pagerank(nodes, [])
    assert len(ranks) == 3
    for v in ranks.values():
        assert abs(v - 1.0 / 3.0) < 1e-6


def test_schema_migration_adds_centrality(tmp_path: Path):
    """_graph_schema_migrations is idempotent and lands centrality."""
    from prism_service.services.graph_service import _graph_schema_migrations

    db = tmp_path / "graph.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        "CREATE TABLE entities ("
        "  id INTEGER PRIMARY KEY, name TEXT, kind TEXT,"
        "  file TEXT, line INTEGER,"
        "  UNIQUE(name, file)"
        ");"
        "CREATE TABLE relationships ("
        "  id INTEGER PRIMARY KEY,"
        "  source_id INTEGER, target_id INTEGER, relation TEXT"
        ");"
        "CREATE TABLE communities (id INTEGER PRIMARY KEY, label TEXT,"
        "  size INTEGER, top_files TEXT, top_entities TEXT, summary TEXT);"
    )
    _graph_schema_migrations(conn)
    # second call is a no-op (idempotent)
    _graph_schema_migrations(conn)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(entities)").fetchall()}
    assert "centrality" in cols
    # default 0 on a new row
    conn.execute("INSERT INTO entities (name, file) VALUES ('foo', 'a.py')")
    conn.commit()
    row = conn.execute("SELECT centrality FROM entities WHERE name='foo'").fetchone()
    assert row[0] == 0.0
    conn.close()


def test_top_central_entities_orders_desc(tmp_path: Path, monkeypatch):
    """top_central_entities returns rows sorted by centrality desc, skips zero."""
    from prism_service.services.graph_service import (
        GraphService,
        _graph_schema_migrations,
    )

    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    db = tmp_path / "graph.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        "CREATE TABLE entities ("
        "  id INTEGER PRIMARY KEY, name TEXT, kind TEXT,"
        "  file TEXT, line INTEGER,"
        "  UNIQUE(name, file)"
        ");"
        "CREATE TABLE relationships ("
        "  id INTEGER PRIMARY KEY,"
        "  source_id INTEGER, target_id INTEGER, relation TEXT"
        ");"
        "CREATE TABLE communities (id INTEGER PRIMARY KEY, label TEXT,"
        "  size INTEGER, top_files TEXT, top_entities TEXT, summary TEXT);"
    )
    _graph_schema_migrations(conn)
    conn.executemany(
        "INSERT INTO entities (name, kind, file, line, centrality) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            ("hub", "func", "a.py", 10, 0.42),
            ("mid", "func", "b.py", 20, 0.13),
            ("leaf", "func", "c.py", 30, 0.05),
            ("orphan", "func", "d.py", 40, 0.0),
        ],
    )
    conn.commit()
    conn.close()

    svc = GraphService(project_data_dir=str(proj_dir), graph_db_path=str(db))
    rows = svc.top_central_entities(limit=10)
    assert [r["name"] for r in rows] == ["hub", "mid", "leaf"]
    assert rows[0]["centrality"] > rows[1]["centrality"] > rows[2]["centrality"]
    assert rows[0]["file"] == "a.py"
    assert rows[0]["line"] == 10
