"""OntologyStore — persisted ontology tables (task 15c06516).

Real sqlite rows in the project's OWN data directory, beside brain.db /
graph.db / tasks.db (config.project_data_dir — the same resolver okf_host.py
and the task/memory/graph services all use), as ontology.db. Populated by
services/ontology_prototype_projection.rebuild(); NEVER computed on the read
path — api/okf.py's ontology routes are a thin SELECT over these tables.

Tables (mx-2d14b0 mapping): ontology_classes (graph entity kinds / catalog
groupings), ontology_instances (real rows), ontology_properties (graph edge
kinds / scalars), ontology_axioms (arc_governance principle names, 'quiet'
until sibling task c1d0ee70 wires violation detection).
"""

from __future__ import annotations

import sqlite3
from typing import Any

from prism_service.config import project_data_dir
from prism_service.services import sqlite_db


class OntologyStore:
    def __init__(self, project: str) -> None:
        self._db_path = project_data_dir(project) / "ontology.db"
        # sqlite chokepoint (timeout + WAL + busy_timeout), never bare connect.
        self._conn = sqlite_db.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS ontology_classes (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'class',
                parent_id TEXT,
                description TEXT DEFAULT '',
                instance_count INTEGER DEFAULT 0,
                source TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS ontology_instances (
                id TEXT PRIMARY KEY,
                class_id TEXT NOT NULL,
                label TEXT NOT NULL,
                ref TEXT DEFAULT '',
                provenance TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS ontology_properties (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                domain_class TEXT,
                range_class TEXT,
                kind TEXT NOT NULL DEFAULT 'property'
            );
            CREATE TABLE IF NOT EXISTS ontology_axioms (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                state TEXT NOT NULL DEFAULT 'quiet',
                detail TEXT DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_ontology_instances_class
                ON ontology_instances(class_id);
            """
        )
        self._conn.commit()

    def replace_all(
        self,
        classes: list[dict[str, Any]],
        instances: list[dict[str, Any]],
        properties: list[dict[str, Any]],
        axioms: list[dict[str, Any]],
    ) -> None:
        """Atomic full-table swap — the projection's only write path (rows
        are PERSISTED, never computed at request time; every read below is
        a plain SELECT)."""
        c = self._conn
        c.execute("DELETE FROM ontology_instances")
        c.execute("DELETE FROM ontology_classes")
        c.execute("DELETE FROM ontology_properties")
        c.execute("DELETE FROM ontology_axioms")
        c.executemany(
            "INSERT INTO ontology_classes "
            "(id,name,kind,parent_id,description,instance_count,source) "
            "VALUES (?,?,?,?,?,?,?)",
            [(x["id"], x["name"], x.get("kind", "class"), x.get("parent_id"),
              x.get("description", ""), x.get("instance_count", 0),
              x.get("source", "")) for x in classes],
        )
        c.executemany(
            "INSERT INTO ontology_instances (id,class_id,label,ref,provenance) "
            "VALUES (?,?,?,?,?)",
            [(x["id"], x["class_id"], x["label"], x.get("ref", ""),
              x.get("provenance", "")) for x in instances],
        )
        c.executemany(
            "INSERT INTO ontology_properties (id,name,domain_class,range_class,kind) "
            "VALUES (?,?,?,?,?)",
            [(x["id"], x["name"], x.get("domain_class"), x.get("range_class"),
              x.get("kind", "property")) for x in properties],
        )
        c.executemany(
            "INSERT INTO ontology_axioms (id,name,description,state,detail) "
            "VALUES (?,?,?,?,?)",
            [(x["id"], x["name"], x.get("description", ""),
              x.get("state", "quiet"), x.get("detail", "")) for x in axioms],
        )
        c.commit()

    def is_empty(self) -> bool:
        row = self._conn.execute("SELECT COUNT(*) n FROM ontology_classes").fetchone()
        return (row["n"] if row else 0) == 0

    def list_classes(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT id,name,kind,parent_id,description,instance_count,source "
            "FROM ontology_classes ORDER BY source, name"
        ).fetchall()
        return [dict(r) for r in rows]

    def list_instances(self, class_id: str, limit: int = 200) -> list[dict]:
        rows = self._conn.execute(
            "SELECT id,class_id,label,ref,provenance FROM ontology_instances "
            "WHERE class_id=? ORDER BY label LIMIT ?", (class_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_properties(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT id,name,domain_class,range_class,kind "
            "FROM ontology_properties ORDER BY name"
        ).fetchall()
        return [dict(r) for r in rows]

    def list_axioms(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT id,name,description,state,detail FROM ontology_axioms ORDER BY name"
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        self._conn.close()
