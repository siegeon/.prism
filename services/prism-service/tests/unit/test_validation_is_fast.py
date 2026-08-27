"""Ontology validation stays under ten seconds on a full code graph
(task 6503d7f8, epic 61821448). owlrl's full RDFS closure cost ~34s on
the prism project's real code graph (56518 -> 106656 triples,
measured 2026-08-27) inside services/ontology_rules.validate(), most
of it entailing rdfs:domain/range/subPropertyOf machinery no shape in
shapes.ttl / shapes-knowledge.ttl actually reads. This pins the
replacement (services/ontology_rules._expand_subclass_types): a plain
Python rdfs:subClassOf type expansion that (1) still lets a shape
targeting a superclass catch subclass instances, (2) never expands
o:Code or its subclasses (no shape targets o:Code), and (3) keeps the
whole validate() pass fast even on a 60k-triple seeded graph.
"""

from __future__ import annotations

import time

import rdflib

from prism_service.services import ontology_rules
from prism_service.services.ontology_graph import NS

_O = rdflib.Namespace(NS)
_RDF = rdflib.RDF
_RDFS = rdflib.RDFS


def _tbox() -> rdflib.Graph:
    """The real TBox files, the same ones to_rdflib() merges into every
    data graph — never a hand-rolled subset, so the subclass tree this
    test exercises is the real one."""
    from pathlib import Path

    ontology_dir = Path(ontology_rules.__file__).resolve().parent.parent / "ontology"
    g = rdflib.Graph()
    g.parse(str(ontology_dir / "model.ttl"), format="turtle")
    g.parse(str(ontology_dir / "model-knowledge.ttl"), format="turtle")
    return g


# ---------------------------------------------------------------------------
# _expand_subclass_types adds the superclass type a shape needs, for every
# subclass the real emitters actually use — no subprocess, no pyshacl,
# just the expansion function itself.
# ---------------------------------------------------------------------------

def test_expansion_adds_superclass_types_the_shapes_need():
    g = _tbox()
    sig = _O["signal/s1"]
    dec = _O["memory/mx1"]
    person = _O["person/p1"]
    ask = _O["ask/a1"]
    g.add((sig, _RDF.type, _O.Signal))
    g.add((dec, _RDF.type, _O.Decision))
    g.add((person, _RDF.type, _O.Person))
    g.add((ask, _RDF.type, _O.AskForDecision))

    ontology_rules._expand_subclass_types(g)

    assert (sig, _RDF.type, _O.QueueItem) in g, "Signal -> QueueItem"
    assert (dec, _RDF.type, _O.Concept) in g, "Decision -> Concept"
    assert (person, _RDF.type, _O.Party) in g, "Person -> Party"
    assert (ask, _RDF.type, _O.Ask) in g, "AskForDecision -> Ask"
    # AskForDecision's ancestor chain is two levels deep (Ask -> Activity)
    assert (ask, _RDF.type, _O.Activity) in g, "AskForDecision -> Activity (transitive)"


def test_expansion_skips_code_and_its_subclasses():
    g = _tbox()
    fn = _O["code/fn1"]
    g.add((fn, _RDF.type, _O.Function))
    before = len(g)

    ontology_rules._expand_subclass_types(g)

    # Function is a subclass of Code, but no shape targets o:Code, so no
    # (fn, rdf:type, o:Code) triple should ever be added.
    assert (fn, _RDF.type, _O.Code) not in g
    assert len(g) == before, "no triples added for a Code-graph instance"


def test_expansion_leaves_50k_seeded_code_triples_untouched():
    g = _tbox()
    tbox_size = len(g)
    n = 12_500  # 4 triples/instance -> 50,000 code triples
    for i in range(n):
        fn = _O[f"code/fn{i}"]
        g.add((fn, _RDF.type, _O.Function))
        g.add((fn, _RDFS.label, rdflib.Literal(f"fn{i}")))
        g.add((fn, _O.inFile, rdflib.Literal("a/b.py")))
        g.add((fn, _RDFS.comment, rdflib.Literal(f"fn{i}")))
    assert len(g) - tbox_size == 4 * n

    before = len(g)
    ontology_rules._expand_subclass_types(g)
    assert len(g) == before, "50k seeded o:Code triples must not grow the graph"


# ---------------------------------------------------------------------------
# twin-classes still fires on its own violating fixture and stays quiet on
# its compliant one (re-verifies the scoped-off SPARQL still behaves) —
# the exhaustive per-rule sweep lives in test_rules_are_shacl_shapes.py;
# this only re-checks the specific rule this task's shapes.ttl edit touched.
# ---------------------------------------------------------------------------

def test_twin_classes_still_fires_and_stays_quiet():
    prefixes = """
    @prefix o: <urn:prism:onto:> .
    @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
    """
    compliant = rdflib.Graph()
    compliant.parse(data=prefixes + (
        'o:ClassA a rdfs:Class . o:ClassB a rdfs:Class . '
        'o:x1 a o:ClassA . o:x2 a o:ClassB .'
    ), format="turtle", publicID=NS)
    _inferred, quiet = ontology_rules.run_shapes(compliant)
    assert "twin-classes" not in quiet

    violating = rdflib.Graph()
    violating.parse(data=prefixes + (
        'o:ClassA a rdfs:Class . o:ClassB a rdfs:Class . '
        'o:x1 a o:ClassA, o:ClassB .'
    ), format="turtle", publicID=NS)
    _inferred, bad = ontology_rules.run_shapes(violating)
    assert "twin-classes" in bad
    assert any(f.endswith("ClassA") for f in bad["twin-classes"])


def test_twin_classes_does_not_flag_code_graph_classes():
    """A real Function/Module/Class population, each with its own
    disjoint instance set, must never be reported as twins of each
    other or of anything else — and must not be scanned at all (task
    6503d7f8's shapes.ttl scoping)."""
    g = _tbox()
    for i in range(200):
        g.add((_O[f"code/fn{i}"], _RDF.type, _O.Function))
    for i in range(200):
        g.add((_O[f"code/mod{i}"], _RDF.type, _O.Module))

    _inferred, violations = ontology_rules.run_shapes(g)
    assert "twin-classes" not in violations


# ---------------------------------------------------------------------------
# The wall-clock bound: a 60k-triple seeded graph (mostly a synthetic code
# graph, plus a handful of knowledge/ABox instances) validates well inside
# a generous bound. This exercises the REAL path: run_shapes() -> child
# process -> pyshacl, not just the expansion helper in-process.
# ---------------------------------------------------------------------------

def test_60k_triple_graph_validates_under_a_generous_bound():
    g = _tbox()
    n = 15_000  # 4 triples/instance -> 60,000 code triples
    for i in range(n):
        fn = _O[f"code/fn{i}"]
        g.add((fn, _RDF.type, _O.Function))
        g.add((fn, _RDFS.label, rdflib.Literal(f"fn{i}")))
        g.add((fn, _O.inFile, rdflib.Literal("a/b.py")))
        g.add((fn, _RDFS.comment, rdflib.Literal(f"fn{i}")))
    # a handful of ABox instances so the expansion + shapes still do
    # real, non-code work on this graph too.
    for i in range(50):
        g.add((_O[f"task/t{i}"], _RDF.type, _O.Task))
        g.add((_O[f"task/t{i}"], _O.arrivedVia, _O[f"channel/ui"]))
        g.add((_O[f"signal/s{i}"], _RDF.type, _O.Signal))
        g.add((_O[f"signal/s{i}"], _O.state, rdflib.Literal("closed")))
    assert len(g) >= 60_000, len(g)

    start = time.monotonic()
    ontology_rules.run_shapes(g)
    elapsed = time.monotonic() - start

    # Generous bound (task 6503d7f8 target is "under ten seconds"; 15s
    # leaves headroom for a loaded CI box while still catching a
    # regression back to a full owlrl-style closure, which measured 34s
    # on a comparably-sized real project graph).
    assert elapsed < 15.0, f"validation took {elapsed:.1f}s, wanted < 15s"
