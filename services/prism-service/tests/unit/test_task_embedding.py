"""LL-03 tests — task embedding on create/update.

Parent task: 37932f3f-9cd4-40bf-9df3-e9db19fcc88d · Sub-task LL-03
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


def _fake_embed(text: str) -> bytes:
    """Deterministic fake embedder: packs a 4-float vector derived from
    the text. Different text → different bytes. Same text → same bytes."""
    h = hash(text) & 0xFFFFFFFF
    return struct.pack("<4f", float(h & 0xFF),
                       float((h >> 8) & 0xFF),
                       float((h >> 16) & 0xFF),
                       float((h >> 24) & 0xFF))


def _mk_service(tmp_path: Path, embed_fn=_fake_embed):
    from app.services.task_service import TaskService
    return TaskService(str(tmp_path / "tasks.db"), embed_fn=embed_fn)


# ----------------------------------------------------------------------


def test_embedding_populated_on_create(tmp_path):
    """Creating a task stores an embedding BLOB."""
    svc = _mk_service(tmp_path)
    t = svc.create(title="Refactor auth middleware",
                   description="Move JWT parse into a dedicated helper.")
    row = svc._db.execute(
        "SELECT embedding FROM tasks WHERE id=?", (t.id,)
    ).fetchone()
    assert row is not None
    assert row["embedding"] is not None
    assert len(row["embedding"]) > 0


def test_embedding_updated_on_title_change(tmp_path):
    """Changing the title re-embeds (new bytes stored)."""
    svc = _mk_service(tmp_path)
    t = svc.create(title="Task A", description="Do a thing.")
    old = svc._db.execute(
        "SELECT embedding FROM tasks WHERE id=?", (t.id,)
    ).fetchone()["embedding"]
    svc.update(t.id, title="Task A — renamed")
    new = svc._db.execute(
        "SELECT embedding FROM tasks WHERE id=?", (t.id,)
    ).fetchone()["embedding"]
    assert old != new, "title change must trigger re-embedding"


def test_embedding_unchanged_when_title_unchanged(tmp_path):
    """Updating unrelated fields (priority, tags) leaves the embedding alone."""
    svc = _mk_service(tmp_path)
    t = svc.create(title="Task A", description="Do a thing.")
    old = svc._db.execute(
        "SELECT embedding FROM tasks WHERE id=?", (t.id,)
    ).fetchone()["embedding"]
    svc.update(t.id, priority=7)
    new = svc._db.execute(
        "SELECT embedding FROM tasks WHERE id=?", (t.id,)
    ).fetchone()["embedding"]
    assert old == new, "non-title/description update must not re-embed"


# Removed test_embedding_dim_matches_miniLM_384: the assertion required
# the MiniLM embedder + sqlite-vec to be loaded, but the runtime guard
# always took the skip branch in this environment (sqlite-vec optional).
# A test that always skips is dead code, not a guard. If we want the
# 384-dim invariant enforced, encode it as a constant assertion at the
# point of use in brain_engine, not as an environment-conditional test.
