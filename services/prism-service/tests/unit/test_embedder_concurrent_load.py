"""Embedder load is serialized across threads.

Without the lock added in v5.3.18 two Brain.__init__ calls on different
threads could both observe `_MODEL is None`, both call _load_model2vec,
and both kick a HuggingFace download — racing tqdm's class state and
failing one of the loads with `'tqdm' object has no attribute '_lock'`.

This test fires N concurrent _try_enable_vector calls against a fake
loader that records its call count; the loader must be invoked exactly
once.
"""

from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from prism_service.engines import brain_engine as be


class _FakeModel:
    def encode(self, _):
        return [[0.1] * 8]


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch):
    monkeypatch.setattr(be, "_MODEL", None)
    monkeypatch.setattr(be, "_SQLITE_VEC_LOADED", False)


def test_loader_called_once_under_thread_race(monkeypatch):
    call_count = {"n": 0}

    def _slow_loader(_model_id):
        # Sleep so threads pile up at the lock; without the lock both
        # would race past the `_MODEL is None` check and increment.
        call_count["n"] += 1
        time.sleep(0.05)
        return _FakeModel()

    monkeypatch.setattr(be, "_load_model2vec", _slow_loader)

    # Pretend sqlite_vec succeeded so we reach the model load.
    class _DummyVec:
        @staticmethod
        def load(_db):
            pass

    monkeypatch.setitem(__import__("sys").modules, "sqlite_vec", _DummyVec)

    def _worker():
        db = sqlite3.connect(":memory:")
        try:
            db.enable_load_extension(True)
        except Exception:
            pass
        be._try_enable_vector(db)

    threads = [threading.Thread(target=_worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert call_count["n"] == 1, (
        f"expected exactly one model load under thread race, got {call_count['n']}"
    )
    assert be._MODEL is not None


def test_tqdm_progress_bars_disabled_at_module_import():
    import os
    # Set at module import (top of brain_engine.py) so HuggingFace + tqdm
    # see them before any worker fetches a model.
    assert os.environ.get("HF_HUB_DISABLE_PROGRESS_BARS") == "1"
    assert os.environ.get("TQDM_DISABLE") == "1"
