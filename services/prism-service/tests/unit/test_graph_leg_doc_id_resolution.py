"""RED — task e58543b8: brain_search's graph leg must key on docs.id.

``Brain._graph_search``/``_traverse_graph`` (brain_engine.py:2792-2843,
verified live this session) still emit raw ``{"doc_id": entities.file,
"score": 1.0}`` rows. ``entities.file`` is graph.db's own id space (a bare
file path); ``docs.id`` is ``"<source_file>::<entity_name>"`` — the space
BM25, vector, and RRF fusion actually key on (docs table defined at
brain_engine.py:925-936; doc_id constructed as
``f"{filepath}::{name}"`` at brain_engine.py:1829/2216). Measured live on a
real corpus (task af75838d, benchmark commit d9ba9a2d): 0/437 id overlap —
the graph leg contributes nothing to hybrid search today, silently.

These tests pin the correctness fix scoped by this task: the graph leg
must resolve entities onto real docs.id rows, and must never surface a
pseudo-id with no docs row. They are expected to FAIL against the current
``_graph_search``/``_traverse_graph`` implementation and pass once this
task's plan step F (delegate wiring to a resolved implementation such as
``graph_search_ranked``, per stranded task 763ee039) lands.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest


_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _seed_entity(graph_db: str, name: str, file: str, kind: str = "function") -> int:
    conn = sqlite3.connect(graph_db)
    try:
        cur = conn.execute(
            "INSERT INTO entities (name, kind, file, line) VALUES (?, ?, ?, ?)",
            (name, kind, file, 1),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _seed_relationship(graph_db: str, source_id: int, target_id: int,
                        relation: str = "calls") -> None:
    conn = sqlite3.connect(graph_db)
    try:
        conn.execute(
            "INSERT INTO relationships (source_id, target_id, relation) "
            "VALUES (?, ?, ?)",
            (source_id, target_id, relation),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def brain(tmp_path):
    from prism_service.engines.brain_engine import Brain
    return Brain(
        brain_db=str(tmp_path / "brain.db"),
        graph_db=str(tmp_path / "graph.db"),
        scores_db=str(tmp_path / "scores.db"),
    )


def test_traverse_graph_resolves_target_entity_to_docs_id_space(brain, tmp_path):
    """AC-2/AC-3: a structural traversal must key its results on docs.id,
    the "<source_file>::<entity_name>" space — never the bare graph.db
    file path. Currently _traverse_graph returns entities.file raw
    (brain_engine.py:2841), so this fails until the leg resolves onto
    a real docs row."""
    graph_db = str(tmp_path / "graph.db")
    src_id = _seed_entity(graph_db, "AuthHandler", "src/auth.py")
    tgt_id = _seed_entity(graph_db, "TokenStore", "src/store.py")
    _seed_relationship(graph_db, src_id, tgt_id, relation="calls")

    resolved_doc_id = "src/store.py::TokenStore"
    brain._ingest_single(
        resolved_doc_id, "class TokenStore:\n    ...\n",
        source_file="src/store.py", domain="py",
        entity_name="TokenStore", entity_kind="class",
    )

    results = brain._traverse_graph("AuthHandler", "calls", limit=10)

    assert results, "expected at least one traversal result"
    doc_ids = [r["doc_id"] for r in results]
    assert resolved_doc_id in doc_ids, (
        f"graph leg did not resolve onto the real docs.id row "
        f"{resolved_doc_id!r} — got raw graph ids instead: {doc_ids}")
    assert "src/store.py" not in doc_ids, (
        "graph leg returned the bare entities.file path as doc_id — "
        "that id space is not what BM25/vector/RRF key on")


def test_graph_search_token_match_resolves_to_docs_id_space(brain, tmp_path):
    """AC-2/AC-3: the non-structural (token LIKE) branch of _graph_search
    must also resolve onto docs.id, not entities.file
    (brain_engine.py:2808-2812)."""
    graph_db = str(tmp_path / "graph.db")
    _seed_entity(graph_db, "PaymentProcessor", "src/payment.py")

    resolved_doc_id = "src/payment.py::PaymentProcessor"
    brain._ingest_single(
        resolved_doc_id, "def process_payment():\n    ...\n",
        source_file="src/payment.py", domain="py",
        entity_name="PaymentProcessor", entity_kind="function",
    )

    results = brain._graph_search("how does PaymentProcessor work", limit=10)

    assert results, "expected at least one graph-leg result"
    doc_ids = [r["doc_id"] for r in results]
    assert resolved_doc_id in doc_ids, (
        f"graph leg did not resolve the token match onto the real "
        f"docs.id row {resolved_doc_id!r} — got: {doc_ids}")
    assert "src/payment.py" not in doc_ids, (
        "graph leg returned the bare entities.file path as doc_id")


def test_graph_leg_never_returns_a_pseudo_id_with_no_docs_row(brain, tmp_path):
    """AC-4 (correctness half of stranded fix e03e4fd1, task 61666a4f):
    an entity with no matching docs row is a pseudo-id — it can never be
    scored downstream and must not be surfaced as a graph-leg candidate
    at all. Currently _graph_search happily returns it
    (brain_engine.py:2802-2812 has no docs-row check)."""
    graph_db = str(tmp_path / "graph.db")
    # Ghost: present in the graph, no docs row anywhere for its file.
    _seed_entity(graph_db, "GhostWidgetFactory", "src/ghost.py")
    # Real: present in the graph AND has a matching docs row.
    _seed_entity(graph_db, "RealWidgetFactory", "src/real.py")
    brain._ingest_single(
        "src/real.py::RealWidgetFactory", "class RealWidgetFactory:\n    ...\n",
        source_file="src/real.py", domain="py",
        entity_name="RealWidgetFactory", entity_kind="class",
    )

    results = brain._graph_search("WidgetFactory lookup", limit=10)

    doc_ids = {r["doc_id"] for r in results}
    docs_rows = {
        row["id"] for row in brain._brain.execute("SELECT id FROM docs").fetchall()
    }
    pseudo_ids = doc_ids - docs_rows
    assert not pseudo_ids, (
        f"graph leg surfaced pseudo-id(s) with no docs row, which can "
        f"starve a file's real chunk downstream: {pseudo_ids}")
