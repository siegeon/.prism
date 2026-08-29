"""RED tests for brain.db growth: FTS orphans, no short-circuit, pinned WAL.

trace (task 72ccaf94-78f8-4a6d-add4-b457776fe489, HEAD 65fbd175):

OBSERVED 2026-08-28 (OS ops): ~/.prism/projects held 18 GB for ~50 MB of
real content. think-shift/brain.db was 2.9 GB for 1,419 Documents /
3.5 MB of text; docs_fts_data carried 306,959 segment rows and a 1.7 GB
freelist. prism/brain.db carried 337 more docs_fts rows than docs rows
and a 750 MB -wal that never truncated.

Four root causes, and the measurement that pins each one:

  1. `_ingest_single` (brain_engine.py:2309) writes INSERT OR REPLACE.
     The REPLACE conflict deletes the old `docs` row WITHOUT firing
     `docs_fts_ad`, because `PRAGMA recursive_triggers` defaults to OFF
     (sqlite_db.connect, sqlite_db.py:41-43, sets three pragmas and not
     this one). The superseded index entry stays behind at a rowid no
     `docs` row occupies. Measured: the stale term still returns 1 hit.
  2. `BrainService.index_doc` (brain_service.py:372) has no content-hash
     short-circuit -- it DELETEs and re-INSERTs every doc of a file on
     every pass, even byte-identical ones, so the drift loop rewrites an
     unchanged tree forever.
  3. `sqlite_maint.checkpoint_db` (sqlite_maint.py:49) runs no FTS merge,
     so docs_fts segments accumulate unbounded.
  4. `checkpoint_db` discards the row `PRAGMA wal_checkpoint(TRUNCATE)`
     returns. Measured at HEAD with a reader snapshot held on the file:
     the pragma returns (busy=1, 59, 59) and leaves the -wal at 243,112
     bytes, and `checkpoint_db` still returns True. With the reader
     released it returns (0, 0, 0) and the -wal is 0 bytes. Only the
     HONESTY half is in scope here; releasing the live readers is filed
     as a follow-up child.

Two detectors this file deliberately does NOT use, both measured:

  * `INSERT INTO docs_fts(docs_fts, rank) VALUES('integrity-check', 1)`
    RAISES "database disk image is malformed" on a CLEAN, zero-orphan
    database that holds one CamelCase token, because the triggers index
    `expand_identifiers(new.content)` (brain_engine.py:968) while
    integrity-check recomputes from the RAW `docs.content` column. It is
    red at HEAD and stays red after the fix, so it is a false alarm.
  * `count(docs_fts) == count(docs)` cannot see an orphan at all: an
    external-content FTS5 count scans the content table, so it reads
    equal while the orphan is present.

What IS used: a literal hit count for a superseded term, read from the
INDEX (`docs_fts MATCH`), which is where an orphan lives.

Every test here runs against a REAL on-disk database that has taken a
genuine REPLACE conflict -- never an in-memory fixture that never
conflicted.

Covers AC-1, AC-2, AC-3, AC-4, AC-6, AC-7, AC-11 and AC-12. AC-13's
live scan is the `--live-scan` entry point at the bottom, behind an
`if __name__ == "__main__"` guard pytest never collects.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

# Requirement 11: the limit is read from PRISM_SQLITE_JOURNAL_SIZE_LIMIT
# with a 64 MB default. SQLite returns the APPLIED value, so a rejected
# value reads back as the old one -- which is why AC-2 reads it back.
_DEFAULT_JOURNAL_SIZE_LIMIT = 64 * 1024 * 1024


def _make_brain(db_dir: Path):
    from prism_service.engines.brain_engine import Brain
    db_dir.mkdir(parents=True, exist_ok=True)
    return Brain(
        brain_db=str(db_dir / "brain.db"),
        graph_db=str(db_dir / "graph.db"),
        scores_db=str(db_dir / "scores.db"),
    )


def _close(brain) -> None:
    for attr in ("_brain", "_graph", "_scores"):
        conn = getattr(brain, attr, None)
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def _hits(conn: sqlite3.Connection, term: str) -> int:
    """Hit count for ``term`` read from the INDEX, not the content table.

    The REPLACE overwrote the content column, so sampling terms from
    `docs` cannot see a superseded entry; `docs_fts MATCH` can.
    """
    return conn.execute(
        "SELECT count(*) FROM docs_fts WHERE docs_fts MATCH ?", (term,)
    ).fetchone()[0]


def _tracing_connect(monkeypatch, sink: list[str]):
    """Route every sqlite_db.connect through a statement trace.

    brain_engine._connect (brain_engine.py:891) and sqlite_maint both go
    through this one chokepoint, so patching it captures what was really
    EXECUTED -- AC-4 and AC-6 need executed statements, not source text.
    """
    from prism_service.services import sqlite_db

    real = sqlite_db.connect

    def _connect(path, **kwargs):
        conn = real(path, **kwargs)
        conn.set_trace_callback(lambda stmt: sink.append(stmt))
        return conn

    monkeypatch.setattr(sqlite_db, "connect", _connect)
    return sink


# ---------------------------------------------------------------------
# AC-1 -- a re-index of a changed Document leaves no stale FTS entry
# ---------------------------------------------------------------------

def test_stale_term_is_gone_after_reindex(tmp_path):
    brain = _make_brain(tmp_path / "proj")
    try:
        conn = brain._brain
        brain._ingest_single("doc::one", "alpha zebracorn GammaRay beta")
        assert _hits(conn, "zebracorn") == 1, (
            "precondition: the unique term was never indexed"
        )

        brain._ingest_single("doc::one", "alpha delta GammaRay beta")

        # Literal zero, never count(docs_fts) == count(docs): an
        # external-content count is equal by construction.
        assert _hits(conn, "zebracorn") == 0, (
            "the superseded term is still in docs_fts -- the INSERT OR "
            "REPLACE conflict deleted the docs row without firing "
            "docs_fts_ad, so the index entry is orphaned at a rowid no "
            "docs row occupies"
        )
    finally:
        _close(brain)


# ---------------------------------------------------------------------
# AC-2 -- every connection from sqlite_db.connect carries both pragmas
# ---------------------------------------------------------------------

def test_connect_sets_the_two_pragmas(tmp_path):
    from prism_service.services import sqlite_db

    conn = sqlite_db.connect(str(tmp_path / "x.db"))
    try:
        assert conn.execute("PRAGMA recursive_triggers").fetchone()[0] == 1, (
            "recursive_triggers is OFF, so a REPLACE conflict never fires "
            "docs_fts_ad and every changed document leaves an orphan"
        )
        applied = conn.execute("PRAGMA journal_size_limit").fetchone()[0]
        assert applied == _DEFAULT_JOURNAL_SIZE_LIMIT, (
            f"journal_size_limit read back as {applied}, not the 64 MB "
            "default -- SQLite returns the APPLIED value, so a rejected "
            "setting reads back as the old one (-1 means unset)"
        )
    finally:
        conn.close()


def test_pragmas_honour_the_env_override(tmp_path, monkeypatch):
    from prism_service.services import sqlite_db

    monkeypatch.setenv("PRISM_SQLITE_JOURNAL_SIZE_LIMIT", "32768")
    conn = sqlite_db.connect(str(tmp_path / "y.db"))
    try:
        assert conn.execute("PRAGMA journal_size_limit").fetchone()[0] == 32768
    finally:
        conn.close()


# ---------------------------------------------------------------------
# AC-3 -- index_doc does no write work for byte-identical content
# ---------------------------------------------------------------------

def test_index_doc_skips_unchanged_content(tmp_path):
    from prism_service.services.brain_service import BrainService

    d = tmp_path / "proj"
    d.mkdir(parents=True, exist_ok=True)
    svc = BrainService(
        brain_db=str(d / "brain.db"),
        graph_db=str(d / "graph.db"),
        scores_db=str(d / "scores.db"),
    )
    if not getattr(svc, "_available", False) or svc._brain is None:
        pytest.skip("Brain engine unavailable in this environment")
    conn = svc._brain._brain
    content = "def handler():\n    return 'unchanged'\n"

    svc.index_doc("mod.py", content)
    before_rowids = conn.execute(
        "SELECT rowid FROM docs ORDER BY rowid"
    ).fetchall()

    def _vec_rows() -> int:
        try:
            return conn.execute("SELECT count(*) FROM docs_vec").fetchone()[0]
        except sqlite3.Error:
            return -1

    before_vecs = _vec_rows()

    status = svc.index_doc("mod.py", content)

    after_rowids = conn.execute(
        "SELECT rowid FROM docs ORDER BY rowid"
    ).fetchall()
    assert [tuple(r) for r in after_rowids] == [
        tuple(r) for r in before_rowids
    ], (
        "index_doc rewrote the rows for byte-identical content -- the "
        "DELETE at brain_service.py:412 ran and every doc got a new rowid"
    )
    assert _vec_rows() == before_vecs, "embeddings were recomputed"
    # The skip must hand back a REAL doc id. It used to return
    # "skipped:<id>", and brain_index_doc passed that straight out as
    # doc_id (mcp/tools.py builds {"indexed": True, "doc_id": ...}), so a
    # caller -- brain_search_feedback among them -- received an id that
    # matches no row. The skip is an internal write optimisation.
    assert not str(status).startswith("skipped:"), (
        f"index_doc returned a malformed doc id: {status!r}"
    )
    resolved = conn.execute(
        "SELECT count(*) FROM docs WHERE id = ?", (status,)
    ).fetchone()[0]
    assert resolved == 1, (
        f"index_doc returned {status!r}, which resolves to no docs row"
    )


# ---------------------------------------------------------------------
# AC-4 -- the FTS merge runs BEFORE the WAL checkpoint, never after
# ---------------------------------------------------------------------

def test_optimize_before_checkpoint_in_the_trace(tmp_path, monkeypatch):
    from prism_service.services import sqlite_maint

    brain = _make_brain(tmp_path / "proj")
    brain._ingest_single("doc::one", "alpha GammaRay beta")
    brain._brain.commit()
    db_path = brain._brain_db_path
    _close(brain)

    trace: list[str] = []
    _tracing_connect(monkeypatch, trace)
    assert sqlite_maint.checkpoint_db(db_path) is True

    lowered = [s.lower() for s in trace]
    merge_at = next(
        (i for i, s in enumerate(lowered)
         if "docs_fts" in s and "merge" in s), None)
    ckpt_at = next(
        (i for i, s in enumerate(lowered) if "wal_checkpoint" in s), None)
    assert merge_at is not None, (
        "no incremental docs_fts merge statement was executed at all; "
        f"trace was {trace}"
    )
    assert ckpt_at is not None, "no wal_checkpoint statement was executed"
    assert merge_at < ckpt_at, (
        "the FTS merge ran AFTER the checkpoint, so its own write undoes "
        "the TRUNCATE fold -- the same ordering trap the comment at "
        "sqlite_maint.py:60-64 already records for PRAGMA optimize"
    )
    assert not any("'optimize'" in s for s in lowered), (
        "a full docs_fts(docs_fts) VALUES('optimize') is on the routine "
        "path; only the bounded VALUES('merge', N) form is in scope"
    )


def test_merge_tolerates_a_db_with_no_docs_fts_table(tmp_path):
    from prism_service.services import sqlite_maint

    p = tmp_path / "plain.db"
    conn = sqlite3.connect(str(p))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (a TEXT)")
    conn.execute("INSERT INTO t VALUES ('x')")
    conn.commit()
    conn.close()

    assert sqlite_maint.checkpoint_db(p) is True, (
        "the new merge statement broke checkpointing for a store that "
        "carries no FTS index -- most of the per-project stores"
    )


# ---------------------------------------------------------------------
# AC-6 -- an already-orphaned database self-heals once, and only once
# ---------------------------------------------------------------------

def _seed_orphan(db_dir: Path) -> None:
    """Build a database that has taken a GENUINE REPLACE conflict.

    recursive_triggers is forced OFF on the seeding connection so the
    orphan is reproducible even after Requirement 1 turns the pragma on
    for every production connection.
    """
    brain = _make_brain(db_dir)
    try:
        brain._ingest_single("doc::one", "alpha zebracorn GammaRay beta")
        brain._brain.commit()
        conn = brain._brain
        conn.execute("PRAGMA recursive_triggers=OFF")
        conn.execute(
            "INSERT OR REPLACE INTO docs (id, content, content_hash) "
            "VALUES (?, ?, ?)",
            ("doc::one", "alpha delta GammaRay beta", "seeded"),
        )
        conn.commit()
        assert _hits(conn, "zebracorn") == 1, (
            "precondition: the seed did not produce an orphan"
        )
    finally:
        _close(brain)


def test_self_heal_clears_the_orphan_once(tmp_path, monkeypatch):
    d = tmp_path / "proj"
    _seed_orphan(d)

    first: list[str] = []
    _tracing_connect(monkeypatch, first)
    brain = _make_brain(d)
    try:
        conn = brain._brain
        assert _hits(conn, "zebracorn") == 0, (
            "the pre-existing orphan survived the first open -- no code "
            "path at HEAD can remove an index entry whose rowid no docs "
            "row occupies"
        )
        # Identifier expansion must SURVIVE the heal. 'Gamma' exists only
        # because expand_identifiers split 'GammaRay'; VALUES('rebuild')
        # reads the raw content column and deletes it (measured: hits
        # fall 2 -> 0), which is why rebuild is forbidden here.
        assert _hits(conn, "GammaRay") >= 1
        assert _hits(conn, "Gamma") >= 1, (
            "the heal destroyed identifier expansion -- it read the raw "
            "docs.content column instead of re-inserting through "
            "expand_identifiers"
        )
        marker = [
            r[0] for r in conn.execute("SELECT key FROM index_meta").fetchall()
        ]
        assert any("heal" in str(k) for k in marker), (
            f"no self-heal marker was persisted in index_meta: {marker}"
        )
    finally:
        _close(brain)

    joined = " ".join(first).lower()
    assert "'rebuild'" not in joined, (
        "the heal executed docs_fts(docs_fts) VALUES('rebuild'), which "
        "destroys every expand_identifiers split token"
    )
    assert "delete-all" in joined, (
        "the heal never ran VALUES('delete-all'), the only mechanism "
        "measured to empty the index without reading docs.content"
    )

    second: list[str] = []
    _tracing_connect(monkeypatch, second)
    brain2 = _make_brain(d)
    try:
        assert _hits(brain2._brain, "zebracorn") == 0
    finally:
        _close(brain2)
    assert "delete-all" not in " ".join(second).lower(), (
        "the marker did not stop a second repair pass -- the heal runs "
        "on every open"
    )


# ---------------------------------------------------------------------
# AC-7 -- repeated re-indexing does not grow the store without bound
# ---------------------------------------------------------------------

# FIXTURE-SENSITIVE, and the fixture is named on purpose. Measured over
# 25 re-indexes of ONE changing document: at 20 lines HEAD is already
# 1.71x and inside the bound, at 100 lines HEAD is 4.29x and the fixed
# build 1.71x, at 400 lines HEAD is 6.69x. 100 lines is the fixture.
# MEASURED, 2026-08-29. The earlier shape (100 lines, 25 revisions) could
# not go red: the store's fixed cost is ~540 pages, so 25 orphan copies of a
# 100-line document moved it 548 -> 561 pages, 1.02x against a 2.00x bound,
# and the AC passed at the tests-only base commit 927d35c5 -- which this
# task's own stop_if forbids. Orphan mass has to clear the fixed cost, so
# the document and the revision count both grow. Measured on this fixture,
# three trials each, deterministic:
#   base commit 927d35c5 (unfixed): 717 -> 2901 pages, 4.05x  RED
#   this branch    (fixed)        : 717 ->  890 pages, 1.24x  GREEN
_GROWTH_FIXTURE_LINES = 8000
_GROWTH_FIXTURE_REVISIONS = 80


def _fixture_content(n: int) -> str:
    return "\n".join(
        f"def handler_{i}():  # {'pad' * 12} revision {n}" 
        for i in range(_GROWTH_FIXTURE_LINES)
    )


def _page_count(path: str) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("PRAGMA page_count").fetchone()[0]
    finally:
        conn.close()


def test_bounded_growth_over_repeated_reindex(tmp_path):
    from prism_service.services import sqlite_maint

    d = tmp_path / "proj"
    brain = _make_brain(d)
    db_path = brain._brain_db_path
    try:
        brain._ingest_single("doc::one", _fixture_content(0))
        brain._brain.commit()
    finally:
        _close(brain)
    sqlite_maint.checkpoint_data_dir(d)
    baseline = _page_count(db_path)

    brain = _make_brain(d)
    try:
        for n in range(1, _GROWTH_FIXTURE_REVISIONS + 1):
            brain._ingest_single("doc::one", _fixture_content(n))
        brain._brain.commit()
    finally:
        _close(brain)
    sqlite_maint.checkpoint_data_dir(d)

    grown = _page_count(db_path)
    assert grown <= 2 * baseline, (
        f"the store grew to {grown} pages from a {baseline}-page baseline "
        f"({grown / max(baseline, 1):.2f}x) over "
        f"{_GROWTH_FIXTURE_REVISIONS} re-indexes of one "
        f"{_GROWTH_FIXTURE_LINES}-line document -- every re-index leaves "
        "an orphan behind and no short-circuit fires (unfixed: 4.05x)"
    )
    wal = Path(db_path + "-wal")
    wal_size = wal.stat().st_size if wal.exists() else 0
    limit = int(os.environ.get(
        "PRISM_SQLITE_JOURNAL_SIZE_LIMIT", _DEFAULT_JOURNAL_SIZE_LIMIT))
    assert wal_size <= limit, f"-wal held {wal_size} bytes, over {limit}"


# ---------------------------------------------------------------------
# AC-11(a) -- a checkpoint that folded nothing is not reported as success
# ---------------------------------------------------------------------

def _grow_wal(path: Path, rows: int = 400) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE IF NOT EXISTS t (a TEXT)")
    conn.executemany(
        "INSERT INTO t VALUES (?)", [("x" * 400,) for _ in range(rows)])
    conn.commit()
    return conn


def test_checkpoint_honesty_against_a_held_reader(tmp_path, caplog):
    import logging
    from prism_service.services import sqlite_maint

    p = tmp_path / "brain.db"
    writer = _grow_wal(p)

    reader = sqlite3.connect(str(p))
    reader.execute("BEGIN")
    reader.execute("SELECT count(*) FROM t").fetchone()
    writer.execute("INSERT INTO t VALUES ('after-snapshot')")
    writer.commit()
    try:
        with caplog.at_level(logging.WARNING):
            result = sqlite_maint.checkpoint_db(p)
        assert result is False, (
            "checkpoint_db reported success while the TRUNCATE checkpoint "
            "was downgraded by a held read snapshot and folded nothing -- "
            "measured at HEAD: the pragma returns busy=1 and the -wal "
            "keeps its full size, and checkpoint_db returns True anyway"
        )
        assert str(p) in caplog.text, (
            "the degraded checkpoint was not logged with the db path"
        )
    finally:
        reader.close()
        writer.close()

    assert sqlite_maint.checkpoint_db(p) is True, (
        "with no reader held the checkpoint completes, so checkpoint_db "
        "must still report True -- the honesty branch must not turn a "
        "succeeding checkpoint into a reported failure"
    )


# ---------------------------------------------------------------------
# AC-12 -- journal_size_limit is load-bearing on the PASSIVE path
# ---------------------------------------------------------------------

def test_journal_size_limit_enforced_on_passive_checkpoint(
        tmp_path, monkeypatch):
    from prism_service.services import sqlite_db

    limit = 32768
    monkeypatch.setenv("PRISM_SQLITE_JOURNAL_SIZE_LIMIT", str(limit))
    p = tmp_path / "brain.db"

    # The PRODUCTION path, not a hand-rolled sqlite3.connect. Asserting
    # after a TRUNCATE checkpoint could not go red: measured, TRUNCATE
    # folds 1,841,672 -> 4,152 bytes with the limit set OR deleted. The
    # limit only bites on PASSIVE, which is the path production takes
    # whenever a reader pins the WAL (AC-11).
    conn = sqlite_db.connect(str(p))
    try:
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("CREATE TABLE t (a TEXT)")
        conn.executemany(
            "INSERT INTO t VALUES (?)", [("y" * 400,) for _ in range(800)])
        conn.commit()
        wal = Path(str(p) + "-wal")
        assert wal.stat().st_size > limit, "precondition: -wal never grew"

        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        conn.execute("INSERT INTO t VALUES ('one more')")
        conn.commit()

        size = wal.stat().st_size
        assert size <= limit, (
            f"-wal held {size} bytes after a PASSIVE checkpoint against a "
            f"{limit}-byte journal_size_limit -- the pragma is unset, so "
            "the file stays pinned at its full size (measured at HEAD: "
            "the limit reads back as -1 and 1,841,672 bytes survive)"
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------
# AC-13 -- the live orphan scan. Not collected by pytest.
# ---------------------------------------------------------------------

def _orphan_terms(db_path: Path) -> tuple[int, int, int, int, list[str]]:
    """Shadow-vocab differential on a COPY of ``db_path``. Read-only.

    vocab(docs_fts) - vocab(shadow), where shadow is a plain FTS5 table
    built from expand_identifiers(content) over the live docs rows. The
    term set comes from the INDEX, which is where an orphan lives.
    """
    import shutil
    import tempfile
    from prism_service.engines.brain_engine import _expand_identifiers

    tmp = Path(tempfile.mkdtemp()) / "copy.db"
    shutil.copy2(db_path, tmp)
    conn = sqlite3.connect(str(tmp))
    try:
        conn.create_function(
            "expand_identifiers", 1, _expand_identifiers, deterministic=True)
        n_docs = conn.execute("SELECT count(*) FROM docs").fetchone()[0]
        n_fts = conn.execute("SELECT count(*) FROM docs_fts").fetchone()[0]
        conn.execute("CREATE VIRTUAL TABLE shadow USING fts5(content)")
        conn.execute(
            "INSERT INTO shadow(rowid, content) "
            "SELECT rowid, expand_identifiers(content) FROM docs")
        conn.execute(
            "CREATE VIRTUAL TABLE v_i USING fts5vocab(docs_fts, 'row')")
        conn.execute(
            "CREATE VIRTUAL TABLE v_s USING fts5vocab(shadow, 'row')")
        idx = {r[0] for r in conn.execute("SELECT term FROM v_i")}
        shd = {r[0] for r in conn.execute("SELECT term FROM v_s")}
        orphans = sorted(idx - shd)
        return n_fts, n_docs, len(idx), len(orphans), orphans[:8]
    finally:
        conn.close()


def _live_scan(root: str) -> int:
    base = Path(os.path.expanduser(root))
    rc = 0
    print(f"{'project':<28}{'docs_fts':>10}{'docs':>8}{'terms':>9}"
          f"{'orphans':>9}  sample")
    for db in sorted(base.glob("*/brain.db")):
        try:
            n_fts, n_docs, n_terms, n_orph, sample = _orphan_terms(db)
        except Exception as exc:  # noqa: BLE001
            print(f"{db.parent.name:<28}  SCAN FAILED: {exc}")
            rc = 1
            continue
        print(f"{db.parent.name:<28}{n_fts:>10}{n_docs:>8}{n_terms:>9}"
              f"{n_orph:>9}  {', '.join(sample)}")
        if n_orph:
            rc = 1
    print("rc=%d (0 only when every project shows zero orphan terms)" % rc)
    return rc


if __name__ == "__main__":
    if "--live-scan" in sys.argv:
        i = sys.argv.index("--live-scan")
        target = sys.argv[i + 1] if len(sys.argv) > i + 1 else "~/.prism/projects"
        raise SystemExit(_live_scan(target))
    raise SystemExit("usage: python <this file> --live-scan <projects dir>")


# ---------------------------------------------------------------------
# green_gate round 1, finding F2 -- the skip suppresses the DOCUMENT
# REWRITE and nothing else. A content hash answers whether the CONTENT
# changed; it can never answer whether the CALLER asked for something the
# store does not hold yet.
# ---------------------------------------------------------------------

def _svc(tmp_path):
    from prism_service.services.brain_service import BrainService
    d = tmp_path / "proj"
    d.mkdir(parents=True, exist_ok=True)
    svc = BrainService(
        brain_db=str(d / "brain.db"),
        graph_db=str(d / "graph.db"),
        scores_db=str(d / "scores.db"),
    )
    if not getattr(svc, "_available", False) or svc._brain is None:
        pytest.skip("Brain engine unavailable in this environment")
    return svc


def test_a_skipped_reindex_still_records_caller_supplied_entities():
    """Reproduced live in the task workspace: index a.py, then re-index
    IDENTICAL content WITH entities. The second call took the skip and the
    graph entities table for a.py stayed empty, so an MCP brain_index_doc
    carrying entities after source_service had indexed the same bytes was
    a silent no-op."""
    import tempfile
    svc = _svc(Path(tempfile.mkdtemp()))
    content = "def handler():\n    return 1\n"

    svc.index_doc("a.py", content)
    ents = svc._brain._graph.execute(
        "SELECT count(*) FROM entities WHERE file = ?", ("a.py",),
    ).fetchone()[0]
    assert ents == 0, "no entities were supplied on the first call"

    doc_id = svc.index_doc(
        "a.py", content, entities=[{"name": "Foo", "kind": "class"}])

    ents = svc._brain._graph.execute(
        "SELECT name FROM entities WHERE file = ?", ("a.py",),
    ).fetchall()
    assert [r[0] for r in ents] == ["Foo"], (
        "the content-hash skip swallowed the caller's entities: index_doc "
        f"returned {doc_id!r} and the graph holds {ents!r}"
    )


def test_a_skipped_reindex_still_stages_the_graph():
    """Bulk graph ingest reads the staging dir. A skipped re-index used to
    leave the file unstaged forever."""
    import tempfile
    svc = _svc(Path(tempfile.mkdtemp()))
    content = "def handler():\n    return 1\n"

    staged: list[str] = []

    class _Graph:
        def stage_doc(self, path, body):
            staged.append(path)
            return True

    svc.graph_svc = _Graph()
    svc.index_doc("b.py", content)
    assert staged == ["b.py"], "first index did not stage"

    svc.index_doc("b.py", content)
    assert staged == ["b.py", "b.py"], (
        "the content-hash skip suppressed graph staging as well as the "
        f"document rewrite; staged={staged!r}"
    )


def test_a_partial_prune_does_not_keep_the_skip_marker():
    """The guard used to ask only whether SOME row survived (ORDER BY
    rowid LIMIT 1), so a prune that removed one chunk of two left the
    marker standing and the missing chunk never came back."""
    import tempfile
    svc = _svc(Path(tempfile.mkdtemp()))
    conn = svc._brain._brain
    content = (
        "def alpha():\n    return 'a'\n\n\n"
        "def beta():\n    return 'b'\n"
    )
    svc.index_doc("c.py", content)
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM docs WHERE source_file = ? ORDER BY rowid", ("c.py",),
    ).fetchall()]
    if len(ids) < 2:
        pytest.skip("chunker produced a single chunk for this fixture")

    conn.execute("DELETE FROM docs WHERE id = ?", (ids[-1],))
    conn.commit()

    svc.index_doc("c.py", content)

    after = [r[0] for r in conn.execute(
        "SELECT id FROM docs WHERE source_file = ? ORDER BY rowid", ("c.py",),
    ).fetchall()]
    assert len(after) == len(ids), (
        "a chunk pruned out from under the marker never came back: had "
        f"{ids!r}, pruned {ids[-1]!r}, re-index left {after!r}"
    )


def test_a_failed_heal_leaves_no_open_transaction():
    """delete-all empties the index and only the re-insert puts it back. A
    failure between the two must roll back, never sit open on the shared
    connection for a later commit to make permanent -- that would persist
    an EMPTIED search index."""
    import tempfile
    svc = _svc(Path(tempfile.mkdtemp()))
    brain = svc._brain
    conn = brain._brain
    svc.index_doc("d.py", "def gamma():\n    return 'zebracorn'\n")
    before = _hits(conn, "zebracorn")
    assert before >= 1, "fixture term is not in the index"

    conn.execute(
        "DELETE FROM index_meta WHERE key = ?", (brain._FTS_HEAL_KEY,))
    conn.commit()

    # The failure is injected into the SQL function the re-insert calls,
    # which fails that statement exactly where a real fault would: after
    # delete-all has emptied the index and before anything is put back.
    # (Connection.execute is read-only and Brain._brain is a property, so
    # neither can be patched.)
    from prism_service.engines.brain_engine import _expand_identifiers
    calls = {"n": 0}

    def _boom(text):
        calls["n"] += 1
        raise sqlite3.OperationalError("injected failure mid-heal")

    conn.create_function("expand_identifiers", 1, _boom)
    try:
        brain._heal_fts_orphans()
    finally:
        conn.create_function("expand_identifiers", 1, _expand_identifiers)

    assert calls["n"] == 1, "the injected failure never fired"
    assert not conn.in_transaction, (
        "the heal left a transaction OPEN after failing between delete-all "
        "and the re-insert; a later commit would persist an emptied index"
    )
    assert _hits(conn, "zebracorn") == before, (
        "the failed heal was not rolled back -- index entries were lost"
    )
