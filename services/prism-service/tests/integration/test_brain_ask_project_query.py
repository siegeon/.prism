"""RED — POST /api/brain/ask must honor the ?project= query param
(task 8dd3293d).

`AskBody.project` is a BODY-only field with no `Query()` binding, unlike
sibling `brain/search`, so a standard `?project=prism` call is silently
ignored and the answer grounds in the empty `default` brain. The fix
resolves the effective project as query-wins / body-fallback / 'default'
(mirroring `api/conductor._effective_project`, the 48b965e0 precedent).

Exercises the REAL FastAPI app via TestClient; Brain retrieval and the
local inference backend are stubbed so pytest never needs Ollama or
claude.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.fixture
def client(monkeypatch, tmp_path):
    import prism_service.api.brain as brain_api
    import prism_service.services.pi_run_log as prl

    class _FakeBrain:
        def search(self, q, domain=None, limit=8):
            return []

    seen: dict = {}

    def _fake_get_project(project):
        seen["project"] = project
        return SimpleNamespace(brain_svc=_FakeBrain())

    monkeypatch.setattr(brain_api, "get_project", _fake_get_project)

    pi_dir = tmp_path / "pi_runs"
    monkeypatch.setattr(prl, "_RUNS_DIR", pi_dir)
    monkeypatch.setattr(prl, "_MANIFEST", pi_dir / "manifest.jsonl")

    # Route through the local backend and stub the completion so no real
    # model is invoked — we only care about which project the handler
    # resolves and echoes.
    monkeypatch.setenv("PRISM_BRAIN_ASK_BACKEND", "local")
    from prism_service.inference import local_llm
    monkeypatch.setattr(
        local_llm, "complete",
        lambda prompt, **kw: {"text": "stub", "run_id": "pi-1",
                              "input_tokens": 1, "output_tokens": 1,
                              "tokens": 1})

    from fastapi.testclient import TestClient
    from prism_service.main import app
    tc = TestClient(app, raise_server_exceptions=False)
    tc._seen = seen
    return tc


# AC-1 — the query param grounds the answer in the named brain.
def test_query_param_selects_project(client):
    r = client.post("/api/brain/ask?project=prism", json={"q": "hi"})
    assert r.status_code == 200, r.text
    assert r.json()["project"] == "prism"
    assert client._seen["project"] == "prism", \
        "Brain was retrieved from the wrong project"


# AC-2 — a body project with no query param is still honored (fallback).
def test_body_project_still_fallback(client):
    r = client.post("/api/brain/ask", json={"q": "hi", "project": "beta"})
    assert r.status_code == 200, r.text
    assert r.json()["project"] == "beta"


# AC-3 — the query param wins when both are supplied.
def test_query_wins_over_body(client):
    r = client.post("/api/brain/ask?project=prism",
                    json={"q": "hi", "project": "beta"})
    assert r.status_code == 200, r.text
    assert r.json()["project"] == "prism"
