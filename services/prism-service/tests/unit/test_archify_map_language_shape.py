"""The language map must draw PRISM's real vocabulary, not only the classes
that happen to have rows today.

The ontology store is single-writer, so an out-of-process build cannot open it
while the daemon is running. These tests inject the SAME shapes the live
/api/okf/ontology endpoint returns, so the builder's judgment is exercised
without the store.
"""

from __future__ import annotations

import shutil
import subprocess
import json
import tempfile
from pathlib import Path

import pytest

from prism_service.services.archify_maps import language
from prism_service.vendor.archify_paths import ARCHIFY_BIN, ARCHIFY_DIR, node_executable

# Real rows, abridged, from GET /api/okf/ontology?project=prism.
_CLASSES = [
    {"id": "Document", "name": "Document", "instance_count": 959, "source": "brain",
     "description": "What work produced."},
    {"id": "Task", "name": "Task", "instance_count": 525, "source": "prism"},
    {"id": "Signal", "name": "Signal", "instance_count": 40, "source": "prism"},
    {"id": "Agent", "name": "Agent", "instance_count": 12, "source": "prism"},
    {"id": "Folder", "name": "Folder", "instance_count": 88, "source": "brain"},
    {"id": "Provider", "name": "Provider", "instance_count": 3, "source": "prism"},
]
_PROPERTIES = [
    {"id": "prop::becameTask", "name": "becameTask", "domain_class": "Signal",
     "range_class": "Task"},
    {"id": "prop::hasWorkflow", "name": "hasWorkflow", "domain_class": "Task",
     "range_class": "Agent"},
    {"id": "prop::inFolder", "name": "inFolder", "domain_class": "Document",
     "range_class": "Folder"},
    {"id": "prop::invokes", "name": "invokes", "domain_class": "Agent",
     "range_class": "Provider"},
    {"id": "prop::parent", "name": "parent", "domain_class": "Task",
     "range_class": "Task"},
    # Vocabulary that has no instances yet — still part of the language.
    {"id": "prop::askedBy", "name": "askedBy", "domain_class": "Ask",
     "range_class": "Party"},
    # A literal range, which is not a class and must never be drawn.
    {"id": "prop::arrivedAt", "name": "arrivedAt", "domain_class": "Signal",
     "range_class": "rg/2001/XMLSchema#dateTime"},
]
_RULES = {"rules": [
    {"name": "one-tier-down", "title": "One tier up decides each gate", "violations": 2},
    {"name": "plain-text", "title": "Text is plain", "violations": 0},
]}


class _FakeGraph:
    def __init__(self, project): pass
    def classes(self): return _CLASSES
    def properties(self): return _PROPERTIES


@pytest.fixture()
def ir(monkeypatch):
    import prism_service.services.ontology_graph as og
    monkeypatch.setattr(og, "OntologyGraph", _FakeGraph)
    import prism_service.services.rule_decisions as rd
    monkeypatch.setattr(rd, "decorated_report", lambda project: _RULES)
    return language.build("prism")


def _labels(ir):
    return {c["label"] for c in ir["components"]}


def test_a_class_with_no_instances_is_still_vocabulary(ir):
    """Ask and Party have no rows, and are still part of the language."""
    assert {"Ask", "Party"} <= _labels(ir)
    ask = next(c for c in ir["components"] if c["label"] == "Ask")
    assert ask["sublabel"] == "no instances yet"


def test_a_literal_range_is_never_drawn_as_a_class(ir):
    assert not any("XMLSchema" in c["label"] for c in ir["components"])


def test_relations_are_drawn(ir):
    assert ir["connections"], "the relations are the point of this map"
    for conn in ir["connections"]:
        assert conn["from"] != conn["to"], "a self-relation has no line to draw"


def test_the_relation_names_are_still_reachable(ir):
    """The line carries no label, so the names must appear on a card."""
    cards = {c["title"]: c["items"] for c in ir["cards"]}
    assert any("becameTask" in i for i in cards["Relations drawn"])


def test_every_endpoint_resolves_to_a_component(ir):
    ids = {c["id"] for c in ir["components"]}
    for conn in ir["connections"]:
        assert conn["from"] in ids and conn["to"] in ids


def test_failing_rules_are_named_not_counted(ir):
    cards = {c["title"]: c["items"] for c in ir["cards"]}
    assert any("One tier up decides each gate" in i for i in cards["Rules that fail"])
    assert any("Text is plain" in i for i in cards["Rules that hold"])


def test_an_unreadable_store_says_so_and_still_renders(monkeypatch):
    import prism_service.services.ontology_graph as og

    class _Locked:
        def __init__(self, project):
            raise OSError("LOCK: Resource temporarily unavailable")

    monkeypatch.setattr(og, "OntologyGraph", _Locked)
    out = language.build("prism")
    assert out["components"], "an empty map must still be a valid diagram"
    assert "could not be read" in out["meta"]["subtitle"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not on PATH")
def test_the_map_renders(ir):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(ir, fh)
        path = fh.name
    try:
        out = subprocess.run(
            [node_executable(), str(ARCHIFY_BIN), "validate", "architecture",
             path, "--json"],
            capture_output=True, text=True, timeout=180, cwd=str(ARCHIFY_DIR),
        )
        assert json.loads(out.stdout).get("ok") is True, out.stdout[:800]
    finally:
        Path(path).unlink(missing_ok=True)
