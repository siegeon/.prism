"""RED scaffold — /api/memory activation seam (Tier 1, task 0bda3046).

Drives the REAL memory router with a FastAPI TestClient over a REAL
MemoryService backed by a tmp mulch dir. This pins the USER-FACING
seam, not just a service method:

  * GET /api/memory/entries — every entry dict carries an 'activation'
    float computed at query time.
  * GET /api/memory/fading — a NEW route returning low-activation entries
    ordered ascending by activation, with a configurable limit. This is
    the selection prior the background pruner reads.

Both FAIL today: the entries route serializes raw ExpertiseEntry (no
'activation' key) and /api/memory/fading 404s (route does not exist).
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service.models.memory import ExpertiseEntry
from prism_service.services.memory_service import MemoryService


# Anchor to the REAL current time, not a frozen past date. The
# /api/memory/* endpoints compute activation against the live wall clock
# (datetime.now), so a hardcoded past _NOW turned this into a time bomb:
# once real time advanced ~30d past it, the "1h ago" hot seed read as
# weeks-stale and leaked into the fading set. Relative offsets below
# (1h / 40d / 220d ago) now hold on any run date.
_NOW = datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _entry(**kw) -> ExpertiseEntry:
    base = dict(
        id=kw.pop("id", "mx-aaaaaa"),
        domain=kw.pop("domain", "feedback"),
        name=kw.pop("name", "an-entry"),
        description=kw.pop("description", "desc"),
        type="convention",
        classification="tactical",
        status="active",
    )
    base.update(kw)
    return ExpertiseEntry(**base)


def _client(tmp_path, monkeypatch):
    """Stand up the real memory router over a real MemoryService whose
    mulch dir is tmp. get_project(...).memory_svc is pointed at it."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from prism_service.api import memory as memory_api

    svc = MemoryService(mulch_dir=str(tmp_path / "mulch"))

    class _Ctx:
        memory_svc = svc

    monkeypatch.setattr(memory_api, "get_project", lambda p: _Ctx())

    app = FastAPI()
    app.include_router(memory_api.router, prefix="/api/memory")
    return TestClient(app), svc


def _seed(svc: MemoryService) -> None:
    hot = _entry(id="mx-hot", name="hot", importance=9, effectiveness=0.8,
                 last_recalled=_iso(_NOW - timedelta(hours=1)))
    warm = _entry(id="mx-warm", name="warm", importance=5, effectiveness=0.0,
                  last_recalled=_iso(_NOW - timedelta(days=40)))
    cold = _entry(id="mx-cold", name="cold", importance=2, effectiveness=-0.6,
                  last_recalled=_iso(_NOW - timedelta(days=220)))
    svc._write_entries("feedback", [hot, warm, cold])


def test_entries_response_includes_activation_field(tmp_path, monkeypatch):
    client, svc = _client(tmp_path, monkeypatch)
    _seed(svc)
    resp = client.get("/api/memory/entries", params={"project": "prism"})
    assert resp.status_code == 200, resp.text
    entries = resp.json()["entries"]
    assert entries, "expected seeded entries"
    for e in entries:
        assert "activation" in e, f"entry missing 'activation' field: {e.get('id')}"
        assert isinstance(e["activation"], (int, float)), e["activation"]
    # The hot entry's activation must exceed the cold entry's.
    by_id = {e["id"]: e["activation"] for e in entries}
    assert by_id["mx-hot"] > by_id["mx-cold"], by_id


def test_fading_endpoint_returns_low_activation_ascending(tmp_path, monkeypatch):
    client, svc = _client(tmp_path, monkeypatch)
    _seed(svc)
    resp = client.get("/api/memory/fading", params={"project": "prism", "limit": 10})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    entries = body["entries"]
    assert entries, "fading endpoint returned nothing for clearly-faded seed"
    acts = [e["activation"] for e in entries]
    assert acts == sorted(acts), f"fading must be ascending by activation: {acts}"
    # Coldest (lowest activation) leads.
    assert entries[0]["id"] == "mx-cold", [e["id"] for e in entries]
    # The hot entry is not a fading candidate.
    assert "mx-hot" not in [e["id"] for e in entries]


def test_fading_endpoint_respects_limit(tmp_path, monkeypatch):
    client, svc = _client(tmp_path, monkeypatch)
    # Six clearly-faded entries.
    faded = [
        _entry(id=f"mx-f{i}", name=f"f{i}", importance=1, effectiveness=-1.0,
               last_recalled=_iso(_NOW - timedelta(days=300 + i)))
        for i in range(6)
    ]
    svc._write_entries("feedback", faded)
    resp = client.get("/api/memory/fading", params={"project": "prism", "limit": 3})
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["entries"]) == 3, "limit must cap the fading list"
