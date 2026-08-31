"""Red tests for task 1edee95c -- "Brain coverage is a number you can see".

TRACE. Each test below names the acceptance criterion it pins, the concrete
measurement that is RED at the base commit d16acb78, and the file the fix
must land in. The task's own `verify` pins exactly the two test functions
in this file.

  AC-1 route exists and answers entries/indexed/ratio/measured_at
       RED AT BASE: `grep -n 'def health' prism_service/api/brain.py` -> no
       match; `curl .../api/brain/health?project=prism` -> 404.
       FIX LANDS IN: prism_service/api/brain.py
  AC-2 removing one memory's rows lowers `indexed` by exactly 1
  AC-3 one memory with several chunk rows counts ONCE, never once per chunk
       RED AT BASE: `grep -n 'expertise_coverage'
       prism_service/services/brain_service.py` -> no match. The only
       counting code on file is `BrainService.status()` (brain_service.py
       :666), a raw `SELECT COUNT(*) FROM docs` with no DISTINCT and no
       domain filter. Against the live store that reads 1032 chunk rows for
       397 memories -- 260 percent coverage. That inflated number is the
       task's stated `likely_misfire`.
       FIX LANDS IN: prism_service/services/brain_service.py
  AC-4 the Dashboard renders the number the endpoint returned
       RED AT BASE: `grep -n 'brain/health'
       prism_service/web/src/pages/DashboardPage.tsx` -> no match.
       FIX LANDS IN: prism_service/web/src/pages/DashboardPage.tsx
  AC-5 the coverage query never triggers a reindex (task `stop_if` 1)

Both new symbols (`BrainService.expertise_coverage`, the `health` route) are
reached LAZILY inside each test body, mirroring
tests/unit/test_dashboard_unshipped_card.py's own convention, so a run
against the base commit is a genuine RED (rc==1, real FAILUREs) and not a
collection ERROR (rc==2), which is what the red_gate machine seat requires.

The store is a DISPOSABLE sqlite database in tmp_path built by the real
Brain engine schema -- never the live /home/siegeon/.prism store -- so the
counting rule is proven generically, not merely matched against this
machine's own 397 memories.
"""
from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "prism_service" / "web" / "src"
_DASH = _SRC / "pages" / "DashboardPage.tsx"


def _seeded_brain_svc(tmp_path: Path):
    """A REAL BrainService over a REAL, disposable Brain store.

    The engine builds its own `docs` schema; we then seed the exact shape
    the live store holds: `source_file` is the pre-chunk memory path that
    MemoryService._index_in_brain writes (memory_service.py:545), and `id`
    is that path plus a per-chunk suffix (`::main`, `::win_0`, ...).

    Seeded expertise rows: 3 chunks for mx-aaa + 2 chunks for mx-bbb = 5
    raw rows for 2 distinct memories. One extra row in the `code` domain
    guards the domain filter. Raw count 6, expertise raw count 5,
    expertise DISTINCT count 2.
    """
    from prism_service.services import sqlite_db
    from prism_service.services.brain_service import BrainService

    svc = BrainService(
        brain_db=str(tmp_path / "brain.db"),
        graph_db=str(tmp_path / "graph.db"),
        scores_db=str(tmp_path / "scores.db"),
    )
    rows = [
        ("memory/feedback/mx-aaa::main", "memory/feedback/mx-aaa", "expertise"),
        ("memory/feedback/mx-aaa::win_0", "memory/feedback/mx-aaa", "expertise"),
        ("memory/feedback/mx-aaa::win_1", "memory/feedback/mx-aaa", "expertise"),
        ("memory/decision/mx-bbb::main", "memory/decision/mx-bbb", "expertise"),
        ("memory/decision/mx-bbb::win_0", "memory/decision/mx-bbb", "expertise"),
        ("src/api/brain.py::0", "src/api/brain.py", "code"),
    ]
    conn = _brain_conn(sqlite_db, tmp_path)
    conn.executemany(
        "INSERT INTO docs (id, source_file, content, domain) VALUES (?, ?, ?, ?)",
        [(i, s, "body", d) for i, s, d in rows],
    )
    conn.commit()
    conn.close()
    return svc


def _brain_conn(sqlite_db, tmp_path):
    """A brain.db connection carrying the engine's own custom SQL function.

    The `docs` FTS5 sync triggers call `expand_identifiers(content)`, which
    brain_engine registers per connection (brain_engine.py:905-910). A plain
    connection cannot write to `docs` without it.
    """
    from prism_service.engines.brain_engine import _expand_identifiers

    conn = sqlite_db.connect(tmp_path / "brain.db", timeout=5.0)
    conn.create_function("expand_identifiers", 1, _expand_identifiers)
    return conn

def test_coverage_counts_distinct_memories_not_chunks(tmp_path, monkeypatch):
    """AC-1 / AC-2 / AC-3 / AC-5, against a real store and the real route."""
    import inspect

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from prism_service.api import brain as brain_api
    from prism_service.services import sqlite_db

    svc = _seeded_brain_svc(tmp_path)

    # -- AC-3: DISTINCT memories, never raw chunk rows. The literal the spec
    # names is 2. The wrong-and-inflated number the likely_misfire warns
    # about is 5, which is what a raw COUNT(*) over the expertise rows
    # returns, and 6 is what status()'s unfiltered COUNT(*) returns today.
    assert svc.expertise_coverage() == 2
    assert svc.status()["doc_count"] == 6

    # -- AC-5: the coverage query reads the store and never reindexes.
    src = inspect.getsource(type(svc).expertise_coverage)
    assert "reindex" not in src
    assert "COUNT(DISTINCT" in src.upper().replace("COUNT (DISTINCT", "COUNT(DISTINCT")

    # -- AC-1: the route answers with the four documented fields, and its
    # numbers are the ones the store really holds.
    class _Ctx:
        brain_svc = svc
        memory_svc = _StubMemory(3)

    monkeypatch.setattr(brain_api, "get_project", lambda p: _Ctx())
    app = FastAPI()
    app.include_router(brain_api.router, prefix="/api/brain")
    client = TestClient(app)

    body = client.get("/api/brain/health", params={"project": "prism"}).json()
    assert body["entries"] == 3
    assert body["indexed"] == 2
    assert body["ratio"] == 2 / 3
    assert body["measured_at"]

    # -- AC-2: delete every chunk row of ONE memory. `indexed` falls by
    # exactly 1 on the very next call; `entries` does not move.
    conn = _brain_conn(sqlite_db, tmp_path)
    conn.execute("DELETE FROM docs WHERE source_file = ?", ("memory/feedback/mx-aaa",))
    conn.commit()
    conn.close()

    after = client.get("/api/brain/health", params={"project": "prism"}).json()
    assert after["indexed"] == 1
    assert after["entries"] == 3


class _StubMemory:
    """The memory side of the seam, shaped like MemoryService.

    The route sums `len(list_entries(domain))` over `list_domains()`. Only
    the count matters here; the Brain half of the seam stays real, because
    that is the half the likely_misfire lives in.
    """

    def __init__(self, total: int) -> None:
        self._total = total

    def list_domains(self):
        return ["feedback", "decision"]

    def list_entries(self, domain):
        return [object()] * (self._total if domain == "feedback" else 0)


def test_the_dashboard_reads_the_endpoint():
    """AC-4: the Dashboard number comes from the endpoint, not a constant.

    The PRISM SPA has no JS test runner, so this pins the ACTUAL TSX source,
    the convention tests/unit/test_dashboard_unshipped_card.py:46 documents.
    A source read proves DELIVERY, never RENDERING -- the rendered pixel on
    http://localhost:7780 is owed at verify_green_state and is named in the
    plan as the gap to close there.
    """
    src = _DASH.read_text(encoding="utf-8")

    # The real endpoint is fetched, with the project scope the route requires.
    assert "/api/brain/health" in src

    # The rendered number is read off the response, never hardcoded.
    assert "ratio" in src

    # Hydration-guarded like every other card on this page: the number must
    # not paint before the fetch settles.
    assert "Skeleton" in src
