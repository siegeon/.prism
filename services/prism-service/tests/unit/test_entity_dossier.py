"""A dossier must describe WHAT THE THING IS, name its sources, and admit gaps.

Two defects this pins, both found by opening the real page:

  * The first cut asked only the CODE questions, so selecting a task — which
    is what Explore opens on — produced four empty sections in a row
    ("resolved to no source file", "no symbol to look up", ...). A task has
    contents; they were simply never asked for.
  * A section that cannot answer must say why. An empty success reads as
    "there is nothing here" when the truth is "this store was unreachable".
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
        # A document has a title; a code entity has title=None + entity_kind,
        # and its domain is the language as often as the generic "code".
        return [
            {"title": None, "domain": "code", "entity_kind": "function",
             "source_file": "tests/test_a.py"},
            {"title": "_services", "domain": "py", "entity_kind": "function",
             "source_file": "b.py"},
            {"title": "why the conductor exists", "domain": "md",
             "source_file": "docs/conductor.md"},
            {"title": "why the conductor exists", "domain": "md",
             "source_file": "docs/conductor.md"},
        ]


class _Graph:
    def file_detail(self, path):
        return {"entities": [1, 2, 3],
                "in_edges": [{"from": "a.py", "weight": 4}],
                "out_edges": [{"to": "b.py", "weight": 2}]}

    def file_communities(self, files):
        return {files[0]: 1}


class _Task:
    id = "b9772333"
    title = "Remove Simulate Flow button"
    status = "blocked"
    workflow = "implement"
    workflow_step = "green_gate"
    gate_state = "pending"
    parent_id = ""
    proof_type = "test"
    oracle = "Navigate to /workflows and see no Simulate button"


class _TaskSvc:
    def __init__(self, children=()):
        self._children = list(children)

    def get(self, task_id):
        return _Task() if task_id == _Task.id else None

    def list(self, parent_id=None):
        return self._children


def _titles(d):
    return [s["title"] for s in d["sections"]]


def _section(d, title):
    return next(s for s in d["sections"] if s["title"] == title)


@pytest.fixture()
def as_symbol(monkeypatch):
    monkeypatch.setattr(
        "prism_service.api.xref.resolve_token",
        lambda token, m, b, graph_svc=None, task_svc=None, conductor_svc=None: {
            "kind": "symbol", "label": token,
            "href": f"/artifact?focus=services/conductor_service.py&symbol={token}",
        })
    monkeypatch.setattr(ed, "_ontology",
                        lambda p, k: ed._unavailable("ontology", "Ontology",
                                                     "ontology store", "locked"))


@pytest.fixture()
def as_task(monkeypatch):
    monkeypatch.setattr(
        "prism_service.api.xref.resolve_token",
        lambda token, m, b, graph_svc=None, task_svc=None, conductor_svc=None: {
            "kind": "task", "label": _Task.title, "href": f"/tasks/{token}"})


# ------------------------------------------- the sections fit the subject

def test_a_task_is_described_by_its_own_contents(as_task, monkeypatch):
    """The defect: a task got the CODE questions and answered nothing."""
    monkeypatch.setattr(ed, "_ontology",
                        lambda p, k: ed._section("ontology", "Ontology",
                                                 "ontology store",
                                                 [ed._row("Class", k)]))
    monkeypatch.setattr(ed, "_task_knowledge",
                        lambda m, b, t: ed._section("knowledge", "Knowledge it pulled in",
                                                    "curated memory (recall log)", []))
    d = ed.dossier(_Task.id, "prism", task_svc=_TaskSvc(), brain_svc=_Brain())
    assert _titles(d)[:3] == ["Task", "Knowledge it pulled in", "Work under it"]
    assert "Code graph" not in _titles(d)
    assert "Symbols" not in _titles(d)


def test_a_task_reports_where_it_is_in_the_workflow(as_task, monkeypatch):
    monkeypatch.setattr(ed, "_ontology", lambda p, k: ed._unavailable(
        "ontology", "Ontology", "ontology store", "x"))
    monkeypatch.setattr(ed, "_task_knowledge", lambda m, b, t: ed._section(
        "knowledge", "Knowledge it pulled in", "recall log", []))
    task = _section(ed.dossier(_Task.id, "prism", task_svc=_TaskSvc(),
                               brain_svc=_Brain()), "Task")
    got = {r["label"]: r["text"] for r in task["rows"]}
    assert got["Status"] == "blocked"
    assert got["Step"] == "green_gate"
    assert got["Gate"] == "pending"


def test_a_tasks_children_are_links(as_task, monkeypatch):
    monkeypatch.setattr(ed, "_ontology", lambda p, k: ed._unavailable(
        "ontology", "Ontology", "ontology store", "x"))
    monkeypatch.setattr(ed, "_task_knowledge", lambda m, b, t: ed._section(
        "knowledge", "Knowledge it pulled in", "recall log", []))
    kid = type("K", (), {"id": "child-1", "title": "a slice", "status": "done"})()
    work = _section(ed.dossier(_Task.id, "prism", task_svc=_TaskSvc([kid]),
                               brain_svc=_Brain()), "Work under it")
    assert work["rows"][0]["text"] == "a slice"
    assert work["rows"][0]["href"] == "/tasks/child-1"


def test_code_is_described_by_the_code_readings(as_symbol):
    d = ed.dossier("ConductorService", "prism",
                   brain_svc=_Brain(), graph_svc=_Graph())
    assert "Code graph" in _titles(d) and "Symbols" in _titles(d)
    assert "Task" not in _titles(d)


# ----------------------------------------------- every part is attributed

def test_every_section_names_the_store_it_read(as_symbol):
    d = ed.dossier("ConductorService", "prism",
                   brain_svc=_Brain(), graph_svc=_Graph())
    for s in d["sections"]:
        assert s["source"], f"{s['title']} does not say where it came from"


def test_a_store_that_cannot_answer_gives_a_reason(as_symbol):
    class _Locked:
        def file_detail(self, path):
            raise OSError("graph.db is locked")

        def file_communities(self, files):
            return {}

    g = _section(ed.dossier("X", "prism", brain_svc=_Brain(), graph_svc=_Locked()),
                 "Code graph")
    assert g["ok"] is False and "locked" in g["reason"]


def test_a_missing_subsystem_is_named_not_silently_skipped(as_symbol):
    g = _section(ed.dossier("X", "prism", brain_svc=_Brain(), graph_svc=None),
                 "Code graph")
    assert g["ok"] is False and "no code graph" in g["reason"]


# ------------------------------------------------------------ the details

def test_the_call_chain_names_the_other_party_not_the_entity(as_symbol):
    """For callers the counterpart is `from`; for callees it is `to`."""
    sy = _section(ed.dossier("ConductorService", "prism", brain_svc=_Brain(),
                             graph_svc=_Graph()), "Symbols")
    text = " ".join(r["text"] for r in sy["rows"])
    assert "conductor_svc()" in text and "_cond()" in text
    assert "L131" in text, "a call edge carries the line a reader can check"


def test_the_brain_section_does_not_restate_the_symbol_index(as_symbol):
    br = _section(ed.dossier("X", "prism", brain_svc=_Brain(), graph_svc=_Graph()),
                  "Written about it")
    assert [r["text"] for r in br["rows"]] == ["why the conductor exists"]


def test_no_prose_is_reported_with_what_was_searched(as_symbol):
    class _CodeOnly(_Brain):
        def search(self, q, limit=8):
            return [{"title": None, "domain": "code", "entity_kind": "function",
                     "source_file": "a.py"}]

    br = _section(ed.dossier("X", "prism", brain_svc=_CodeOnly(),
                             graph_svc=_Graph()), "Written about it")
    assert "nothing written about this" in br["rows"][0]["text"]
    assert "1 index rows" in br["rows"][0]["text"]


def test_a_task_carries_its_ontology_class(as_task, monkeypatch):
    """A task reported 'carries no ontology class yet' while the ontology
    held 924 instances of Task."""
    seen = {}

    def _spy(project, klass):
        seen["klass"] = klass
        return ed._section("ontology", "Ontology", "ontology store", [])

    monkeypatch.setattr(ed, "_ontology", _spy)
    monkeypatch.setattr(ed, "_task_knowledge", lambda m, b, t: ed._section(
        "knowledge", "Knowledge it pulled in", "recall log", []))
    ed.dossier(_Task.id, "prism", task_svc=_TaskSvc(), brain_svc=_Brain())
    assert seen["klass"] == "Task"


def test_an_unresolvable_token_still_returns_sections(monkeypatch):
    monkeypatch.setattr(
        "prism_service.api.xref.resolve_token",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    d = ed.dossier("nonsense", "prism")
    assert d["kind"] == "unresolved"
    assert d["sections"] and all(s["source"] for s in d["sections"])
