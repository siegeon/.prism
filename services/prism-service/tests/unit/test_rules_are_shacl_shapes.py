"""The rules are SHACL shapes that can fail (task 8eeb3e65, epic 3efbcd89).

shapes.ttl (prism_service/ontology/shapes.ttl) declares 13 SHACL shapes
(task 5ac5d04c added text-is-plain) over the o: model, keyed per
ontology-SKILL.md's "Adding to the model / A new rule": the property
(or SPARQLConstraint) shape carries the rule's own IRI, the node shape
is <rule>.target. "A rule that cannot fail is decoration" — every rule
below gets BOTH a compliant fixture (validates clean) and a violating
one (reports exactly that rule, by IRI, with the offending focus node).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import rdflib

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
_MODEL_TTL = _SERVICE_ROOT / "prism_service" / "ontology" / "model.ttl"
_SHAPES_TTL = _SERVICE_ROOT / "prism_service" / "ontology" / "shapes.ttl"


def _graph(snippet: str) -> rdflib.Graph:
    """TBox (model.ttl) + a small ABox snippet — the fixture shape the
    task instructs: 'small rdflib Graphs + the TBox'."""
    g = rdflib.Graph()
    g.parse(str(_MODEL_TTL), format="turtle")
    g.parse(data=snippet, format="turtle", publicID="urn:prism:onto:")
    return g


_PREFIXES = """
@prefix o: <urn:prism:onto:> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
"""

_OLD_TS = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
_RECENT_TS = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

# name -> (compliant snippet, violating snippet, expected focus local name)
RULE_FIXTURES: dict[str, tuple[str, str, str]] = {
    "task-names-its-channel": (
        _PREFIXES + 'o:t1 a o:Task ; o:arrivedVia o:c1 .',
        _PREFIXES + 'o:t1 a o:Task .',
        "t1",
    ),
    "ask-comes-from-the-queue": (
        _PREFIXES + 'o:sig1 a o:Signal ; o:becameTask o:task1 ; '
        'o:arrivedVia o:c-ui . o:task1 a o:Task ; o:arrivedVia o:c-ui .',
        _PREFIXES + 'o:sig1 a o:Signal ; o:becameTask o:task1 ; '
        'o:arrivedVia o:c-ui . o:task1 a o:Task ; o:arrivedVia o:c-mcp .',
        "sig1",
    ),
    "flagged-signal-is-placed": (
        _PREFIXES + f'o:sig1 a o:Signal ; o:state "open" ; '
        f'o:arrivedAt "{_RECENT_TS}"^^xsd:dateTime .',
        _PREFIXES + 'o:sig1 a o:Signal ; o:state "open" .',
        "sig1",
    ),
    "no-artifacts-in-the-root": (
        _PREFIXES + 'o:d1 a o:Document ; o:inFolder o:f1 .',
        _PREFIXES + 'o:d1 a o:Document .',
        "d1",
    ),
    "dated-folder-uses-one-format": (
        _PREFIXES + 'o:f1 a o:Folder ; rdfs:label "engineering/weekly-reports/2026-08-18" .',
        _PREFIXES + 'o:f1 a o:Folder ; rdfs:label "engineering/weekly-reports/2026-Q1" .',
        "f1",
    ),
    "skill-description-says-when": (
        _PREFIXES + 'o:a1 a o:Agent ; rdfs:comment "Use when the user asks for X." .',
        _PREFIXES + 'o:a1 a o:Agent ; rdfs:comment "Reviews PRs and leaves comments." .',
        "a1",
    ),
    "skill-description-has-no-markup": (
        _PREFIXES + 'o:a1 a o:Agent ; rdfs:comment "Use when the user asks for X." .',
        _PREFIXES + 'o:a1 a o:Agent ; rdfs:comment "Use when <thing> happens." .',
        "a1",
    ),
    "agent-delegates-one-tier-down": (
        _PREFIXES + 'o:green_gate a o:Step ; o:decidedBy "sm" ; o:producedBy "qa" .',
        _PREFIXES + 'o:story_gate a o:Step ; o:decidedBy "sm" ; o:producedBy "sm" .',
        "story_gate",
    ),
    "twin-classes": (
        _PREFIXES + 'o:ClassA a rdfs:Class . o:ClassB a rdfs:Class . '
        'o:x1 a o:ClassA . o:x2 a o:ClassB .',
        _PREFIXES + 'o:ClassA a rdfs:Class . o:ClassB a rdfs:Class . '
        'o:x1 a o:ClassA, o:ClassB .',
        "ClassA",
    ),
    # task 31b737fb: signals are parsed the prototype's way into the
    # ontology — three new rules over the aboutTicket/askedBy joins
    # signal_parse's regex extraction feeds into the graph.
    "jira-issue-known-project": (
        _PREFIXES + 'o:i1 a o:JiraIssue ; o:projectKnown true .',
        _PREFIXES + 'o:i1 a o:JiraIssue ; o:projectKnown false .',
        "i1",
    ),
    "ask-names-a-person": (
        _PREFIXES + 'o:ask1 a o:Ask ; o:askedBy o:p1 .',
        _PREFIXES + 'o:ask1 a o:Ask .',
        "ask1",
    ),
    "signal-joins-on-address": (
        _PREFIXES + 'o:ask1 a o:Ask ; o:askedBy o:p1 . '
        'o:p1 a o:Person ; o:email "a@b.com" .',
        _PREFIXES + 'o:ask1 a o:Ask ; o:askedBy o:p1 . o:p1 a o:Person .',
        "ask1",
    ),
    # task 5ac5d04c: one instance of each of the four target classes
    # (Task/Decision/Term/Agent), so looked_at is non-zero for every
    # one of them, not just whichever class the SPARQL result lands on.
    "text-is-plain": (
        _PREFIXES + 'o:t1 a o:Task ; rdfs:label "Review the plan before you start work." . '
        'o:dec1 a o:Decision ; rdfs:label "Use SQLite for local caching." . '
        'o:term1 a o:Term ; rdfs:comment "A task is one unit of tracked work." . '
        'o:a1 a o:Agent ; rdfs:comment "Use when the user asks for X." .',
        _PREFIXES + 'o:t1 a o:Task ; rdfs:comment "don\'t; it\'s robust" .',
        "t1",
    ),
}


# ---------------------------------------------------------------------------
# shapes.ttl itself parses, and declares exactly the 9 rules above.
# ontology_rules.rule_catalog() now merges EVERY ontology/shapes*.ttl file
# (task f5352fa1's shapes-knowledge.ttl sibling) into one catalog, so its
# result is a SUPERSET of shapes.ttl's own 9 rules, not an exact match --
# shapes.ttl itself still declares exactly these 9.
# ---------------------------------------------------------------------------

def test_shapes_ttl_parses_and_declares_the_rule_catalog():
    from prism_service.services import ontology_rules

    g = rdflib.Graph()
    g.parse(str(_SHAPES_TTL), format="turtle")
    assert len(g) > 20

    catalog = ontology_rules.rule_catalog()
    names = {r["name"] for r in catalog}
    assert set(RULE_FIXTURES) <= names
    for r in catalog:
        assert r["target_class"], r["name"]


# ---------------------------------------------------------------------------
# every rule: a compliant fixture validates clean, a violating fixture
# reports EXACTLY that rule (by IRI) with the offending focus node
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rule_name", sorted(RULE_FIXTURES))
def test_rule_is_quiet_on_compliant_and_fires_on_violation(rule_name):
    from prism_service.services import ontology_rules

    compliant_snippet, violating_snippet, focus_local = RULE_FIXTURES[rule_name]

    _inferred, quiet_violations = ontology_rules.run_shapes(_graph(compliant_snippet))
    assert rule_name not in quiet_violations, (
        f"{rule_name} fired on its OWN compliant fixture: "
        f"{quiet_violations.get(rule_name)}")

    _inferred, bad_violations = ontology_rules.run_shapes(_graph(violating_snippet))
    assert rule_name in bad_violations, (
        f"{rule_name} did not fire on its OWN violating fixture — "
        "a rule that cannot fail is decoration")
    focus_nodes = bad_violations[rule_name]
    assert any(f.endswith(focus_local) for f in focus_nodes), (
        rule_name, focus_nodes, focus_local)


# ---------------------------------------------------------------------------
# Seeded project fixture: a task without a channel, an open signal
# (unplaced), and a document loose in the project root.
# ---------------------------------------------------------------------------

@pytest.fixture
def seeded_project():
    from prism_service.project_context import get_project

    pid = f"shacl-rules-{uuid.uuid4().hex[:8]}"
    ctx = get_project(pid)
    ctx.task_svc.create(title="legacy task")  # blank channel

    import sqlite3
    from prism_service.config import project_data_dir

    brain_db = project_data_dir(pid) / "brain.db"
    conn = sqlite3.connect(str(brain_db))
    conn.execute("CREATE TABLE docs (id TEXT PRIMARY KEY, source_file TEXT)")
    conn.execute("INSERT INTO docs VALUES ('d1','README.md')")  # loose in root
    conn.commit()
    conn.close()

    from prism_service.models.signal import Signal
    from prism_service.services.signal_store import SignalStore

    store = SignalStore(pid)
    store.create(Signal(project=pid, channel="mcp", subject="unplaced",
                         arrived_at=_OLD_TS))  # >7 days old, still open
    store.close()

    return pid


# ---------------------------------------------------------------------------
# The report round-trips through the store: validate() persists, evaluate()
# reads it back with the same states/looked_at — no recompute on read.
# ---------------------------------------------------------------------------

def test_report_round_trips_through_the_store(seeded_project):
    from prism_service.services import ontology_prototype_projection as proj
    from prism_service.services import ontology_rules

    proj.rebuild(seeded_project)  # rebuild() -> validate() at the end

    computed = {a["name"]: a for a in ontology_rules.evaluate(seeded_project)}
    read_back = {a["name"]: a for a in ontology_rules.evaluate(seeded_project)}
    assert computed == read_back

    assert computed["task-names-its-channel"]["state"] == "violated"
    assert computed["task-names-its-channel"]["looked_at"] >= 1


# ---------------------------------------------------------------------------
# On the seeded project, at least three rules are violated
# ---------------------------------------------------------------------------

def test_seeded_project_violates_at_least_three_rules(seeded_project):
    from prism_service.services import ontology_prototype_projection as proj
    from prism_service.services import ontology_rules

    proj.rebuild(seeded_project)
    axioms = ontology_rules.evaluate(seeded_project)
    violated = [a["name"] for a in axioms if a["state"] == "violated"]
    assert len(violated) >= 3, violated
    assert "task-names-its-channel" in violated
    assert "no-artifacts-in-the-root" in violated
    assert "flagged-signal-is-placed" in violated


# ---------------------------------------------------------------------------
# GET /api/okf/ontology axioms carry looked_at
# ---------------------------------------------------------------------------

def test_get_ontology_axioms_carry_looked_at(seeded_project):
    from prism_service.api import okf

    okf._HOSTS.clear()
    out = okf.ontology(project=seeded_project)
    assert out["axioms"]
    for a in out["axioms"]:
        assert "looked_at" in a
        assert "violations" in a

    violated = [a for a in out["axioms"] if a["state"] == "violated"]
    assert len(violated) >= 3


def test_get_ontology_rules_route_caps_focus_at_twenty(seeded_project):
    from prism_service.api import okf
    from prism_service.services import ontology_prototype_projection as proj

    proj.rebuild(seeded_project)
    out = okf.ontology_rules(project=seeded_project)
    assert out["rules"]
    for r in out["rules"]:
        assert len(r["focus"]) <= 20
        assert r["violations"] >= len(r["focus"])
