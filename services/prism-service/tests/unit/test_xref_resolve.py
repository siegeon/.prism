"""Unit coverage for the deterministic xref resolver (SLICE S1).

Drives the REAL /api/xref router with a FastAPI TestClient. memory is a
REAL MemoryService over a tmp mulch dir (exercises the id + name match
ladder for real); brain_svc / graph_svc are small fakes so the symbol,
file, and annotation rungs are unit-testable without seeding a brain.db.

Every rung asserts BOTH kind AND href:
  * known memory concept by id     -> kind=concept,  /understand?concept=<id>
  * known memory concept by name   -> kind=concept,  /understand?concept=<id>
  * known code symbol              -> kind=symbol,   /artifact?focus=<f>&symbol=<n>
  * known indexed file path        -> kind=file,     /artifact?focus=<f>
  * unresolved garbage token       -> kind=unresolved, href is None
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

_FILE = "prism_service/services/foo.py"
_SYMBOL = "FooResolver"


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


class _FakeBrain:
    """Matches one symbol by entity_name and one indexed file by path.
    `shared_fn` lives in TWO files to exercise qualifier disambiguation."""

    def find_symbol(self, name, kind=None, limit=10):
        if name == _SYMBOL:
            return [{"source_file": _FILE, "entity_name": _SYMBOL,
                     "entity_kind": "class", "line_start": 1, "line_end": 9}]
        if name == "shared_fn":
            return [
                {"source_file": "prism_service/services/mod_a.py",
                 "entity_name": "shared_fn", "entity_kind": "function",
                 "line_start": 1, "line_end": 3},
                {"source_file": "prism_service/services/mod_b.py",
                 "entity_name": "shared_fn", "entity_kind": "function",
                 "line_start": 1, "line_end": 3},
            ]
        return []

    def outline(self, source_file):
        if source_file == _FILE:
            return [{"entity_name": _SYMBOL, "entity_kind": "class",
                     "line_start": 1, "line_end": 9}]
        return []

    def resolve_indexed_file(self, path):
        # Exact, else heal by the trailing two path segments (mirrors the real
        # suffix match) so a stale-prefix path still finds the indexed file.
        norm = (path or "").replace("\\", "/").strip().lstrip("/")
        if norm == _FILE:
            return _FILE
        suffix = "/".join(norm.split("/")[-2:])
        if _FILE == suffix or _FILE.endswith("/" + suffix):
            return _FILE
        return None


class _FakeGraph:
    """Returns a stored annotation only for the known file scope."""

    def get_annotation(self, scope_kind, scope_id, task="name"):
        if scope_kind == "hierarchy" and scope_id == _FILE:
            return {"name": "Foo", "purpose": "Resolves foo references.",
                    "input_hash": "abc", "provenance": "claude @ 2026-06-29",
                    "updated_at": "2026-06-29"}
        return None


def _client(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from prism_service.api import xref as xref_api

    svc = MemoryService(mulch_dir=str(tmp_path / "mulch"))
    svc._write_entries("feedback", [
        _entry(id="mx-concept", name="My Concept", description="a concept"),
    ])

    class _Ctx:
        memory_svc = svc
        brain_svc = _FakeBrain()
        graph_svc = _FakeGraph()

    monkeypatch.setattr(xref_api, "get_project", lambda p: _Ctx())

    app = FastAPI()
    app.include_router(xref_api.router, prefix="/api/xref")
    return TestClient(app)


def _resolve(client, token):
    resp = client.get("/api/xref/resolve",
                      params={"token": token, "project": "prism"})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_resolves_concept_by_id(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    body = _resolve(client, "mx-concept")
    assert body["kind"] == "concept", body
    assert body["href"] == "/understand?concept=mx-concept", body


def test_resolves_concept_by_name(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    body = _resolve(client, "My Concept")
    assert body["kind"] == "concept", body
    assert body["href"] == "/understand?concept=mx-concept", body


def test_resolves_symbol(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    body = _resolve(client, _SYMBOL)
    assert body["kind"] == "symbol", body
    assert body["href"] == f"/artifact?focus={_FILE}&symbol={_SYMBOL}", body


def test_resolves_file_with_summary(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    body = _resolve(client, _FILE)
    assert body["kind"] == "file", body
    assert body["href"] == f"/artifact?focus={_FILE}", body
    # summary is wired from the existing graph annotation, never fabricated.
    assert body["summary"] == "Resolves foo references.", body


def test_resolves_symbol_with_call_parens(tmp_path, monkeypatch):
    # Real-world chip form: a symbol written with trailing call parens.
    client = _client(tmp_path, monkeypatch)
    body = _resolve(client, f"{_SYMBOL}()")
    assert body["kind"] == "symbol", body
    assert body["href"] == f"/artifact?focus={_FILE}&symbol={_SYMBOL}", body


def test_resolves_module_qualified_symbol(tmp_path, monkeypatch):
    # Real-world chip form: module.symbol (with or without call parens).
    client = _client(tmp_path, monkeypatch)
    body = _resolve(client, f"foo.{_SYMBOL}()")
    assert body["kind"] == "symbol", body
    assert body["href"] == f"/artifact?focus={_FILE}&symbol={_SYMBOL}", body


def test_qualified_symbol_prefers_matching_file(tmp_path, monkeypatch):
    # `shared_fn` exists in mod_a.py and mod_b.py; the module qualifier
    # must steer the resolver to the file whose stem matches.
    client = _client(tmp_path, monkeypatch)
    body = _resolve(client, "mod_b.shared_fn()")
    assert body["kind"] == "symbol", body
    assert body["href"] == (
        "/artifact?focus=prism_service/services/mod_b.py&symbol=shared_fn"), body


def test_strips_line_suffix_from_file_citation(tmp_path, monkeypatch):
    # The ubiquitous `path:NN` citation form must still resolve to the file.
    client = _client(tmp_path, monkeypatch)
    body = _resolve(client, f"{_FILE}:60")
    assert body["kind"] == "file", body
    assert body["href"] == f"/artifact?focus={_FILE}", body


def test_heals_stale_prefixed_file_path(tmp_path, monkeypatch):
    # A citation left over from the app->prism_service rename: the stale prefix
    # no longer exists, but the trailing path still identifies the real file,
    # so the token LINKS instead of reading as drift.
    client = _client(tmp_path, monkeypatch)
    body = _resolve(client, "services/prism-service/old_app/services/foo.py")
    assert body["kind"] == "file", body
    assert body["href"] == f"/artifact?focus={_FILE}", body


def test_unresolved_garbage_token(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    body = _resolve(client, "zzz-no-such-token-9999")
    assert body["kind"] == "unresolved", body
    assert body["href"] is None, body


def test_resolve_batch_returns_per_token_results(tmp_path, monkeypatch):
    # One request resolves many chips so the renderer can resolve eagerly and
    # only link what resolves. Mixed kinds in a single payload.
    client = _client(tmp_path, monkeypatch)
    resp = client.post("/api/xref/resolve_batch", json={
        "project": "prism",
        "tokens": ["mx-concept", _SYMBOL, "zzz-garbage"],
    })
    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    assert results["mx-concept"]["kind"] == "concept", results
    assert results[_SYMBOL]["kind"] == "symbol", results
    assert results["zzz-garbage"]["kind"] == "unresolved", results
