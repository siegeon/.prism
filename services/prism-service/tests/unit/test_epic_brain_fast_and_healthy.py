"""Epic 95474ec7 trace — "The Brain answers fast and stays healthy".

Each test pins ONE acceptance clause of the epic's story to the artifact that
demonstrates it in THIS tree, so the epic's red/green is decided by what the
repo actually contains, never by a roll-up of child task statuses in PRISM
(lesson: an epic's gate is its own oracle demonstrated).

AC-1 -> child 3a3f90da  brain-first retrieval on drives
AC-2 -> child 39244a32  benchmark watchdog suite
AC-3 -> child 33142ead  memory_recall hydrates ::win_N chunks
AC-4 -> child 2f2f13ba  brain WAL bounded suite
AC-5 is the human demo clause (live-daemon observation) and has no unit pin.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[4]
UNIT = REPO / "services/prism-service/tests/unit"


def _memory_service_for(tmp_path, entries, hits):
    """A MemoryService whose Brain returns `hits` and whose JSONL holds `entries`."""
    from prism_service.services import memory_service as ms

    svc = object.__new__(ms.MemoryService)
    svc._dir = tmp_path / "projects" / "prism" / "memory"
    svc._all_entries = lambda: entries
    fake_ctx = SimpleNamespace(
        brain_svc=SimpleNamespace(search=lambda *a, **k: hits)
    )
    return svc, fake_ctx


# AC-3: a memory longer than one window is indexed as ::win_0, ::win_1, ...
# and memory_recall must hydrate those hits, not silently drop them.
@pytest.mark.parametrize("suffix", ["::main", "::win_0", "::win_7"])
def test_ac3_brain_recall_hydrates_every_chunk_suffix(tmp_path, monkeypatch, suffix):
    entry = SimpleNamespace(id="mx-abc123", status="active")
    hit = {"doc_id": f"memory/expertise/mx-abc123{suffix}", "score": 0.9}
    svc, ctx = _memory_service_for(tmp_path, [entry], [hit])
    import prism_service.project_context as pc
    monkeypatch.setattr(pc, "get_project", lambda pid: ctx)

    got = svc._brain_recall("anything", None, 5)

    assert got == [entry], (
        f"hit {hit['doc_id']!r} was dropped — the {suffix} chunk suffix is not "
        "stripped before entry_map lookup (memory_service._brain_recall)"
    )


def test_ac3_multi_window_memory_returns_once_not_per_chunk(tmp_path, monkeypatch):
    entry = SimpleNamespace(id="mx-long", status="active")
    hits = [{"doc_id": f"memory/expertise/mx-long::win_{i}"} for i in range(3)]
    svc, ctx = _memory_service_for(tmp_path, [entry], hits)
    import prism_service.project_context as pc
    monkeypatch.setattr(pc, "get_project", lambda pid: ctx)

    got = svc._brain_recall("anything", None, 5)

    assert got == [entry], "three window chunks of one memory must hydrate to that one memory"


# AC-1 / AC-2 / AC-4: the child slices' pinned suites are present in THIS tree.
@pytest.mark.parametrize(
    "ac,rel",
    [
        ("AC-1", "services/prism-service/tests/unit/test_implement_brain_first_retrieval.py"),
        ("AC-2", "benchmarks/tests/test_ab_retrieval_watchdog.py"),
        ("AC-4", "services/prism-service/tests/unit/test_brain_wal_bounded.py"),
    ],
)
def test_child_pinned_suite_is_in_this_tree(ac, rel):
    assert (REPO / rel).is_file(), f"{ac}: pinned suite {rel} is not in the tree under review"
