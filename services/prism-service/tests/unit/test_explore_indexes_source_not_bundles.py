"""Explore indexes real source, never a bundle (task f9e0745e, epic
61821448 "Understand writes the law, the ontology holds it, the code
obeys it").

Measured on the prism project itself (2026-08-26): graph.db carried
24,927 code symbols, of which 24,439 sat under web_dist -- a built,
hashed-filename Vite bundle, never something a human wrote. Root cause:
services/source_service.py's `_INGEST_SKIP_DIRS` already listed
web_dist/web_dist_next, but engines/brain_engine.py kept a PRIVATE copy
of the skip-dir set (`Brain._EXCLUDED_PATH_SEGMENTS`) that was missing
both -- so a file the walker correctly skipped could still pass
`Brain._should_index()` and get indexed anyway wherever that method (not
the walker) was the gate.

This file pins three repairs:
  1. ONE skip list -- every indexer consults source_service's own set,
     never a private copy that can drift out of sync again.
  2. Purge by PATH SEGMENT, never substring -- a stale graph.db row (or a
     stale staged file) under a skipped dir is removed on the next
     ingest, and a real file whose NAME merely contains a skip-dir
     string ("redistribute.py") survives.
  3. The `::__module__` chunk is capped to a 4 KB head slice, so a
     changelog-style module (one string appended to on every release,
     e.g. `__version__.py`) cannot grow past that and out-score every
     real symbol on every search -- the sliding-window tier still
     covers the rest of the file.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


# ---------------------------------------------------------------------
# 1. ONE skip list -- no private duplicate, every indexer agrees
# ---------------------------------------------------------------------

def test_brain_engine_has_no_private_skip_dir_copy():
    """Brain._EXCLUDED_PATH_SEGMENTS must literally BE source_service's
    own _INGEST_SKIP_DIRS object, not a second set someone typed out by
    hand -- that is exactly how web_dist/web_dist_next fell out of sync
    the first time."""
    from prism_service.engines.brain_engine import Brain
    from prism_service.services.source_service import _INGEST_SKIP_DIRS

    assert Brain._EXCLUDED_PATH_SEGMENTS is _INGEST_SKIP_DIRS


def test_should_index_skips_every_bundle_and_dependency_dir(tmp_path):
    """Brain._should_index (the gate brain_engine's own indexers call)
    rejects every path source_service.is_ingest_excluded rejects --
    web_dist, web_dist_next, node_modules, dist, build -- and still
    accepts real source files."""
    from prism_service.engines.brain_engine import Brain

    b = Brain(
        brain_db=str(tmp_path / "brain.db"),
        graph_db=str(tmp_path / "graph.db"),
        scores_db=str(tmp_path / "scores.db"),
    )

    poisoned = [
        "services/prism-service/prism_service/web_dist/assets/index-abc.js",
        "services/prism-service/prism_service/web_dist_next/assets/index-def.js",
        "services/prism-service/node_modules/react/index.js",
        "services/prism-service/dist/bundle.js",
        "services/prism-service/build/out.js",
    ]
    for p in poisoned:
        assert not b._should_index(p), f"{p!r} must not be indexed"

    real = [
        "services/prism-service/prism_service/services/ste.py",
        "services/prism-service/prism_service/web/src/pages/TasksPage.tsx",
    ]
    for p in real:
        assert b._should_index(p), f"{p!r} must be indexed"


# ---------------------------------------------------------------------
# 2. Purge by PATH SEGMENT, never substring
# ---------------------------------------------------------------------

def _make_graph_service(tmp_path):
    from prism_service.services.graph_service import GraphService

    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    graph_db = tmp_path / "graph.db"
    svc = GraphService(project_data_dir=str(proj_dir), graph_db_path=str(graph_db))
    return svc, graph_db


def _seed_entities(graph_db: Path, rows: list[tuple]) -> None:
    """rows: (id, name, kind, file). Builds the base schema exactly as
    production graph.db has it (entities.file/relationships.source_id
    etc.) so purge_excluded's real SQL runs against the real shape."""
    conn = sqlite3.connect(str(graph_db))
    conn.executescript("""
        CREATE TABLE entities (
            id INTEGER PRIMARY KEY, name TEXT, kind TEXT, file TEXT, line INTEGER
        );
        CREATE TABLE relationships (
            source_id INTEGER, target_id INTEGER, relation TEXT
        );
    """)
    conn.executemany(
        "INSERT INTO entities (id, name, kind, file) VALUES (?, ?, ?, ?)", rows,
    )
    conn.commit()
    conn.close()


def test_purge_excluded_removes_entities_under_a_skipped_dir(tmp_path):
    """A stale web_dist entity (and the relationship touching it) is
    removed; a real source entity is untouched."""
    svc, graph_db = _make_graph_service(tmp_path)
    _seed_entities(graph_db, [
        (1, "bundle_fn", "function",
         "services/prism-service/prism_service/web_dist/assets/index-abc.js"),
        (2, "real_fn", "function", "services/prism-service/prism_service/services/ste.py"),
    ])
    conn = sqlite3.connect(str(graph_db))
    conn.execute(
        "INSERT INTO relationships (source_id, target_id, relation) VALUES (1, 2, 'calls')"
    )
    conn.commit()
    conn.close()

    result = svc.purge_excluded()
    assert result["entities_removed"] == 1
    assert result["relationships_removed"] == 1

    conn = sqlite3.connect(str(graph_db))
    names = {r[0] for r in conn.execute("SELECT name FROM entities").fetchall()}
    rel_count = conn.execute("SELECT COUNT(*) FROM relationships").fetchone()[0]
    conn.close()
    assert names == {"real_fn"}
    assert rel_count == 0


def test_purge_excluded_never_matches_a_skip_word_inside_a_filename(tmp_path):
    """A real file whose NAME merely contains a skip-dir string --
    redistribute.py contains "dist" -- must survive. Segment match only."""
    svc, graph_db = _make_graph_service(tmp_path)
    _seed_entities(graph_db, [
        (1, "redistribute_fn", "function",
         "services/prism-service/prism_service/services/redistribute.py"),
    ])

    result = svc.purge_excluded()
    assert result["entities_removed"] == 0

    conn = sqlite3.connect(str(graph_db))
    names = {r[0] for r in conn.execute("SELECT name FROM entities").fetchall()}
    conn.close()
    assert names == {"redistribute_fn"}


def test_purge_excluded_unstages_the_matching_file_so_rebuild_cannot_resurrect_it(tmp_path):
    """rebuild() imports graphify's graph.json as a FULL snapshot of
    whatever is staged -- a file left in graphify-src keeps re-emitting
    its entities on every rebuild even after the walker starts skipping
    it. purge_excluded must delete the staged copy too."""
    svc, graph_db = _make_graph_service(tmp_path)
    bundle_rel = "prism_service/web_dist/assets/index-abc.js"
    real_rel = "prism_service/services/ste.py"
    svc.stage_doc(bundle_rel, "// built bundle content")
    svc.stage_doc(real_rel, "def f(): pass")

    staging_dir = Path(svc._staging_dir)
    assert (staging_dir / bundle_rel).exists()
    assert (staging_dir / real_rel).exists()

    result = svc.purge_excluded()
    assert result["unstaged"] == 1
    assert not (staging_dir / bundle_rel).exists()
    assert (staging_dir / real_rel).exists()


def test_ontology_projection_scopes_code_kinds_to_real_paths(tmp_path, monkeypatch):
    """ontology_prototype_projection._code_graph_rows never counts a
    web_dist entity as a code-graph symbol -- the ontology's classes must
    reflect real source, not a bundle (this is the "155 Code nodes, ZERO
    edges" root cause from the other half of task f9e0745e)."""
    import uuid
    from prism_service.config import project_data_dir
    from prism_service.project_context import get_project
    from prism_service.services import ontology_prototype_projection as proj

    pid = f"purge-scope-{uuid.uuid4().hex[:8]}"
    get_project(pid)
    graph_db = project_data_dir(pid) / "graph.db"
    _seed_entities(graph_db, [
        (1, "bundle_fn", "function",
         "prism_service/web_dist/assets/index-abc.js"),
        (2, "real_fn", "function", "prism_service/services/ste.py"),
    ])

    rows = proj._code_graph_rows(pid)
    names = {r["name"] for r in rows}
    assert names == {"real_fn"}


# ---------------------------------------------------------------------
# 3. Cap the changelog module: the `::__module__` chunk is bounded
# ---------------------------------------------------------------------

def _brain(tmp_path):
    from prism_service.engines.brain_engine import Brain

    return Brain(
        brain_db=str(tmp_path / "brain.db"),
        graph_db=str(tmp_path / "graph.db"),
        scores_db=str(tmp_path / "scores.db"),
    )


def test_module_chunk_is_capped_to_4kb_and_windows_still_cover_the_file(tmp_path):
    """A 200 KB changelog-style module (one huge top-level string, no
    functions or classes to carve out) must not produce a 200 KB
    `::__module__` doc -- it is capped to <= 4 KB, and the sliding-window
    tier still carries the full content so nothing is actually lost."""
    b = _brain(tmp_path)

    filler = "\n".join(f'"changelog entry number {i} explains a real fix"'
                        for i in range(6000))
    content = (
        'PRISM_VERSION = "9.9.9"\n'
        'PRISM_VERSION_NOTES = (\n' + filler + '\n)\n'
    )
    assert len(content.encode("utf-8")) > 100_000

    chunks = b._chunk_source_file(
        "services/prism-service/prism_service/__version__.py", content,
    )

    module_chunks = [c for c in chunks if c["entity_kind"] == "module"]
    assert module_chunks, "expected at least one __module__ chunk"
    for c in module_chunks:
        assert len(c["content"].encode("utf-8")) <= 4096, (
            f"{c['doc_id']} is {len(c['content'].encode('utf-8'))} bytes, "
            "expected <= 4096")

    window_chunks = [c for c in chunks if c["entity_kind"] == "window"]
    assert window_chunks, "expected sliding windows to cover the full file"
    covered = sum(len(c["content"]) for c in window_chunks)
    # Windows overlap by 256 chars each, so total covered content exceeds
    # the source length; the point is they are NOT also capped to 4 KB.
    assert covered > len(content) * 0.9


def test_module_chunk_under_the_cap_is_left_untouched(tmp_path):
    """A small, real module docstring/body is not truncated -- the cap
    only ever bites a chunk that is actually oversized."""
    b = _brain(tmp_path)
    content = (
        '"""A small real module."""\n\n'
        "TOP_LEVEL_CONSTANT = 1\n"
    )
    chunks = b._chunk_source_file("services/prism-service/prism_service/tiny.py", content)
    module_chunks = [c for c in chunks if c["entity_kind"] == "module"]
    assert module_chunks
    for c in module_chunks:
        assert "TOP_LEVEL_CONSTANT" in c["content"]
