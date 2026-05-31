"""Unit tests for the Claude Code auto-memory -> PRISM bridge (v6.2.16).

Covers frontmatter parsing, memory-dir resolution precedence (override
arg -> per-project claude_memory_path -> slug auto), the skip rules,
type mapping, and idempotency — exercised through the same
.list_entries/.store surface the transcript poller and API route use.
"""

from dataclasses import dataclass
from pathlib import Path

from prism_service.services import claude_memory as cm


@dataclass
class _Entry:
    id: str
    name: str
    description: str
    domain: str = ""
    invalid_at: str = ""


class _StubMemorySvc:
    """Minimal stand-in for MemoryService — just enough surface for
    import_project_memories (list_entries + store)."""

    def __init__(self, existing: list[_Entry] | None = None):
        self._existing = existing or []
        self.stored: list[dict] = []

    def list_entries(self, domain, status_filter="active", **kw):
        return [e for e in self._existing if e.domain == domain]

    def store(self, **kw):
        e = _Entry(id=f"id-{len(self.stored)}", name=kw["name"],
                   description=kw["description"], domain=kw["domain"])
        self.stored.append(kw)
        return e


def _write_mem(d: Path, fname: str, *, name: str, mtype: str,
               description: str = "desc", body: str = "body") -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / fname).write_text(
        f"---\nname: {name}\ndescription: {description}\n"
        f"metadata:\n  type: {mtype}\n---\n{body}\n",
        encoding="utf-8",
    )


def test_parse_valid(tmp_path):
    _write_mem(tmp_path, "a.md", name="rule-one", mtype="feedback",
               description="be careful", body="the why")
    parsed = cm.parse_claude_memory(tmp_path / "a.md")
    assert parsed == {
        "name": "rule-one", "description": "be careful",
        "body": "the why", "metatype": "feedback",
    }


def test_parse_no_frontmatter(tmp_path):
    (tmp_path / "x.md").write_text("no frontmatter here", encoding="utf-8")
    assert cm.parse_claude_memory(tmp_path / "x.md") is None


def test_import_skip_rules_and_mapping(tmp_path):
    mem = tmp_path / "memory"
    _write_mem(mem, "good.md", name="rule-one", mtype="feedback")
    _write_mem(mem, "proj.md", name="proj-fact", mtype="project")
    _write_mem(mem, "MEMORY.md", name="index", mtype="feedback")   # skipped (aggregator)
    _write_mem(mem, "_scratch.md", name="scratch", mtype="feedback")  # skipped (underscore)
    _write_mem(mem, "weird.md", name="odd", mtype="bogus")          # skipped (unknown type)
    (mem / "plain.md").write_text("no frontmatter", encoding="utf-8")  # skipped

    svc = _StubMemorySvc()
    res = cm.import_project_memories("p", svc, override_dir=str(mem))

    assert res["imported"] == 2
    assert res["failed"] == 0
    assert res["skipped"] == 4
    # feedback -> (convention, tactical, procedural, 7)
    fb = next(s for s in svc.stored if s["name"] == "rule-one")
    assert (fb["type"], fb["classification"], fb["memory_type"],
            fb["importance"]) == ("convention", "tactical", "procedural", 7)
    # body is appended onto the description
    assert fb["description"] == "desc\n\nbody"


def test_import_no_dir(tmp_path):
    svc = _StubMemorySvc()
    res = cm.import_project_memories("p", svc, override_dir=str(tmp_path / "nope"))
    assert res == {
        "imported": 0, "skipped": 0, "failed": 0,
        "memory_dir": str(tmp_path / "nope"),
        "reason": "no memory directory for this project",
    }


def test_import_idempotent_skip_unchanged(tmp_path):
    mem = tmp_path / "memory"
    _write_mem(mem, "good.md", name="rule-one", mtype="feedback")
    existing = _Entry(id="e1", name="rule-one", description="desc\n\nbody",
                      domain="feedback")
    svc = _StubMemorySvc(existing=[existing])
    res = cm.import_project_memories("p", svc, override_dir=str(mem))
    assert res["imported"] == 0
    assert res["skipped"] == 1
    assert svc.stored == []


def test_resolve_precedence_override_wins(monkeypatch):
    monkeypatch.setattr(
        "prism_service.engines.understand_engine._read_state",
        lambda project: {"claude_project_dir": "/configured"},
    )
    got = cm.resolve_memory_dir("p", override="/explicit")
    assert got == Path("/explicit")


def test_resolve_precedence_config_over_auto(monkeypatch):
    # A configured claude_project_dir wins over slug auto, and the memory
    # dir is its `memory/` subdir.
    monkeypatch.setattr(
        "prism_service.engines.understand_engine._read_state",
        lambda project: {"claude_project_dir": "/configured",
                         "source_path": "E:\\.prism"},
    )
    assert cm.resolve_memory_dir("p") == Path("/configured") / "memory"
    assert cm.configured_project_dir("p") == "/configured"


def test_resolve_auto_from_slug(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "prism_service.engines.understand_engine._read_state",
        lambda project: {"source_path": "E:\\.prism"},
    )
    got = cm.resolve_memory_dir("p", claude_home=tmp_path)
    assert got == tmp_path / "projects" / "E---prism" / "memory"
