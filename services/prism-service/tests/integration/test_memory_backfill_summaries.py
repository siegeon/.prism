"""RED scaffold — POST /api/memory/backfill-summaries (task 2567758d).

Pins the USER-FACING integration end-to-end through the REAL memory
router + the REAL event_pool bus singleton — not just a service method:

  * POST /api/memory/backfill-summaries enqueues ONLY active,
    summary-less memories onto the memory.written bus.
  * Entries that already carry a non-empty summary are SKIPPED.
  * Entries with invalid_at set are SKIPPED (active-only scoping).
  * Each enqueued item emits an event_pool.Event of type MEMORY_WRITTEN
    whose payload carries BOTH memory_id AND project.
  * The endpoint returns {"queued": N, "project": project} for the SPA
    "Generate summaries (N)" button.
  * Idempotent: a second run with no new summary-less entries queues 0.
  * The MemoryPage UI source drops the "Summarizing…" placeholder (renders
    entry.description when summary is empty), reframes the KPI to a truthful
    "Summaries N/M" coverage count, and wires the backfill button.

All FAIL today: the route 404s and the UI still ships the placeholder /
"Awaiting summary" KPI / no backfill button.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_WEB_SRC = _SERVICE_ROOT / "prism_service" / "web" / "src"

from prism_service.services import event_pool as ep
from prism_service.services.memory_service import MemoryService


# ---------------------------------------------------------------------------
# A real tmp-backed MemoryService wired into the real router via get_project.
# ---------------------------------------------------------------------------

def _client(monkeypatch, svc):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from prism_service.api import memory as memory_api

    class _Ctx:
        memory_svc = svc

    monkeypatch.setattr(memory_api, "get_project", lambda p: _Ctx())
    app = FastAPI()
    app.include_router(memory_api.router, prefix="/api/memory")
    return TestClient(app)


@pytest.fixture
def captured_bus(monkeypatch):
    """Replace the process-singleton bus with one whose emit() records the
    Events, so the test can assert on the REAL emission seam the endpoint
    uses (ep.get_bus().emit(...))."""
    events: list = []

    class _SpyBus:
        def emit(self, event):
            events.append(event)

    monkeypatch.setattr(ep, "_BUS", _SpyBus())
    monkeypatch.setattr(ep, "get_bus", lambda: ep._BUS)
    return events


@pytest.fixture
def svc(tmp_path):
    return MemoryService(str(tmp_path / "expertise"))


def _store(svc, name, desc, *, summary=None, invalidate=False):
    e = svc.store(
        domain="feedback", name=name, description=desc,
        type="convention", classification="tactical",
    )
    patch = {}
    if summary is not None:
        patch["summary"] = summary
    if invalidate:
        patch["invalid_at"] = "2026-06-01T00:00:00+00:00"
    if patch:
        svc.update_entry(e.id, **patch)
        e = svc.get_entry(e.id)
    return e


# ---------------------------------------------------------------------------
# API SEAM — only active + summary-less entries enqueue
# ---------------------------------------------------------------------------

def test_backfill_enqueues_only_active_summaryless(svc, captured_bus, monkeypatch):
    client = _client(monkeypatch, svc)

    bare1 = _store(svc, "bare-one", "the first entry that still needs a one-line summary minted")
    bare2 = _store(svc, "bare-two", "a totally different memory about token budget governance pacing")
    _store(svc, "already", "has a summary", summary="a crisp haiku line")
    _store(svc, "stale", "invalidated entry", invalidate=True)

    resp = client.post("/api/memory/backfill-summaries", params={"project": "prism"})
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["project"] == "prism"
    assert body["queued"] == 2, f"only the 2 bare entries enqueue: {body}"

    emitted_ids = {e.payload["memory_id"] for e in captured_bus}
    assert emitted_ids == {bare1.id, bare2.id}, (
        "must enqueue exactly the active, summary-less entries — "
        "summarized + invalidated entries are skipped"
    )


def test_backfill_emits_memory_written_with_memory_id_and_project(svc, captured_bus, monkeypatch):
    client = _client(monkeypatch, svc)
    e = _store(svc, "lonely-bare", "no summary yet")

    resp = client.post("/api/memory/backfill-summaries", params={"project": "prism"})
    assert resp.status_code == 200, resp.text

    assert len(captured_bus) == 1
    ev = captured_bus[0]
    assert ev.type == ep.MEMORY_WRITTEN, "event type must be memory.written"
    # The likely_misfire to defend against: payload missing project -> the
    # reactive handler can't resolve the project's MemoryService.
    assert ev.payload.get("memory_id") == e.id
    assert ev.payload.get("project") == "prism"


def test_backfill_skips_summarized_entries(svc, captured_bus, monkeypatch):
    client = _client(monkeypatch, svc)
    _store(svc, "done-one", "summarized one", summary="line one")
    _store(svc, "done-two", "summarized two", summary="line two")

    resp = client.post("/api/memory/backfill-summaries", params={"project": "prism"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["queued"] == 0
    assert captured_bus == [], "no events for already-summarized entries"


def test_backfill_skips_invalidated_entries(svc, captured_bus, monkeypatch):
    client = _client(monkeypatch, svc)
    _store(svc, "gone", "invalidated, no summary", invalidate=True)

    resp = client.post("/api/memory/backfill-summaries", params={"project": "prism"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["queued"] == 0
    assert captured_bus == [], "invalid_at entries are out of active scope"


def test_backfill_is_idempotent(svc, captured_bus, monkeypatch):
    """Re-running enqueues only entries STILL lacking a summary. With no new
    bare entries and nothing minting summaries between runs, the second run
    re-queues the same set; the count is stable and never grows."""
    client = _client(monkeypatch, svc)
    _store(svc, "bare", "no summary")

    first = client.post("/api/memory/backfill-summaries", params={"project": "prism"})
    assert first.json()["queued"] == 1
    captured_bus.clear()

    second = client.post("/api/memory/backfill-summaries", params={"project": "prism"})
    assert second.status_code == 200, second.text
    # Same surviving bare entry -> count does not grow; re-runs are safe.
    assert second.json()["queued"] == 1


# ---------------------------------------------------------------------------
# UI SEAM — the MemoryPage.tsx source pins that used to live here were removed
# with the page itself: it was a verified unrouted orphan (App.tsx redirects
# /memory -> /understand) deleted by the ui-redesign epic's orphan sweep
# (16777a76 ws6). The backfill SERVICE seams above remain the live contract;
# the memory reading surface is the Understand wiki, which must stay free of
# the fake "Summarizing…" placeholder this file originally banned.
# ---------------------------------------------------------------------------

def test_memory_orphan_page_stays_deleted():
    assert not (_WEB_SRC / "pages" / "MemoryPage.tsx").exists(), (
        "MemoryPage.tsx was deleted as a verified unrouted orphan "
        "(ui-redesign 16777a76); do not resurrect it — /memory redirects "
        "to the Understand wiki"
    )


def test_understand_surface_has_no_fake_summarizing_placeholder():
    src = (_WEB_SRC / "pages" / "UnderstandPage.tsx").read_text(
        encoding="utf-8")
    assert "Summarizing" not in src, (
        "the memory reading surface must not render a fake 'Summarizing…' "
        "progress placeholder"
    )
