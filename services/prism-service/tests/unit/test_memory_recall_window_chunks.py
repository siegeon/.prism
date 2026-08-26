"""RED — MemoryService._brain_recall drops window-chunk Brain hits.

Task 33142ead. Pins the hydration contract in
MemoryService._brain_recall (services/prism-service/prism_service/services/
memory_service.py:508-522):

  * A Brain hit whose doc_id ends in ``::win_N`` (a sliding-window chunk of
    a long memory description, produced by Brain._chunk_source_file /
    _sliding_window_chunks for entries >= 2048 chars) must still resolve to
    its bare mx-id and hydrate to the matching JSONL entry. Today the code
    only strips the literal ``::main`` suffix, so ``doc_id.replace("::main",
    "")`` is a no-op on a ``::win_N`` doc_id, ``parts[-1]`` keeps the
    ``::win_N`` tail, that string is never a key in entry_map, and the hit
    is silently dropped.
  * Multiple chunk hits (main + one or more win_N windows) that map to the
    SAME entry_id must be deduped to a single result — one long memory
    should not crowd out other entries by appearing N times.
  * The existing ``::main`` hit path, and entries with no window chunks at
    all, must keep working unchanged.

These assertions exercise MemoryService.recall() and _brain_recall()
directly — the real code path behind the memory_recall MCP tool
(prism_service/mcp/tools.py:4387) and the context_bundle role-card call
(context_builder.py:271).
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service.models.memory import ExpertiseEntry
from prism_service.services.memory_service import MemoryService
import prism_service.project_context as project_context


class _FakeBrainSvc:
    """Stands in for ProjectContext.brain_svc — returns canned hits."""

    def __init__(self, hits: list[dict]) -> None:
        self._hits = hits

    def search(self, query, domain=None, limit=5):
        return list(self._hits)


class _FakeCtx:
    def __init__(self, hits: list[dict]) -> None:
        self.brain_svc = _FakeBrainSvc(hits)


def _entry(entry_id: str, domain: str = "feedback") -> ExpertiseEntry:
    return ExpertiseEntry(
        id=entry_id,
        domain=domain,
        name="a-long-memory",
        description="a memory long enough to have earned window chunks",
        type="pattern",
        classification="tactical",
        status="active",
        importance=7,
    )


def _make_service(tmp_path: Path) -> MemoryService:
    # _brain_recall regexes 'projects/<id>/' out of self._dir, so the mulch
    # dir must sit under a path shaped like a real project checkout.
    mulch_dir = tmp_path / "projects" / "testproj" / ".mulch"
    return MemoryService(str(mulch_dir))


def test_window_chunk_only_hit_still_resolves_to_its_entry(tmp_path, monkeypatch):
    """A Brain hit on a ::win_N chunk (no ::main hit at all) must hydrate."""
    entry = _entry("mx-0363a4")
    svc = _make_service(tmp_path)
    monkeypatch.setattr(svc, "_all_entries", lambda: [entry])

    hits = [{"doc_id": "memory/feedback/mx-0363a4::win_3", "score": 0.91}]
    monkeypatch.setattr(
        project_context, "get_project", lambda project_id=None: _FakeCtx(hits)
    )

    results = svc._brain_recall("some query", None, 5)

    assert [e.id for e in results] == ["mx-0363a4"], (
        "a ::win_N-only hit must resolve to the bare entry_id, "
        f"got doc_id-derived results {[e.id for e in results]!r}"
    )


def test_multiple_window_hits_for_one_entry_are_deduped(tmp_path, monkeypatch):
    """main + several win_N hits for the SAME entry must not repeat it."""
    entry = _entry("mx-abc123")
    other = _entry("mx-def456")
    svc = _make_service(tmp_path)
    monkeypatch.setattr(svc, "_all_entries", lambda: [entry, other])

    hits = [
        {"doc_id": "memory/feedback/mx-abc123::main", "score": 0.95},
        {"doc_id": "memory/feedback/mx-abc123::win_1", "score": 0.90},
        {"doc_id": "memory/feedback/mx-abc123::win_2", "score": 0.88},
        {"doc_id": "memory/feedback/mx-def456::main", "score": 0.80},
    ]
    monkeypatch.setattr(
        project_context, "get_project", lambda project_id=None: _FakeCtx(hits)
    )

    results = svc._brain_recall("some query", None, 5)
    ids = [e.id for e in results]

    assert ids.count("mx-abc123") == 1, (
        f"one long memory's window chunks flooded the results: {ids!r}"
    )
    assert "mx-def456" in ids


def test_recall_returns_entry_whose_best_hit_is_a_window_chunk(tmp_path, monkeypatch):
    """End-to-end: MemoryService.recall() must not silently drop it."""
    # Deliberately leave _read_entries UNPATCHED (it reads the empty tmp
    # mulch dir and returns []) — recall()'s domain-fallback supplement
    # would otherwise rescue the entry independently of the Brain-hit path
    # this test exists to pin, masking the very defect under test.
    entry = _entry("mx-0363a4")
    svc = _make_service(tmp_path)
    monkeypatch.setattr(svc, "_all_entries", lambda: [entry])

    hits = [{"doc_id": "memory/feedback/mx-0363a4::win_2", "score": 0.9}]
    monkeypatch.setattr(
        project_context, "get_project", lambda project_id=None: _FakeCtx(hits)
    )

    results = svc.recall("some query", domain="feedback", limit=5)

    assert [e.id for e in results] == ["mx-0363a4"], (
        "recall() must surface a memory found only via a window-chunk hit, "
        f"got {[e.id for e in results]!r}"
    )


def test_main_chunk_hit_still_resolves_unchanged(tmp_path, monkeypatch):
    """Regression guard: the existing ::main hit path must keep working."""
    entry = _entry("mx-plain01")
    svc = _make_service(tmp_path)
    monkeypatch.setattr(svc, "_all_entries", lambda: [entry])

    hits = [{"doc_id": "memory/feedback/mx-plain01::main", "score": 0.7}]
    monkeypatch.setattr(
        project_context, "get_project", lambda project_id=None: _FakeCtx(hits)
    )

    results = svc._brain_recall("some query", None, 5)

    assert [e.id for e in results] == ["mx-plain01"]


def test_no_window_chunks_short_memory_unaffected(tmp_path, monkeypatch):
    """Regression guard: an entry with no chunk suffix hits at all is fine."""
    entry = _entry("mx-short01")
    svc = _make_service(tmp_path)
    monkeypatch.setattr(svc, "_all_entries", lambda: [entry])

    hits = [{"doc_id": "memory/feedback/mx-short01", "score": 0.6}]
    monkeypatch.setattr(
        project_context, "get_project", lambda project_id=None: _FakeCtx(hits)
    )

    results = svc._brain_recall("some query", None, 5)

    assert [e.id for e in results] == ["mx-short01"]
