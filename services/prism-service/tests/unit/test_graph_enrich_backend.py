"""Red suite — env-gated local backend for the graph-enrich seam
(task f669c5ce, claude-p-exit epic be898578).

graph_enrich.enrich_one must route through an env-gated backend seam
mirroring memory_summary_worker._invoke_backend: PRISM_GRAPH_ENRICH_
BACKEND=local -> inference/local_llm.complete (json_mode) with the
unchanged render_prompt(scope) contract; default (unset) stays
claude_cli.invoke byte-for-byte. The local path records an equivalent
claude_runs manifest row (backend='local' + input/output token counts)
via claude_run_log.record_local_run, the _parse truncation contract
(name<=60, purpose<=160, ("","") on failure) is unchanged, and the
graph_annotations provenance string reflects the active backend.

All stubs — pytest never needs Ollama or the claude CLI.
"""

from __future__ import annotations

import json
import urllib.error

import pytest


def _scope():
    return {
        "scope_id": "prism/services",
        "level": 1,
        "files": ["a.py", "b.py"],
        "symbols": ["AlphaService", "beta_helper"],
        "input_hash": "cafebabe1234",
    }


@pytest.fixture
def isolated_runs_dir(tmp_path, monkeypatch):
    """Repoint claude_run_log at tmp_path (module captures _RUNS_DIR /
    _MANIFEST from DATA_DIR at import)."""
    import prism_service.services.claude_run_log as crl
    runs_dir = tmp_path / "claude_runs"
    monkeypatch.setattr(crl, "_RUNS_DIR", runs_dir)
    monkeypatch.setattr(crl, "_MANIFEST", runs_dir / "manifest.jsonl")
    return runs_dir


@pytest.fixture
def forbid_claude(monkeypatch):
    from prism_service.inference import claude_cli

    def forbidden(*a, **k):
        raise AssertionError(
            "claude_cli.invoke must not run on the local backend")
    monkeypatch.setattr(claude_cli, "invoke", forbidden)
    return claude_cli


def _manifest_rows(runs_dir):
    mf = runs_dir / "manifest.jsonl"
    if not mf.exists():
        return []
    return [json.loads(ln) for ln in
            mf.read_text(encoding="utf-8").splitlines() if ln.strip()]


# ----------------------------------------------------------------------
# AC-1 — default (env unset) stays claude_cli with the current contract.
# ----------------------------------------------------------------------
def test_default_routes_claude_cli(monkeypatch):
    from prism_service.inference import claude_cli, local_llm
    from prism_service.services import graph_enrich as ge

    monkeypatch.delenv("PRISM_GRAPH_ENRICH_BACKEND", raising=False)

    def no_local(*a, **k):
        raise AssertionError("local_llm must not run on the default backend")
    monkeypatch.setattr(local_llm, "complete", no_local)

    seen: list[dict] = []

    class _FakeResult:
        exit_code = 0
        run_id = "fake-claude"

        def final_text(self):
            return '{"name": "Alpha Services", "purpose": "Serves alpha."}'

    def fake_invoke(**kw):
        seen.append(kw)
        return _FakeResult()
    monkeypatch.setattr(claude_cli, "invoke", fake_invoke)

    scope = _scope()
    name, purpose = ge.enrich_one(scope, "no-such-project")
    assert (name, purpose) == ("Alpha Services", "Serves alpha.")
    assert seen, "claude_cli.invoke was not called on the default path"
    kw = seen[0]
    assert kw["prompt"] == ge.render_prompt(scope)
    assert kw["max_turns"] == 1
    assert kw["allowed_tools"] == ()
    assert kw["purpose"] == "graph_enrich"


# ----------------------------------------------------------------------
# AC-2 — PRISM_GRAPH_ENRICH_BACKEND=local routes local_llm.complete with
#         json_mode and honors the _parse truncation contract.
# ----------------------------------------------------------------------
def test_local_backend_routes_local_llm(
        monkeypatch, isolated_runs_dir, forbid_claude):
    from prism_service.inference import local_llm
    from prism_service.services import graph_enrich as ge

    monkeypatch.setenv("PRISM_GRAPH_ENRICH_BACKEND", "local")
    monkeypatch.setenv("PRISM_LOCAL_LLM_MODEL", "stub-micro")

    calls: list[dict] = []
    long_name = "N" * 90
    long_purpose = "P" * 200

    def fake_complete(prompt, **kw):
        calls.append({"prompt": prompt, **kw})
        return {"text": json.dumps({"name": long_name,
                                    "purpose": long_purpose}),
                "ms": 5.0, "tokens": 9,
                "input_tokens": 33, "output_tokens": 9}
    monkeypatch.setattr(local_llm, "complete", fake_complete)

    scope = _scope()
    name, purpose = ge.enrich_one(scope, "no-such-project")
    # _parse truncation contract unchanged: name<=60, purpose<=160.
    assert name == long_name[:60]
    assert purpose == long_purpose[:160]
    assert calls, "local_llm.complete was not called on the local backend"
    assert calls[0]["prompt"] == ge.render_prompt(scope)
    assert calls[0].get("model") == "stub-micro"
    assert calls[0].get("json_mode") is True


# ----------------------------------------------------------------------
# AC-3 — the local run lands in the claude_runs ledger with tokens.
# ----------------------------------------------------------------------
def test_local_run_lands_in_ledger(
        monkeypatch, isolated_runs_dir, forbid_claude):
    from prism_service.inference import local_llm
    from prism_service.services import graph_enrich as ge

    monkeypatch.setenv("PRISM_GRAPH_ENRICH_BACKEND", "local")
    monkeypatch.setenv("PRISM_LOCAL_LLM_MODEL", "stub-micro")
    payload = '{"name": "Ledger Cluster", "purpose": "Keeps receipts."}'
    monkeypatch.setattr(local_llm, "complete", lambda prompt, **kw: {
        "text": payload, "ms": 9.0, "tokens": 6,
        "input_tokens": 27, "output_tokens": 6,
    })

    name, purpose = ge.enrich_one(_scope(), "no-such-project")
    assert (name, purpose) == ("Ledger Cluster", "Keeps receipts.")

    rows = _manifest_rows(isolated_runs_dir)
    assert len(rows) == 1, "local run must append exactly one manifest row"
    row = rows[0]
    assert row["backend"] == "local"
    assert row["model"] == "stub-micro"
    assert row["purpose"] == "graph_enrich"
    assert row["exit_code"] == 0
    assert row["input_tokens"] == 27
    assert row["output_tokens"] == 6
    assert row["tokens_used"] == 33
    # The synthetic stream file must exist so /api/claude-runs/{id}/stream
    # (and final_text extraction) keep working for local runs.
    assert row["final_text"] == payload
    import prism_service.services.claude_run_log as crl
    assert crl.stream_path_for(row["run_id"]) is not None


# ----------------------------------------------------------------------
# AC-4 — a local failure returns ("", "") — cycle moves on, no success row.
# ----------------------------------------------------------------------
def test_local_failure_returns_empty(
        monkeypatch, isolated_runs_dir, forbid_claude):
    from prism_service.inference import local_llm
    from prism_service.services import graph_enrich as ge

    monkeypatch.setenv("PRISM_GRAPH_ENRICH_BACKEND", "local")
    attempts: list[int] = []

    def down(*a, **k):
        attempts.append(1)
        raise urllib.error.URLError("endpoint down")
    monkeypatch.setattr(local_llm, "complete", down)

    assert ge.enrich_one(_scope(), "no-such-project") == ("", "")
    assert attempts, "local backend was never attempted"
    assert _manifest_rows(isolated_runs_dir) == [], \
        "a failed local run must not claim success in the ledger"


# ----------------------------------------------------------------------
# AC-5 — annotation provenance reflects the active backend.
# ----------------------------------------------------------------------
class _StubGraph:
    def __init__(self):
        self.upserts: list[tuple] = []

    def get_annotation(self, scope_kind, scope_id, kind):
        return None  # everything reads as changed

    def upsert_annotation(self, scope_kind, scope_id, kind,
                          name, purpose, input_hash, provenance):
        self.upserts.append(
            (scope_kind, scope_id, name, purpose, provenance))
        return True


def test_provenance_reflects_backend(
        monkeypatch, isolated_runs_dir, forbid_claude):
    from prism_service.inference import local_llm
    from prism_service.services import graph_enrich as ge

    monkeypatch.setenv("PRISM_GRAPH_ENRICH_BACKEND", "local")
    monkeypatch.setattr(local_llm, "complete", lambda prompt, **kw: {
        "text": '{"name": "Local Name", "purpose": "Local purpose."}',
        "ms": 3.0, "tokens": 5, "input_tokens": 12, "output_tokens": 5,
    })

    graph = _StubGraph()
    out = {"enriched": 0, "skipped": 0, "errors": 0, "pending": 0}
    ge._enrich_kind(graph, "no-such-project", "community",
                    [_scope()], 1, out)
    assert out["enriched"] == 1
    assert graph.upserts, "annotation was not upserted"
    prov = graph.upserts[0][-1]
    assert prov.startswith("local @ "), \
        f"provenance must reflect the local backend, got {prov!r}"


def test_provenance_default_stays_claude(monkeypatch):
    from prism_service.inference import claude_cli
    from prism_service.services import graph_enrich as ge

    monkeypatch.delenv("PRISM_GRAPH_ENRICH_BACKEND", raising=False)

    class _FakeResult:
        exit_code = 0
        run_id = "fake-claude"

        def final_text(self):
            return '{"name": "Claude Name", "purpose": "Claude purpose."}'

    monkeypatch.setattr(claude_cli, "invoke", lambda **kw: _FakeResult())

    graph = _StubGraph()
    out = {"enriched": 0, "skipped": 0, "errors": 0, "pending": 0}
    ge._enrich_kind(graph, "no-such-project", "hierarchy",
                    [_scope()], 1, out)
    assert out["enriched"] == 1
    prov = graph.upserts[0][-1]
    assert prov.startswith("claude @ "), \
        f"default provenance must stay claude-shaped, got {prov!r}"
