"""RED test for the brain WAL bound (task 2f2f13ba).

trace: brain.db-wal was found grown to 17.85GB on 2026-08-09 against a
1.28GB brain.db (task description, 2f2f13ba-eb62-4792-b10b-a9806be71de5).
This proves the daemon's own checkpoint path (sqlite_maint.checkpoint_data_dir)
folds brain.db's -wal back down after a real write burst through
Brain.ingest() -- not via a manual PRAGMA in the test, and not by closing
Brain's own long-lived per-thread connection first (the daemon never
closes it either -- that lingering connection is exactly what the task's
likely_misfire names as the risk: a perpetually-open read snapshot that
keeps every checkpoint PASSIVE instead of a full TRUNCATE).
"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

# A fully-successful TRUNCATE checkpoint folds the -wal back to an empty
# (0-byte) file -- the same threshold test_sqlite_maint_glob.py's
# test_checkpoints_new_subdir_db pins for the generic glob-discovery path.
# Anything above one page (4096B) means TRUNCATE did not fully complete.
_WAL_BOUND_BYTES = 4096


def _make_brain(db_dir: Path):
    from prism_service.engines.brain_engine import Brain
    return Brain(
        brain_db=str(db_dir / "brain.db"),
        graph_db=str(db_dir / "graph.db"),
        scores_db=str(db_dir / "scores.db"),
    )


def _burst_write_source_files(source_dir: Path, count: int = 300) -> list[str]:
    source_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i in range(count):
        f = source_dir / f"mod_{i}.py"
        f.write_text(
            f"def fn_{i}():\n    '''padding {'x' * 200}'''\n    return {i}\n",
            encoding="utf-8",
        )
        paths.append(str(f))
    return paths


def test_brain_wal_shrinks_below_bound_after_daemon_checkpoint(tmp_path):
    from prism_service.services import sqlite_maint

    data_dir = tmp_path / "proj"
    brain = _make_brain(data_dir)
    try:
        # Force deterministic WAL growth: SQLite's own passive
        # auto-checkpoint (default: every 1000 pages) would otherwise fold
        # part of the WAL mid-burst and make the precondition flaky.
        brain._brain.execute("PRAGMA wal_autocheckpoint=0")

        source_dir = tmp_path / "source"
        files = _burst_write_source_files(source_dir, count=300)
        indexed = brain.ingest(files)
        assert indexed > 0, "precondition: burst ingest indexed nothing"

        wal = Path(brain._brain_db_path).with_name(
            Path(brain._brain_db_path).name + "-wal"
        )
        wal_size_before = wal.stat().st_size if wal.exists() else 0
        assert wal_size_before > _WAL_BOUND_BYTES, (
            "precondition: burst ingest did not grow brain.db-wal past the "
            f"bound ({wal_size_before} bytes) -- nothing for the checkpoint "
            "to prove"
        )

        # The daemon's REAL checkpoint entry point (main.py's periodic
        # maintenance loop calls this on a cadence). No manual
        # `PRAGMA wal_checkpoint` in this test. Brain's own long-lived
        # per-thread connection is deliberately still OPEN here -- the
        # daemon never closes it either.
        sqlite_maint.checkpoint_data_dir(data_dir)

        wal_size_after = wal.stat().st_size if wal.exists() else 0
        assert wal_size_after < _WAL_BOUND_BYTES, (
            "brain.db-wal was not folded below the bound by the daemon's "
            f"own checkpoint path: {wal_size_after} bytes remain (started "
            f"at {wal_size_before} bytes) -- a lingering read snapshot on "
            "Brain's own connection is keeping the checkpoint PASSIVE "
            "instead of a full TRUNCATE"
        )
    finally:
        brain._brain.close()
        brain._graph.close()
        brain._scores.close()
