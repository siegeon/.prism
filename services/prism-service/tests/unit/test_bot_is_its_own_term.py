"""Bot is its own term with two tiers, not a synonym of Agent (task
8bcd4cb3, epic 12029f92, owner 2026-08-27 mx-0e5a88: the top level is a
Bot, a tier-1 deterministic workflow; agentic Behaviors sit under it).

Red until model.ttl declares o:Bot/o:FSM/o:State/o:Transition/o:Behavior
with their relations and model-lexicon.ttl carries the five Term rows.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import rdflib

_SERVICE_ROOT = Path(__file__).resolve().parent.parent.parent
_ONTO = _SERVICE_ROOT / "prism_service" / "ontology"
_NS = "urn:prism:onto:"
O = rdflib.Namespace(_NS)
RDF, RDFS, XSD = rdflib.RDF, rdflib.RDFS, rdflib.XSD
CLASSES = ("Bot", "FSM", "State", "Transition", "Behavior")
RELATIONS = {  # property -> (domain, range)
    "runsFsm": ("Bot", "FSM"), "hasState": ("FSM", "State"),
    "hasTransition": ("FSM", "Transition"), "fromState": ("Transition", "State"),
    "toState": ("Transition", "State"), "partOf": ("Behavior", "FSM"),
}
TITLE = "Bot is its own term with two tiers, not a synonym of Agent"


def _graph(name: str) -> rdflib.Graph:
    g = rdflib.Graph()
    g.parse(str(_ONTO / name), format="turtle")
    return g


def test_model_declares_the_five_classes():
    """AC-1: five classes with label + comment; Bot is not under Agent."""
    g = _graph("model.ttl")
    for name in CLASSES:
        assert (O[name], RDF.type, RDFS.Class) in g, f"o:{name} is not a class"
        assert str(g.value(O[name], RDFS.label) or ""), f"o:{name} has no label"
        assert str(g.value(O[name], RDFS.comment) or ""), f"o:{name} has no comment"
    assert (O.Bot, RDFS.subClassOf, O.Agent) not in g, "o:Bot must not subclass o:Agent"


def test_relations_bind_bot_fsm_state_transition_behavior():
    """AC-2: Bot runs an FSM of States and Transitions; Behavior is partOf."""
    g = _graph("model.ttl")
    for prop, (dom, rng) in RELATIONS.items():
        assert (O[prop], RDFS.domain, O[dom]) in g, f"o:{prop} domain is not o:{dom}"
        assert (O[prop], RDFS.range, O[rng]) in g, f"o:{prop} range is not o:{rng}"
        assert str(g.value(O[prop], RDFS.label) or ""), f"o:{prop} has no label"
    assert (O.tier, RDFS.range, XSD.integer) in g, "o:tier range is not xsd:integer"


def test_lexicon_knows_the_five_terms():
    """AC-3: load_lexicon() returns the five Terms, each denoting its class."""
    from prism_service.services.lexicon import load_lexicon

    by_label = {t.label: t for t in load_lexicon()}
    for name in CLASSES:
        assert name in by_label, f"lexicon has no term labelled {name}"
        assert by_label[name].denotes == name, f"term {name} denotes {by_label[name].denotes!r}"
    assert "finite state machine" in by_label["FSM"].alt_labels
    assert "behaviour" in by_label["Behavior"].alt_labels


def test_bot_is_not_an_agent_synonym():
    """AC-4: no Term lists bot as a synonym, and the aligner leaves it alone."""
    from prism_service.services import lexicon

    for term in lexicon.load_lexicon():
        assert "bot" not in {a.lower() for a in term.alt_labels}, f"{term.label} lists bot"
    text, _changes = lexicon.align(TITLE)
    assert text == TITLE


def test_task_create_keeps_bot_in_the_title(tmp_path):
    """AC-5: task_create with Bot in the title stores Bot (align runs at create)."""
    from prism_service.services.task_service import TaskService

    svc = TaskService(db_path=str(tmp_path / "tasks.db"))
    task = svc.create(title=TITLE)
    stored = svc.get(task.id).title
    assert stored.startswith("Bot"), stored
    assert "Agent is its own term" not in stored, stored


def test_structure_and_vocabulary_expose_bot():
    """AC-7: Structure tab counts the new classes; Vocabulary tab lists Bot."""
    from prism_service.api import okf
    from prism_service.project_context import get_project
    from prism_service.services import ontology_terms

    pid = f"ontology-bot-{uuid.uuid4().hex[:8]}"
    get_project(pid).task_svc.create(title="seed task", channel="ui")
    by_id = {c["id"]: c for c in okf.ontology_structure(project=pid)["classes"]}
    for name in CLASSES:
        assert name in by_id, f"Structure tab has no class {name}"
        assert by_id[name]["comment"], f"{name} has no comment"
        assert by_id[name]["count"] >= 0
    vocab = [v for v in ontology_terms.terms(pid)["vocabularies"] if v["name"] == "lexicon"]
    assert vocab, "no lexicon vocabulary"
    assert "Bot" in {row["value"] for row in vocab[0]["terms"]}, "Vocabulary tab lacks Bot"
