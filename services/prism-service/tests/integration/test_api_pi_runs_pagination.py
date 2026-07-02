"""RED — GET /api/pi-runs must honor `offset` and expose a grand `total`
(task 2bf0d49b).

`limit` is honored but `offset` is silently ignored (offset=0 and
offset=999 return the same first rows) and the response carries only
`count` (page length) with no grand `total`, so a client cannot page
through the ledger or know how many runs exist. The fix threads offset
into the manifest slice and returns the full filtered count as `total`.

Exercises the REAL FastAPI app via TestClient with the pi ledger
repointed at tmp_path (module-level path patch, per the sibling
test_api_pi_runs.py fixture).
"""

from __future__ import annotations

import pytest


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path))
    import prism_service.services.pi_run_log as prl
    runs_dir = tmp_path / "pi_runs"
    monkeypatch.setattr(prl, "_RUNS_DIR", runs_dir)
    monkeypatch.setattr(prl, "_MANIFEST", runs_dir / "manifest.jsonl")

    from fastapi.testclient import TestClient
    from prism_service.main import app
    return TestClient(app)


def _seed(n: int, project: str = "alpha") -> list[str]:
    """Append n runs; return their run_ids in creation (oldest-first)
    order. list_recent reverses append order, so newest-first is the
    reverse of this list — deterministic regardless of clock resolution."""
    import prism_service.services.pi_run_log as prl
    ids: list[str] = []
    for i in range(n):
        ids.append(prl.record_run(
            backend="pi", model=f"m-{i}", purpose="reflect",
            project=project, duration_ms=1.0, tokens=1, turns=1, ok=True))
    return ids


# AC-1 — offset skips the first N newest-first rows; past-the-end -> [].
def test_offset_skips_rows(client):
    ids = _seed(3)
    newest_first = list(reversed(ids))
    r = client.get("/api/pi-runs", params={"offset": 2})
    assert r.status_code == 200, r.text
    assert [x["run_id"] for x in r.json()["runs"]] == newest_first[2:]
    r = client.get("/api/pi-runs", params={"offset": 999})
    assert r.json()["runs"] == []


# AC-2 — total is the full filtered count, independent of limit/offset.
def test_total_is_full_count(client):
    _seed(5)
    body = client.get("/api/pi-runs", params={"limit": 2}).json()
    assert body["total"] == 5
    assert len(body["runs"]) == 2
    body = client.get("/api/pi-runs", params={"offset": 3, "limit": 2}).json()
    assert body["total"] == 5


# AC-3 — limit+offset paginate: disjoint, in newest-first order, full cover.
def test_limit_offset_paginate(client):
    ids = _seed(5)
    newest_first = list(reversed(ids))
    pages: list[str] = []
    for off in (0, 2, 4):
        r = client.get("/api/pi-runs", params={"limit": 2, "offset": off})
        pages.extend(x["run_id"] for x in r.json()["runs"])
    assert pages == newest_first
    assert len(pages) == len(set(pages)), "pages overlapped"
