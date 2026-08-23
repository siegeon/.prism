"""Regression test for the brain-bloat incident, 2026-08-22.

index_doc() only purges stale rows for the exact source_file it is told
to re-index. A file that disappears or gets renamed out from under the
indexer (e.g. a hashed build-output filename that changes every build)
is never told about again, so its old chunks live forever — this is
what let ~500MB/project of stale web_dist_next chunks accumulate before
the web_dist_next skip-dir fix even applied. prune_orphaned_code_docs()
is the belt-and-suspenders cleanup: given the current full set of
on-disk source paths, delete any domain='code' doc whose source_file
fell outside it.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _service(tmp_path: Path):
    from prism_service.services.brain_service import BrainService
    return BrainService(
        brain_db=str(tmp_path / "brain.db"),
        graph_db=str(tmp_path / "graph.db"),
        scores_db=str(tmp_path / "scores.db"),
    )


def _doc_source_files(svc) -> set:
    conn = svc._brain._brain
    rows = conn.execute("SELECT DISTINCT source_file FROM docs").fetchall()
    return {r[0] for r in rows}


def test_prune_deletes_rows_for_files_no_longer_on_disk(tmp_path):
    svc = _service(tmp_path)
    svc.index_doc("app.py", "def live(): pass\n")
    svc.index_doc(
        "assets/index-OLDHASH123.js", "console.log('stale build')",
    )

    assert _doc_source_files(svc) == {
        "app.py", "assets/index-OLDHASH123.js",
    }

    deleted = svc.prune_orphaned_code_docs({"app.py"})

    assert deleted > 0
    assert _doc_source_files(svc) == {"app.py"}


def test_prune_is_noop_when_everything_is_live(tmp_path):
    svc = _service(tmp_path)
    svc.index_doc("app.py", "def live(): pass\n")

    deleted = svc.prune_orphaned_code_docs({"app.py"})

    assert deleted == 0
    assert _doc_source_files(svc) == {"app.py"}


def test_prune_removes_fts_and_vec_rows_too(tmp_path):
    """A plain DELETE FROM docs must not leave the FTS5 shadow tables or
    docs_vec pointing at rows that no longer exist."""
    svc = _service(tmp_path)
    svc.index_doc(
        "assets/index-OLDHASH123.js", "console.log('stale build')",
    )
    conn = svc._brain._brain
    (doc_id,) = conn.execute("SELECT id FROM docs").fetchone()

    svc.prune_orphaned_code_docs(set())

    assert conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0] == 0
    fts_hits = conn.execute(
        "SELECT COUNT(*) FROM docs_fts WHERE id = ?", (doc_id,),
    ).fetchone()[0]
    assert fts_hits == 0
    vec_hits = conn.execute(
        "SELECT COUNT(*) FROM docs_vec WHERE doc_id = ?", (doc_id,),
    ).fetchone()[0]
    assert vec_hits == 0
