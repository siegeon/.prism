"""Unit tests for the read-only OKF host projection (services/okf_host.py).

The host projects PRISM's curated MEMORY (and ONLY memory) into a navigable OKF
wiki: every concept is a real ExpertiseEntry, carries its `id` so the UI can open
the real /memory/<id> detail page, [[wikilinks]] rewrite to /memory/<id> routes,
and code references (evidence file_paths) link into /understand. No brain-file or
fixture junk in the bundle.
"""

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
    """Present only to prove build_bundle now IGNORES the brain store entirely."""

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
        evidence={"file_paths": ["prism_service/api/track.py"]},
    )
    b = ExpertiseEntry(
        id="mx-2", name="render-structured", type="convention", classification="tactical",
        domain="conventions", summary="Render structured, never raw.",
        description="No pre/JSON dumps.", recorded_at="2026-06-26T00:00:00Z",
    )
    return _FakeMemory({"conventions": [a, b]})


def _brain():
    return _FakeBrain([{"doc_id": "svc/x.py::Foo", "domain": "code"}])


def test_projected_bundle_is_conformant():
    rep = validate(build_bundle(_mem(), _brain()))
    assert rep.ok, rep.errors


def test_bundle_is_memory_only_no_brain_section():
    # Memory IS the knowledge — zero brain-file / fixture / empty junk concepts.
    bundle = build_bundle(_mem(), _brain())
    assert bundle.sections() == ["memory"]
    assert all(p.startswith("/memory/") for p in bundle.paths())


def test_every_memory_concept_carries_nonempty_id():
    host = OkfHost(_mem(), _brain())
    idx = host.index()
    assert idx["sections"] == ["memory"]
    assert idx["concepts"]
    for c in idx["concepts"]:
        assert c["id"], c


def test_get_concept_carries_id_and_wikilink_resolves_to_memory_route():
    host = OkfHost(_mem(), _FakeBrain([]))
    got = host.get("/memory/conventions/task-titles.md")
    assert got is not None
    assert got["type"] == "convention"
    assert got["frontmatter"]["id"] == "mx-1"
    # [[render-structured]] -> the target entry's REAL /memory/<id> detail route.
    assert "/memory/mx-2" in got["body"]
    assert "/memory/mx-2" in got["links"]


def test_code_reference_links_into_understand():
    host = OkfHost(_mem(), _FakeBrain([]))
    got = host.get("/memory/conventions/task-titles.md")
    assert "/understand" in got["body"]
    assert "/understand" in got["links"]


def test_raw_serves_conformant_markdown_with_okf_version():
    host = OkfHost(_mem(), _FakeBrain([]))
    raw = host.raw("/index.md")
    assert raw is not None and 'okf_version: "0.1"' in raw
