"""A dossier must say WHICH STORE each part came from, and admit gaps.

The reason this exists: a person looking at an entity in Explore saw a
degree, a timestamp and "unclassified" — they could see THAT a thing existed
without seeing what the code graph, the symbol index, the brain and the
ontology each held on it. The failure mode to guard is a section that comes
back empty and reads as "there is nothing here" when the truth is "this
store could not be reached".
"""

from __future__ import annotations

import pytest

from prism_service.services import entity_dossier as ed


class _Brain:
    def find_symbol(self, name, limit=10):
        return [{"entity_name": "ConductorService", "entity_kind": "class",
                 "source_file": "services/conductor_service.py"}]

    def call_chain(self, name, depth=1, limit=8, direction="callees"):
        if direction == "callers":
            return [{"from": "conductor_svc()", "to": name,
                     "call_site_file": "project_context.py",
                     "call_site_location": "L131", "confidence": "INFERRED"}]
        return [{"from": name, "to": "_cond()",
                 "call_site_file": "test_activity_state.py",
                 "call_site_location": "L30", "confidence": "INFERRED"}]

    def search(self, q, limit=8):
        return [{"title": "why the conductor exists",
                 "source_file": "docs/conductor.md", "domain": "architecture"}]


class _Graph:
    def file_detail(self, path):
        return {"entities": [1, 2, 3],
                "in_edges": [{"from": "a.py", "weight": 4}],
                "out_edges": [{"to": "b.py", "weight": 2}]}

    def file_communities(self, files):
        return {files[0]: 1}


@pytest.fixture()
def resolved_symbol(monkeypatch):
    monkeypatch.setattr(
        "prism_service.api.xref.resolve_token",
        lambda token, m, b, graph_svc=None, task_svc=None, conductor_svc=None: {
            "kind": "symbol", "label": token,
            "href": f"/artifact?focus=services/conductor_service.py&symbol={token}",
        })


# ------------------------------------------------- every part is attributed

def test_every_section_names_the_store_it_read(resolved_symbol, monkeypatch):
    monkeypatch.setattr(ed, "_ontology",
                        lambda p, k: ed._unavailable("ontology store", "locked"))
    d = ed.dossier("ConductorService", "prism",
                   brain_svc=_Brain(), graph_svc=_Graph())
    for part in ("code_graph", "symbols", "brain", "ontology"):
        assert d[part]["source"], f"{part} does not say where it came from"


def test_a_store_that_cannot_answer_gives_a_reason(monkeypatch, resolved_symbol):
    """An unreachable store must never render as an empty success."""
    class _Locked:
        def file_detail(self, path):
            raise OSError("graph.db is locked")

        def file_communities(self, files):
            return {}

    monkeypatch.setattr(ed, "_ontology",
                        lambda p, k: ed._unavailable("ontology store", "locked"))
    d = ed.dossier("X", "prism", brain_svc=_Brain(), graph_svc=_Locked())
    assert d["code_graph"]["ok"] is False
    assert "locked" in d["code_graph"]["reason"]


def test_a_missing_subsystem_is_named_not_silently_skipped(monkeypatch, resolved_symbol):
    monkeypatch.setattr(ed, "_ontology",
                        lambda p, k: ed._unavailable("ontology store", "no class"))
    d = ed.dossier("X", "prism", brain_svc=_Brain(), graph_svc=None)
    assert d["code_graph"]["ok"] is False
    assert "no code graph" in d["code_graph"]["reason"]


# --------------------------------------------------------- the call chain

def test_the_call_chain_names_the_other_party_not_the_entity(resolved_symbol, monkeypatch):
    """For callers the counterpart is `from`; for callees it is `to`. Reading
    the wrong end produced rows whose name was blank."""
    monkeypatch.setattr(ed, "_ontology", lambda p, k: ed._unavailable("o", "x"))
    d = ed.dossier("ConductorService", "prism",
                   brain_svc=_Brain(), graph_svc=_Graph())
    sy = d["symbols"]
    assert [c["name"] for c in sy["callers"]] == ["conductor_svc()"]
    assert [c["name"] for c in sy["callees"]] == ["_cond()"]
    assert all(c["name"] for c in sy["callers"] + sy["callees"])


def test_a_call_edge_carries_its_site_and_its_confidence(resolved_symbol, monkeypatch):
    """These edges are INFERRED. A reader checking one wants the line, and
    deserves to know the claim is inferred rather than proven."""
    monkeypatch.setattr(ed, "_ontology", lambda p, k: ed._unavailable("o", "x"))
    d = ed.dossier("ConductorService", "prism",
                   brain_svc=_Brain(), graph_svc=_Graph())
    caller = d["symbols"]["callers"][0]
    assert caller["file"] == "project_context.py"
    assert caller["line"] == "L131"
    assert caller["confidence"] == "INFERRED"


# ------------------------------------------------------------ the readings

def test_the_code_graph_reading_locates_and_counts(resolved_symbol, monkeypatch):
    monkeypatch.setattr(ed, "_ontology", lambda p, k: ed._unavailable("o", "x"))
    g = ed.dossier("ConductorService", "prism",
                   brain_svc=_Brain(), graph_svc=_Graph())["code_graph"]
    assert g["file"] == "services/conductor_service.py"
    assert g["community"] == 1
    assert g["inbound"] == 1 and g["outbound"] == 1 and g["entities"] == 3


def test_the_brain_reading_lists_what_was_written(resolved_symbol, monkeypatch):
    monkeypatch.setattr(ed, "_ontology", lambda p, k: ed._unavailable("o", "x"))
    br = ed.dossier("ConductorService", "prism",
                    brain_svc=_Brain(), graph_svc=_Graph())["brain"]
    assert br["mentions"][0]["title"] == "why the conductor exists"


def test_an_unresolvable_token_still_returns_every_section(monkeypatch):
    monkeypatch.setattr(
        "prism_service.api.xref.resolve_token",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    d = ed.dossier("nonsense", "prism")
    assert d["kind"] == "unresolved"
    for part in ("code_graph", "symbols", "brain", "ontology"):
        assert part in d and d[part]["source"]
