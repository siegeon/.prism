"""Unit tests for the read-only OKF host projection (services/okf_host.py)."""

from prism_service.models.memory import ExpertiseEntry
from prism_service.okf import validate
from prism_service.services.okf_host import OkfHost, build_bundle


class _FakeMemory:
    def __init__(self, entries_by_domain):
        self._d = entries_by_domain

    def list_domains(self):
        return list(self._d)

    def list_entries(self, domain, **_):
        return self._d.get(domain, [])


class _FakeBrain:
    def __init__(self, docs):
        self._docs = docs

    def list_docs(self, domain=None, limit=100):
        return self._docs[:limit]


def _mem():
    a = ExpertiseEntry(
        id="mx-1", name="task-titles", type="convention", classification="tactical",
        domain="conventions", summary="Titles are human-friendly.",
        description="See [[render-structured]] for the rule.", recorded_at="2026-06-26T00:00:00Z",
        importance=7, memory_type="procedural",
    )
    b = ExpertiseEntry(
        id="mx-2", name="render-structured", type="convention", classification="tactical",
        domain="conventions", summary="Render structured, never raw.",
        description="No pre/JSON dumps.", recorded_at="2026-06-26T00:00:00Z",
    )
    return _FakeMemory({"conventions": [a, b]})


def test_projected_bundle_is_conformant():
    bundle = build_bundle(_mem(), _FakeBrain([{"doc_id": "svc/x.py::Foo", "domain": "code"}]))
    rep = validate(bundle)
    assert rep.ok, rep.errors


def test_index_lists_memory_and_brain_sections():
    host = OkfHost(_mem(), _FakeBrain([{"doc_id": "svc/x.py::Foo", "domain": "code"}]))
    idx = host.index()
    assert idx["okf_version"] == "0.1"
    assert "memory" in idx["sections"] and "brain" in idx["sections"]
    assert idx["concept_count"] >= 3


def test_get_returns_concept_with_type_and_resolved_wikilink():
    host = OkfHost(_mem(), _FakeBrain([]))
    got = host.get("/memory/conventions/task-titles.md")
    assert got is not None
    assert got["type"] == "convention"
    # [[render-structured]] resolved to a real bundle path link.
    assert "/memory/conventions/render-structured.md" in got["body"]
    assert "/memory/conventions/render-structured.md" in got["links"]


def test_raw_serves_conformant_markdown_with_okf_version():
    host = OkfHost(_mem(), _FakeBrain([]))
    raw = host.raw("/index.md")
    assert raw is not None and 'okf_version: "0.1"' in raw
