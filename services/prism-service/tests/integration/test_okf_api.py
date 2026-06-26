"""Integration tests for the OKF host HTTP surface (api/okf.py).

Exercises the real FastAPI router via TestClient against the live `prism`
project stores. Run with PRISM_DATA_DIR=services/prism-service/data (the
documented dev store) — the sparse AppData default has no projected memory.

The OKF wiki is PRISM's curated MEMORY expressed as a navigable bundle: the
only section is "memory", every concept carries its real id (for /memory/<id>
navigation), and internal body links point only at /memory/<id> detail routes
or the /understand graph — never at /brain or other junk.
"""

import pytest


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from prism_service.main import app

    return TestClient(app)


def test_okf_index_sections_are_memory_only(client):
    r = client.get("/api/okf/index", params={"project": "prism"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("okf_version") == "0.1"
    # Memory IS the knowledge: the only projected section is "memory".
    assert body.get("sections") == ["memory"], body.get("sections")
    assert body.get("concept_count", 0) > 0
    # Every manifest concept carries its real memory id so a card click opens
    # the REAL /memory/<id> detail page (not a standalone OKF silo).
    concepts = body.get("concepts", [])
    assert concepts
    assert all(c.get("id") for c in concepts)


def test_okf_raw_index_is_conformant_markdown(client):
    r = client.get("/api/okf/raw/index.md", params={"project": "prism"})
    assert r.status_code == 200, r.text
    assert 'okf_version: "0.1"' in r.text


def test_okf_concept_links_use_memory_id_and_understand_routes(client):
    idx = client.get("/api/okf/index", params={"project": "prism"}).json()
    paths = [p for p in idx["paths"] if p.startswith("/memory/")]
    assert paths
    found_internal = False
    # Walk concepts until we find one whose body carries an internal link; assert
    # every internal link is a /memory/<id> route or /understand (no junk).
    for path in paths[:60]:
        r = client.get("/api/okf/concept", params={"project": "prism", "path": path})
        assert r.status_code == 200, r.text
        concept = r.json()
        assert concept["type"]  # OKF hard rule: non-empty type
        assert concept["frontmatter"].get("id")  # carries the real memory id
        for href in concept.get("links", []):
            if href.startswith("/"):
                found_internal = True
                assert href.startswith("/memory/") or href.startswith("/understand"), href
        if found_internal:
            break
    assert found_internal, "no concept body produced a navigable /memory or /understand link"
