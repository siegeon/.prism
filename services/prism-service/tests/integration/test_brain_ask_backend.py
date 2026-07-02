"""Red suite — env-gated local backend for POST /api/brain/ask
(task e1ebdd6e, claude-p-exit epic be898578).

/ask grounds a question in Brain hybrid-search hits and shells
`claude -p` single-shot (the prompt itself forbids tool calls). With
PRISM_BRAIN_ASK_BACKEND=local the SAME grounded prompt must run as one
local_llm.complete completion, preserving the response contract —
answer / sources / run_id / exit_code / duration_s / tokens{input,
output} — with run_id + tokens filled from the run-ledger seam so the
SPA ask panel keeps its receipts. Default (unset) stays claude_cli.

Exercises the REAL FastAPI app via TestClient; Brain retrieval and both
inference backends are stubbed — pytest never needs Ollama or claude.
"""

from __future__ import annotations

import json
import urllib.error
from types import SimpleNamespace

import pytest


_HITS = [
    {"source_file": "prism_service/api/brain.py", "entity_name": "ask",
     "entity_kind": "function", "rrf_score": 0.91, "content": "def ask(...)"},
    {"source_file": "prism_service/inference/local_llm.py",
     "entity_name": "complete", "entity_kind": "function",
     "rrf_score": 0.55, "content": "def complete(...)"},
]


@pytest.fixture
def client(monkeypatch, tmp_path):
    """TestClient over the real app with Brain retrieval stubbed and the
    claude_runs ledger repointed at tmp_path."""
    import prism_service.api.brain as brain_api
    import prism_service.services.claude_run_log as crl

    class _FakeBrain:
        def search(self, q, domain=None, limit=8):
            return list(_HITS)

    monkeypatch.setattr(
        brain_api, "get_project",
        lambda project: SimpleNamespace(brain_svc=_FakeBrain()))

    runs_dir = tmp_path / "claude_runs"
    monkeypatch.setattr(crl, "_RUNS_DIR", runs_dir)
    monkeypatch.setattr(crl, "_MANIFEST", runs_dir / "manifest.jsonl")

    from fastapi.testclient import TestClient
    from prism_service.main import app
    tc = TestClient(app, raise_server_exceptions=False)
    tc._runs_dir = runs_dir
    return tc


def _manifest_rows(runs_dir):
    mf = runs_dir / "manifest.jsonl"
    if not mf.exists():
        return []
    return [json.loads(ln) for ln in
            mf.read_text(encoding="utf-8").splitlines() if ln.strip()]


@pytest.fixture
def forbid_claude(monkeypatch):
    from prism_service.inference import claude_cli

    def forbidden(*a, **k):
        raise AssertionError(
            "claude_cli.invoke must not run on the local backend")
    monkeypatch.setattr(claude_cli, "invoke", forbidden)


# ----------------------------------------------------------------------
# AC-1 — default (env unset) stays claude_cli with the current contract.
# ----------------------------------------------------------------------
def test_default_routes_claude_cli(client, monkeypatch):
    from prism_service.inference import claude_cli, local_llm

    monkeypatch.delenv("PRISM_BRAIN_ASK_BACKEND", raising=False)
    monkeypatch.setattr(
        local_llm, "complete",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("local_llm must not run on the default path")))

    seen: list[dict] = []

    class _FakeResult:
        exit_code = 0
        run_id = "claude-run-1"
        duration_s = 1.25
        usage = {"input_tokens": 100, "output_tokens": 40}

        def final_text(self):
            return "Grounded claude answer [1]."

    def fake_invoke(prompt, **kw):
        seen.append({"prompt": prompt, **kw})
        return _FakeResult()
    monkeypatch.setattr(claude_cli, "invoke", fake_invoke)

    r = client.post("/api/brain/ask", json={
        "q": "how does ask work?", "project": "proj-x", "max_turns": 2})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["answer"] == "Grounded claude answer [1]."
    assert body["run_id"] == "claude-run-1"
    assert body["exit_code"] == 0
    assert body["tokens"] == {"input": 100, "output": 40}
    assert len(body["sources"]) == len(_HITS)
    assert seen, "claude_cli.invoke was not called on the default path"
    kw = seen[0]
    assert kw["max_turns"] == 2
    assert kw["allowed_tools"] == ()
    assert kw["purpose"].startswith("brain_ask@")
    assert "how does ask work?" in seen[0]["prompt"]


# ----------------------------------------------------------------------
# AC-2 + AC-4 — local backend keeps the full response contract: same
# grounded prompt, non-empty run_id, tokens from the local usage split,
# sources preserved from the stubbed Brain hits.
# ----------------------------------------------------------------------
def test_local_backend_same_contract(client, monkeypatch, forbid_claude):
    from prism_service.inference import local_llm

    monkeypatch.setenv("PRISM_BRAIN_ASK_BACKEND", "local")
    monkeypatch.setenv("PRISM_LOCAL_LLM_MODEL", "stub-micro")

    calls: list[dict] = []

    def fake_complete(prompt, **kw):
        calls.append({"prompt": prompt, **kw})
        return {"text": "Grounded local answer [2].", "ms": 42.0,
                "tokens": 17, "input_tokens": 88, "output_tokens": 17}
    monkeypatch.setattr(local_llm, "complete", fake_complete)

    r = client.post("/api/brain/ask", json={
        "q": "how does ask work?", "project": "proj-x"})
    assert r.status_code == 200, r.text
    body = r.json()

    # Contract keys all present, same shape as the claude path.
    for key in ("question", "project", "answer", "sources", "run_id",
                "exit_code", "duration_s", "tokens"):
        assert key in body, f"missing contract key {key!r}"
    assert body["answer"] == "Grounded local answer [2]."
    assert body["exit_code"] == 0
    assert body["run_id"], "local path must fill run_id from the ledger seam"
    assert body["tokens"] == {"input": 88, "output": 17}

    # Sources keep the retrieval receipts.
    assert [s["file"] for s in body["sources"]] == [
        h["source_file"] for h in _HITS]
    assert body["sources"][0]["index"] == 1
    assert body["sources"][0]["entity_name"] == "ask"
    assert body["sources"][0]["entity_kind"] == "function"

    # Same grounded prompt, single completion.
    assert len(calls) == 1
    assert "how does ask work?" in calls[0]["prompt"]
    assert "## Retrieved context" in calls[0]["prompt"]
    assert calls[0].get("model") == "stub-micro"


# ----------------------------------------------------------------------
# AC-3 — the local run lands in the ledger; run_id matches the response.
# ----------------------------------------------------------------------
def test_local_run_lands_in_ledger(client, monkeypatch, forbid_claude):
    from prism_service.inference import local_llm

    monkeypatch.setenv("PRISM_BRAIN_ASK_BACKEND", "local")
    monkeypatch.setenv("PRISM_LOCAL_LLM_MODEL", "stub-micro")
    monkeypatch.setattr(local_llm, "complete", lambda prompt, **kw: {
        "text": "Ledger answer.", "ms": 7.0, "tokens": 5,
        "input_tokens": 31, "output_tokens": 5})

    r = client.post("/api/brain/ask", json={
        "q": "ledger check?", "project": "proj-x"})
    assert r.status_code == 200, r.text
    body = r.json()

    rows = _manifest_rows(client._runs_dir)
    assert len(rows) == 1, "local ask must append exactly one manifest row"
    row = rows[0]
    assert row["run_id"] == body["run_id"]
    assert row["backend"] == "local"
    assert row["model"] == "stub-micro"
    assert row["purpose"].startswith("brain_ask@")
    assert row["input_tokens"] == 31
    assert row["output_tokens"] == 5
    assert row["final_text"] == "Ledger answer."


# ----------------------------------------------------------------------
# AC-5 — a local endpoint failure returns 502, never a fabricated answer.
# ----------------------------------------------------------------------
def test_local_failure_returns_502(client, monkeypatch, forbid_claude):
    from prism_service.inference import local_llm

    monkeypatch.setenv("PRISM_BRAIN_ASK_BACKEND", "local")

    def down(*a, **k):
        raise urllib.error.URLError("connection refused")
    monkeypatch.setattr(local_llm, "complete", down)

    r = client.post("/api/brain/ask", json={
        "q": "anyone home?", "project": "proj-x"})
    assert r.status_code == 502, r.text
    assert _manifest_rows(client._runs_dir) == [], \
        "a failed local ask must not claim success in the ledger"
