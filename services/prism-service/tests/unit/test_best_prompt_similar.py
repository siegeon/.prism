"""LL-06 tests — Brain.best_prompt(similar_to_task_id=...) cosine-similarity extension.

Given a new task, rank prompt variants by how well they performed on
similar past tasks (cosine similarity on task embeddings, weighted by
cuped_score from task_quality_rollup).

Parent task: 37932f3f · Sub-task LL-06.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def _mk(tmp_path):
    from app.engines.brain_engine import Brain
    from app.services.task_service import TaskService

    tasks_db = str(tmp_path / "tasks.db")
    brain = Brain(
        brain_db=str(tmp_path / "brain.db"),
        graph_db=str(tmp_path / "graph.db"),
        scores_db=str(tmp_path / "scores.db"),
        tasks_db=tasks_db,
    )
    tasks = TaskService(tasks_db)
    return brain, tasks


def _seed_task(tasks, *, task_id, title, embedding: list[float]):
    """Insert a task row directly (bypass embed_fn) with the given vector."""
    t = tasks.create(title=title)
    tasks._db.execute(
        "UPDATE tasks SET id=?, embedding=? WHERE id=?",
        (task_id, _pack(embedding), t.id),
    )
    tasks._db.commit()
    return task_id


def _seed_variant_outcome(brain, *, task_id, prompt_id, persona, step_id,
                         quality=0.8, cuped=None):
    """Wire up a task_variants + task_quality_rollup pair for the scorer."""
    brain._scores.execute(
        "INSERT INTO task_variants (task_id, step_id, prompt_id, persona) "
        "VALUES (?, ?, ?, ?)",
        (task_id, step_id, prompt_id, persona),
    )
    brain._scores.execute(
        "INSERT OR REPLACE INTO task_quality_rollup "
        "(task_id, quality_score, cuped_score) VALUES (?, ?, ?)",
        (task_id, quality, cuped if cuped is not None else quality),
    )
    brain._scores.commit()


# ----------------------------------------------------------------------


def test_similarity_top_k_returns_sorted(tmp_path):
    """Internal helper returns ids sorted by cosine desc, limited to k."""
    from app.engines.brain_engine import _similar_task_ids

    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, embedding BLOB)"
    )
    # Seed: query vec = (1,0); near = (0.99, 0.01); far = (0, 1)
    conn.execute("INSERT INTO tasks VALUES (?, ?)", ("q", _pack([1.0, 0.0])))
    conn.execute("INSERT INTO tasks VALUES (?, ?)", ("near", _pack([0.99, 0.01])))
    conn.execute("INSERT INTO tasks VALUES (?, ?)", ("far", _pack([0.0, 1.0])))
    conn.execute("INSERT INTO tasks VALUES (?, ?)", ("mid", _pack([0.7, 0.7])))
    conn.commit()
    ordered = _similar_task_ids(conn, "q", k=3)
    # Query excluded from its own neighbors; near first, mid second, far last.
    assert [t[0] for t in ordered] == ["near", "mid", "far"]
    # Similarities strictly decreasing
    sims = [t[1] for t in ordered]
    assert sims == sorted(sims, reverse=True)


def test_best_prompt_falls_back_to_global_when_no_similar_task(tmp_path):
    """No task embeddings → existing score_aggregates path is used."""
    brain, tasks = _mk(tmp_path)
    # Seed score_aggregates so the fallback returns something
    brain._scores.execute(
        "INSERT INTO score_aggregates (prompt_id, persona, step_id, avg_score, total_runs) "
        "VALUES (?, ?, ?, ?, ?)",
        ("fallback/default", "dev", "green", 0.9, 10),
    )
    brain._scores.commit()
    result = brain.best_prompt(
        persona="dev", step_id="green",
        similar_to_task_id="no-such-task",
    )
    assert result == "fallback/default"


def test_best_prompt_excludes_below_threshold_variants(tmp_path):
    """Variants with fewer than 5 observations across similar tasks
    don't influence ranking — they're correlational noise."""
    brain, tasks = _mk(tmp_path)

    # New task (the one we're ranking for)
    _seed_task(tasks, task_id="new-t", title="refactor x",
               embedding=[1.0, 0.0])
    # 10 similar tasks, all using variant_A, all high quality
    for i in range(10):
        tid = f"sim-{i}"
        _seed_task(tasks, task_id=tid, title=f"refactor {i}",
                   embedding=[0.95, 0.1])
        _seed_variant_outcome(brain, task_id=tid, prompt_id="variant_A",
                              persona="dev", step_id="green", quality=0.9)
    # 3 similar tasks using variant_B, quality 0.95 (would win on avg,
    # but below the n=5 threshold so must be excluded)
    for i in range(3):
        tid = f"simB-{i}"
        _seed_task(tasks, task_id=tid, title=f"refactor B{i}",
                   embedding=[0.94, 0.12])
        _seed_variant_outcome(brain, task_id=tid, prompt_id="variant_B",
                              persona="dev", step_id="green", quality=0.95)

    choice = brain.best_prompt(
        persona="dev", step_id="green",
        similar_to_task_id="new-t",
    )
    assert choice == "variant_A", (
        f"Low-n variant_B ({0.95} avg but only 3 obs) must not beat "
        f"variant_A (0.9 avg with 10 obs); got {choice!r}"
    )


def test_best_prompt_uses_similar_tasks(tmp_path):
    """20 refactor tasks: A wins 15, B wins 5 — new refactor → A."""
    brain, tasks = _mk(tmp_path)
    _seed_task(tasks, task_id="new-t", title="refactor new",
               embedding=[1.0, 0.0])
    # 15 similar tasks use variant_A, high quality
    for i in range(15):
        tid = f"a-{i}"
        _seed_task(tasks, task_id=tid, title=f"refactor a{i}",
                   embedding=[0.97 + 0.001 * i, 0.05])
        _seed_variant_outcome(brain, task_id=tid, prompt_id="variant_A",
                              persona="dev", step_id="green", quality=0.88)
    # 5 similar tasks use variant_B, lower quality
    for i in range(5):
        tid = f"b-{i}"
        _seed_task(tasks, task_id=tid, title=f"refactor b{i}",
                   embedding=[0.92, 0.1])
        _seed_variant_outcome(brain, task_id=tid, prompt_id="variant_B",
                              persona="dev", step_id="green", quality=0.72)

    choice = brain.best_prompt(
        persona="dev", step_id="green",
        similar_to_task_id="new-t",
    )
    assert choice == "variant_A"


def test_best_prompt_ignores_dissimilar_tasks(tmp_path):
    """When asked about a refactor task, bug-fix tasks shouldn't count.

    Seed two clusters:
      * 10 "refactor" tasks near (1,0) where variant_A dominates
      * 10 "bug" tasks near (0,1) where variant_B dominates
    Query a new refactor task — variant_A should win even though
    variant_B has a higher overall avg across the whole table.
    """
    brain, tasks = _mk(tmp_path)
    _seed_task(tasks, task_id="new-refactor", title="refactor thing",
               embedding=[1.0, 0.0])

    # Refactor cluster — A with decent quality
    for i in range(10):
        tid = f"ref-{i}"
        _seed_task(tasks, task_id=tid, title=f"refactor {i}",
                   embedding=[0.98, 0.05])
        _seed_variant_outcome(brain, task_id=tid, prompt_id="variant_A",
                              persona="dev", step_id="green", quality=0.75)
    # Bug cluster — B with very high quality (would dominate globally)
    for i in range(10):
        tid = f"bug-{i}"
        _seed_task(tasks, task_id=tid, title=f"fix bug {i}",
                   embedding=[0.05, 0.98])
        _seed_variant_outcome(brain, task_id=tid, prompt_id="variant_B",
                              persona="dev", step_id="green", quality=0.98)

    choice = brain.best_prompt(
        persona="dev", step_id="green",
        similar_to_task_id="new-refactor",
    )
    assert choice == "variant_A", (
        f"refactor-cluster winner expected even though bug cluster has "
        f"a higher global avg; got {choice!r}"
    )
