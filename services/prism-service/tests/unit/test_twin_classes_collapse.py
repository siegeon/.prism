"""Twin classes collapse into one class each (task cacfb628, epic 61821448).

Owner decision 2026-08-27, via the Extract Superclass article: "pull up
only what is truly common"; a parent one child alone uses is Refused
Bequest; when the shared slice is everything "the parent is the real
class, hiding in two costumes". The ontology rule `twin-classes` fires on
two classes that share an identical non-empty instance set.

Four candidate parent/child pairs were named at authoring time. This
suite's own premise check (grep across model.ttl/model-*.ttl and every
instance-producing site, repeated here as live assertions) found only
ONE genuine twin:

  - o:QueueItem / o:Signal -- TRUE TWIN. o:QueueItem declared exactly one
    subclass (o:Signal), and every instance-producing site (SIGNALS, task
    785bb4ce) already types instances as o:Signal only. COLLAPSED:
    o:QueueItem removed from the TBox; o:Signal keeps its own label and
    comment (folding in the parent's "what arrived, and where from"
    sense); every property that named o:QueueItem as its domain
    (o:raises, o:aboutTicket, o:aboutCode, o:permalink) now names
    o:Signal directly.
  - o:Work / o:PullRequest -- NOT a twin. o:Work declares TWO subclasses
    (o:JiraIssue AND o:PullRequest) -- a real second sibling, not a
    Refused Bequest. Left alone.
  - o:Party / o:Person -- NOT a twin. o:Party declares THREE subclasses
    (o:Person, o:Group, o:OutsideParty). Left alone.
  - o:Ask / o:AskForDecision -- NOT a twin. o:Ask declares FIVE subclasses
    (o:AskForInformation, o:AskForDecision, o:AskForReview,
    o:AskForDeliverable, o:AskFyi). Left alone.

Re-extracting a parent is for the day a second sibling exists under it --
not something to do here for Work/Party/Ask, which already have their
second (and third, and fourth) sibling.
"""

from __future__ import annotations

import re
import sys
import uuid
from pathlib import Path

import rdflib

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_ONTOLOGY_DIR = _SERVICE_ROOT / "prism_service" / "ontology"
_MODEL_TTL = _ONTOLOGY_DIR / "model.ttl"

from prism_service.services.ontology_graph import NS  # noqa: E402

_O = rdflib.Namespace(NS)
_SH = rdflib.Namespace("http://www.w3.org/ns/shacl#")
_RDFS = rdflib.RDFS

# The four parents the owner named at authoring time, and which of them
# survived the premise check as a genuine twin.
_COLLAPSED_PARENTS = ("QueueItem",)
_NOT_TWINS = {
    "Work": {"JiraIssue", "PullRequest"},
    "Party": {"Person", "Group", "OutsideParty"},
    "Ask": {"AskForInformation", "AskForDecision", "AskForReview",
            "AskForDeliverable", "AskFyi"},
}


def _tbox() -> rdflib.Graph:
    g = rdflib.Graph()
    g.parse(str(_MODEL_TTL), format="turtle")
    return g


# ---------------------------------------------------------------------------
# The loaded model declares none of the collapsed parents, and declares
# each concrete class with no leftover rdfs:subClassOf to a parent that no
# longer exists.
# ---------------------------------------------------------------------------

def test_tbox_declares_no_collapsed_parent_and_declares_signal():
    g = _tbox()
    classes = set(g.subjects(rdflib.RDF.type, _RDFS.Class))

    for parent in _COLLAPSED_PARENTS:
        assert _O[parent] not in classes, f"o:{parent} must not be declared"
        assert (None, None, _O[parent]) not in g, (
            f"o:{parent} must not appear anywhere in the TBox, "
            "not even as an object")

    assert _O.Signal in classes
    assert (_O.Signal, _RDFS.subClassOf, None) not in g, (
        "o:Signal must carry no rdfs:subClassOf now that its one "
        "parent (o:QueueItem) is gone"
    )

    # Every property that used to name o:QueueItem as its domain now
    # names o:Signal.
    for prop in ("raises", "aboutTicket", "aboutCode", "permalink"):
        assert (_O[prop], _RDFS.domain, _O.Signal) in g, prop


def test_work_party_ask_remain_multi_child_and_were_left_alone():
    """The three pairs the owner also named are NOT twins -- each parent
    keeps two or more declared subclasses, so none was collapsed."""
    g = _tbox()
    for parent, children in _NOT_TWINS.items():
        assert _O[parent] in set(g.subjects(rdflib.RDF.type, _RDFS.Class)), parent
        declared_children = {
            str(s).rsplit(":", 1)[-1]
            for s in g.subjects(_RDFS.subClassOf, _O[parent])
        }
        assert children <= declared_children, (parent, declared_children)
        assert len(declared_children) >= 2, (
            f"o:{parent} has fewer than 2 declared children -- "
            "it would need re-checking as a twin candidate"
        )


# ---------------------------------------------------------------------------
# No shape in the loaded shapes graph targets or mentions a collapsed
# parent, in sh:targetClass or in any SPARQLConstraint's select/ask text.
# ---------------------------------------------------------------------------

def test_no_shape_targets_or_mentions_a_collapsed_parent():
    g = rdflib.Graph()
    for path in sorted(_ONTOLOGY_DIR.glob("shapes*.ttl")):
        g.parse(str(path), format="turtle")

    for parent in _COLLAPSED_PARENTS:
        assert list(g.triples((None, _SH.targetClass, _O[parent]))) == [], parent

    for _s, _p, sel in list(g.triples((None, _SH.select, None))) + list(
        g.triples((None, _SH.ask, None))
    ):
        for parent in _COLLAPSED_PARENTS:
            assert parent not in str(sel), (parent, str(sel)[:200])


# ---------------------------------------------------------------------------
# Rules that target the classes at either end of the collapse still fire
# on their own violating fixture and stay quiet on their compliant one --
# reused from test_rules_are_shacl_shapes.py's own RULE_FIXTURES (no rule
# ever targeted o:QueueItem directly -- confirmed above -- so this pins
# the rules that target o:Signal itself, the class the collapse landed
# on, plus o:Ask, the multi-child parent left standing).
# ---------------------------------------------------------------------------

sys.path.insert(0, str(_HERE.parent))
from test_rules_are_shacl_shapes import RULE_FIXTURES, _graph  # noqa: E402


def test_rules_targeting_the_concrete_class_still_fire():
    from prism_service.services import ontology_rules

    for rule_name in ("ask-comes-from-the-queue", "flagged-signal-is-placed",
                       "ask-names-a-person"):
        compliant_snippet, violating_snippet, focus_local = RULE_FIXTURES[rule_name]

        _inferred, quiet = ontology_rules.run_shapes(_graph(compliant_snippet))
        assert rule_name not in quiet, (rule_name, quiet.get(rule_name))

        _inferred, bad = ontology_rules.run_shapes(_graph(violating_snippet))
        assert rule_name in bad, rule_name
        assert any(f.endswith(focus_local) for f in bad[rule_name]), (
            rule_name, bad[rule_name], focus_local)


# ---------------------------------------------------------------------------
# A rebuilt scratch project counts Signal / Person / AskForDecision /
# PullRequest on the Structure tab (all four concrete classes named in
# the task), and twin-classes reports zero violations there.
# ---------------------------------------------------------------------------

def test_rebuilt_project_counts_concrete_classes_and_twin_classes_is_quiet(monkeypatch):
    """twin-classes fires on a project's own INSTANCE SETS (via the
    targeted rdfs:subClassOf expansion, task 6503d7f8), not on the TBox
    taxonomy alone -- so o:Work/o:Ask, both left multi-child, must
    actually see a second sibling instance in THIS project's data, or
    they read as an (unrelated, pre-existing) twin of whichever single
    child happens to be seeded. Two signals give o:Work a JiraIssue AND a
    PullRequest, and o:Ask an AskForDecision AND an AskForReview. o:Party
    has no such second-sibling source anywhere in production code (no
    emitter ever types o:Group/o:OutsideParty) -- one Group instance is
    seeded directly into the project's own ABox graph, the same graph
    rebuild() itself writes to, purely to exercise a genuinely
    multi-child o:Party the way real data already exercises o:Work."""
    import pyoxigraph as ox

    from prism_service.models.signal import Signal
    from prism_service.services import ontology_rules
    from prism_service.services.ontology_graph import NS as _MODEL_NS
    from prism_service.services.ontology_graph import OntologyGraph
    from prism_service.services.signal_resolver import resolve
    from prism_service.services.signal_store import SignalStore

    monkeypatch.setenv("PRISM_KNOWN_JIRA_PROJECTS", "PLAT")

    project = f"twin-collapse-{uuid.uuid4().hex[:8]}"
    store = SignalStore(project)
    s1 = store.create(Signal(
        project=project, channel="slack", subject="Needs a decision",
        body="Please approve PLAT-42 and PR #17. "
             "Ping jane.doe@example.com with questions.",
        arrived_at="2026-08-27T12:00:00+00:00",
    ))
    store.update(s1.id, matches=resolve(project, s1))
    s2 = store.create(Signal(
        project=project, channel="slack", subject="Could you review this?",
        body="Please review the change.",
        arrived_at="2026-08-27T12:05:00+00:00",
    ))
    store.update(s2.id, matches=resolve(project, s2))
    store.close()

    graph = OntologyGraph(project)
    graph.rebuild()

    extra_ttl = (
        f"@prefix o: <{_MODEL_NS}> .\n"
        f"@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
        f"<{_MODEL_NS}instance/group/eng-team> a o:Group ; "
        f'rdfs:label "eng-team" .\n'
    )
    graph._store.load(input=extra_ttl, format=ox.RdfFormat.TURTLE,
                       to_graph=graph._abox_iri)

    structure = {row["id"]: row for row in graph.structure()["classes"]}
    for cls in ("Signal", "Person", "AskForDecision", "PullRequest"):
        assert cls in structure, (cls, sorted(structure))
        assert structure[cls]["own_count"] >= 1, (cls, structure[cls])
    assert "QueueItem" not in structure

    report = {row["name"]: row for row in ontology_rules.validate(project)}
    assert "twin-classes" in report
    assert report["twin-classes"]["state"] == "quiet", report["twin-classes"]


# ---------------------------------------------------------------------------
# No .py/.tsx under prism_service constructs, queries, or names a
# collapsed parent as a live class IRI -- historical prose (a comment or
# docstring explaining the collapse, several of which this task itself
# added) is expected and tolerated; a Python string literal or attribute
# access actually BUILDING an o:QueueItem reference is not.
# ---------------------------------------------------------------------------

_LIVE_CLASS_PATTERNS = [
    re.compile(r'"id":\s*"QueueItem"'),
    re.compile(r"_add_class\([^)]*[\"']QueueItem[\"']"),
    re.compile(r'class_id\s*=\s*[\"\']QueueItem[\"\']'),
    re.compile(r"\bCLS\(\s*[\"']QueueItem[\"']\s*\)"),
    re.compile(r"\b(?:_O|NS|O)\.QueueItem\b"),
    re.compile(r"\bNS\[[\"']QueueItem[\"']\]"),
    re.compile(r"rdfs:subClassOf\s+o:QueueItem"),
]

# Files this task's own hard rules forbid touching, or that are pure
# historical changelog -- excluded from the "no live reference" scan
# below, never from the earlier, stricter TBox/shapes checks above.
_EXCLUDED_PY = {
    _SERVICE_ROOT / "prism_service" / "services" / "ontology_rules.py",
    _SERVICE_ROOT / "prism_service" / "__version__.py",
}


def test_no_python_or_tsx_constructs_a_collapsed_parent_as_a_class():
    service_root = _SERVICE_ROOT / "prism_service"
    offenders: list[str] = []

    for path in service_root.rglob("*.py"):
        if path in _EXCLUDED_PY:
            continue
        text = path.read_text(encoding="utf-8")
        for pattern in _LIVE_CLASS_PATTERNS:
            for m in pattern.finditer(text):
                lineno = text.count("\n", 0, m.start()) + 1
                offenders.append(f"{path}:{lineno}: {m.group(0)}")

    for path in service_root.rglob("*.tsx"):
        text = path.read_text(encoding="utf-8")
        if "QueueItem" in text:
            offenders.append(f"{path}: literal QueueItem in TSX source")

    assert not offenders, offenders


def test_a_parent_with_two_declared_children_is_not_a_twin_of_the_populated_one():
    """task cacfb628: Work declares JiraIssue and PullRequest; with only
    PullRequests in the data, Work and PullRequest share every instance,
    but Work is a parent waiting for its second child, not a twin. A parent
    with ONE declared child that holds every instance IS a twin."""
    import rdflib
    from prism_service.services.ontology_rules import (
        _mark_twin_classes, _expand_subclass_types, _TWIN_MARKER)
    from prism_service.services.ontology_graph import NS

    O = rdflib.Namespace(NS)
    RDF, RDFS = rdflib.RDF, rdflib.RDFS
    g = rdflib.Graph()
    for c in ("Work", "JiraIssue", "PullRequest", "Lone", "OnlyChild"):
        g.add((O[c], RDF.type, RDFS.Class))
    g.add((O.JiraIssue, RDFS.subClassOf, O.Work))
    g.add((O.PullRequest, RDFS.subClassOf, O.Work))
    g.add((O.OnlyChild, RDFS.subClassOf, O.Lone))
    for i in range(3):
        g.add((O[f"pr{i}"], RDF.type, O.PullRequest))
        g.add((O[f"pr{i}"], RDF.type, O.Work))
        g.add((O[f"oc{i}"], RDF.type, O.OnlyChild))
        g.add((O[f"oc{i}"], RDF.type, O.Lone))
    _mark_twin_classes(g, None)
    marked = {str(s).rsplit(":", 1)[-1] for s in g.subjects(_TWIN_MARKER, None)}
    assert "Work" not in marked and "PullRequest" not in marked
    assert marked == {"Lone", "OnlyChild"}
