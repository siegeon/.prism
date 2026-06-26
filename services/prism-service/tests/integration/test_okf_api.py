"""Integration tests for the OKF host HTTP surface (api/okf.py).

RED until api/okf.py is written and mounted in main.py. Exercises the real
FastAPI router via TestClient against the live `prism` project stores.
"""

import pytest


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from prism_service.main import app

    return TestClient(app)


def test_okf_index_returns_manifest_with_version_and_sections(client):
    r = client.get("/api/okf/index", params={"project": "prism"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("okf_version") == "0.1"
    # memory is always present for the prism project; sections come from the
    # projected bundle, not a hardcoded list.
    assert "memory" in body.get("sections", [])
    assert body.get("concept_count", 0) > 0


def test_okf_raw_index_is_conformant_markdown(client):
    r = client.get("/api/okf/raw/index.md", params={"project": "prism"})
    assert r.status_code == 200, r.text
    assert 'okf_version: "0.1"' in r.text


def test_okf_concept_returns_typed_concept(client):
    idx = client.get("/api/okf/index", params={"project": "prism"}).json()
    # pick any projected concept path and fetch it
    path = next(p for p in idx["paths"] if p.startswith("/memory/"))
    r = client.get("/api/okf/concept", params={"project": "prism", "path": path})
    assert r.status_code == 200, r.text
    concept = r.json()
    assert concept["type"]  # non-empty type (OKF hard rule)
    assert "frontmatter" in concept and "body" in concept
