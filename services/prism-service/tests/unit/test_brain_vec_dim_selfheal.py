"""docs_vec self-heals an embedding-dimension change.

Regression guard for v6.3.36. The vec0 table is created `IF NOT EXISTS`, so
when the embedder's output dim changes (MiniLM 384 -> potion-base-32M 512)
the stale table survived and every index_doc vector insert raised
"Dimension mismatch ... Expected 384 ... received 512" — silently, because
FTS still worked. ``Brain._init_brain_schema`` must drop + recreate docs_vec
at the live model dim, and must NOT drop it when the dim is unchanged.
"""

from __future__ import annotations

import re

import numpy as np
import pytest

from prism_service.engines import brain_engine as be


class _FakeModel:
    """Stand-in embedder whose output dimension we control.

    Returns numpy arrays like the real model2vec / sentence-transformers
    backends — Brain._embed calls ``.tolist()`` on each row.
    """

    def __init__(self, dim: int) -> None:
        self._dim = dim

    def encode(self, texts):
        return np.asarray(
            [[0.1] * self._dim for _ in texts], dtype=np.float32
        )


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch):
    monkeypatch.setattr(be, "_MODEL", None)
    monkeypatch.setattr(be, "_SQLITE_VEC_LOADED", False)


def _docs_vec_dim(brain) -> int | None:
    row = brain._brain.execute(
        "SELECT sql FROM sqlite_master WHERE name='docs_vec'"
    ).fetchone()
    if not row or not row[0]:
        return None
    m = re.search(r"float\[(\d+)\]", row[0])
    return int(m.group(1)) if m else None


def _vec_count(brain) -> int:
    return brain._brain.execute("SELECT count(*) FROM docs_vec").fetchone()[0]


def _close(brain) -> None:
    for attr in ("_brain", "_graph", "_scores"):
        try:
            getattr(brain, attr).close()
        except Exception:
            pass


def _new_brain(tmp_path):
    return be.Brain(
        str(tmp_path / "brain.db"),
        str(tmp_path / "graph.db"),
        str(tmp_path / "scores.db"),
    )


def test_docs_vec_rebuilds_on_dim_change(tmp_path, monkeypatch):
    # First boot: 384-dim model -> docs_vec float[384] with a row in it.
    monkeypatch.setattr(be, "_MODEL", _FakeModel(384))
    b1 = _new_brain(tmp_path)
    if not b1.vector_enabled:
        pytest.skip("sqlite-vec not available in this environment")
    assert _docs_vec_dim(b1) == 384
    b1._ingest_single("d1", "def foo():\n    return 1\n", source_file="d1.py")
    assert _vec_count(b1) == 1
    _close(b1)

    # Second boot on the SAME db: 512-dim model. The stale 384 table must be
    # rebuilt at 512, and a fresh 512-dim insert must succeed (the bug was
    # every insert raising "Dimension mismatch").
    monkeypatch.setattr(be, "_MODEL", _FakeModel(512))
    b2 = _new_brain(tmp_path)
    assert b2.vector_enabled
    assert _docs_vec_dim(b2) == 512, "stale 384 table should be rebuilt at 512"
    assert _vec_count(b2) == 0, "rebuilt table starts empty; reindex repopulates"
    ok = b2._ingest_single("d2", "def bar():\n    return 2\n", source_file="d2.py")
    assert ok is True
    assert _vec_count(b2) == 1
    _close(b2)


def test_docs_vec_preserved_on_same_dim(tmp_path, monkeypatch):
    # Self-heal must be CONDITIONAL: an unchanged dim must not drop the table
    # (a normal restart must never wipe existing vectors).
    monkeypatch.setattr(be, "_MODEL", _FakeModel(384))
    b1 = _new_brain(tmp_path)
    if not b1.vector_enabled:
        pytest.skip("sqlite-vec not available in this environment")
    b1._ingest_single("d1", "content one", source_file="d1.py")
    assert _vec_count(b1) == 1
    _close(b1)

    monkeypatch.setattr(be, "_MODEL", _FakeModel(384))
    b2 = _new_brain(tmp_path)
    assert _docs_vec_dim(b2) == 384
    assert _vec_count(b2) == 1, "same-dim reboot must preserve existing vectors"
    _close(b2)
