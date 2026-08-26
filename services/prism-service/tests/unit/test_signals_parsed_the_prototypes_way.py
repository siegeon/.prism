"""Signals are parsed the prototype's way into the ontology (task
31b737fb, epic 3a652b3b, owner: "make sure you are not overlooking
anything in the world of the python regex process when bringing in items
off the queue and adding them to the rules and knowledge in the
ontology").

Pins prism_service/services/signal_parse.py's parse()/gate_enrichment(),
the resolver's use of it, the ontology_graph projection of the joins it
finds, vocab.json's round trip, and the three new SHACL shapes
(jira-issue-known-project, ask-names-a-person, signal-joins-on-address —
also exercised in test_rules_are_shacl_shapes.py's own RULE_FIXTURES).
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import rdflib

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_MODEL_TTL = _SERVICE_ROOT / "prism_service" / "ontology" / "model.ttl"
_SHAPES_TTL = _SERVICE_ROOT / "prism_service" / "ontology" / "shapes.ttl"

SAMPLE_ARRIVED_AT = "2026-08-25T12:00:00+00:00"
SAMPLE_BODY = (
    "Please review PLAT-42 before Friday.\n"
    "Meeting is at 10:00 AM-10:30, ignore that.\n"
    "Can you also check PR #17? Ping jane.doe@example.com with questions.\n"
    "Thread: https://acme.slack.com/archives/C0123456/p1234567890123\n"
)


@pytest.fixture(autouse=True)
def _known_jira_projects(monkeypatch):
    monkeypatch.setenv("PRISM_KNOWN_JIRA_PROJECTS", "PLAT")


def _next_weekday_on_or_after(base: datetime, weekday: int) -> str:
    days_ahead = (weekday - base.weekday() + 7) % 7
    if days_ahead == 0:
        days_ahead = 7
    return (base + timedelta(days=days_ahead)).date().isoformat()


def _sample_signal(**overrides):
    from prism_service.models.signal import Signal

    defaults = dict(
        project="proto-parse", channel="slack", subject="Needs your review",
        body=SAMPLE_BODY, sender="jane.doe@example.com",
        arrived_at=SAMPLE_ARRIVED_AT,
    )
    defaults.update(overrides)
    return Signal(**defaults)


# ── parse() ──────────────────────────────────────────────────────────────

def test_parse_extracts_the_prototypes_way_on_the_oracle_sample():
    from prism_service.services.signal_parse import parse

    extraction = parse(_sample_signal())

    keys = {t["key"] for t in extraction.tickets}
    assert "PLAT-42" in keys
    assert "AM-10" not in keys
    plat = next(t for t in extraction.tickets if t["key"] == "PLAT-42")
    assert plat["project"] == "PLAT"
    assert plat["known"] is True

    prs = [c for c in extraction.code_refs if c["kind"] == "pr"]
    assert any(c["number"] == 17 for c in prs)

    assert "jane.doe@example.com" in extraction.addresses
    assert any("slack.com/archives" in p for p in extraction.permalinks)

    base = datetime.fromisoformat(SAMPLE_ARRIVED_AT).replace(tzinfo=None)
    expected = _next_weekday_on_or_after(base, 4)  # Friday
    assert expected in extraction.deadlines


def test_parse_rejects_jira_looking_key_with_unknown_project():
    from prism_service.models.signal import Signal
    from prism_service.services.signal_parse import parse

    signal = Signal(project="p", channel="ui", subject="",
                     body="10:00 AM-10:30 works for me",
                     arrived_at=SAMPLE_ARRIVED_AT)
    extraction = parse(signal)
    assert extraction.tickets == []


def test_parse_unknown_keys_land_in_drift():
    from prism_service.services.signal_parse import parse

    raw = {"subject": "hi", "body": "there", "arrived_at": SAMPLE_ARRIVED_AT,
           "a_field_nobody_declared": "surprise"}
    extraction = parse(raw)
    assert "a_field_nobody_declared" in extraction.drift


def test_parse_eod_tomorrow_resolves_one_day_after_arrived_at():
    from prism_service.models.signal import Signal
    from prism_service.services.signal_parse import parse

    signal = Signal(project="p", channel="ui",
                     subject="Need this EOD tomorrow please", body="",
                     arrived_at=SAMPLE_ARRIVED_AT)
    extraction = parse(signal)
    base = datetime.fromisoformat(SAMPLE_ARRIVED_AT).replace(tzinfo=None)
    expected = (base + timedelta(days=1)).date().isoformat()
    assert expected in extraction.deadlines


def test_parse_iso_date_verbatim():
    from prism_service.models.signal import Signal
    from prism_service.services.signal_parse import parse

    signal = Signal(project="p", channel="ui", subject="Ship by 2026-09-01",
                     body="", arrived_at=SAMPLE_ARRIVED_AT)
    extraction = parse(signal)
    assert "2026-09-01" in extraction.deadlines


# ── gate_enrichment ──────────────────────────────────────────────────────

def test_gate_enrichment_holds_back_unknown_bucket_with_a_reason():
    from prism_service.services.signal_parse import HeldBack, gate_enrichment

    result = gate_enrichment(
        {"ask_kind": "decision", "bucket": "urgent", "channel": "slack"})
    assert isinstance(result, HeldBack)
    held = {h["field"]: h for h in result.held}
    assert "bucket" in held
    assert held["bucket"]["value"] == "urgent"
    assert held["bucket"]["reason"]


def test_gate_enrichment_accepts_a_known_bucket():
    from prism_service.services.signal_parse import Enrichment, gate_enrichment

    result = gate_enrichment(
        {"ask_kind": "decision", "bucket": "needs_attention", "channel": "slack"})
    assert isinstance(result, Enrichment)
    assert result.bucket == "needs_attention"


# ── vocab.json ───────────────────────────────────────────────────────────

def test_vocab_json_round_trips_the_enums():
    from prism_service.ontology.vocab import build_vocab

    vocab = build_vocab()
    for key in ("channel", "ask", "bucket", "signal_state", "task_status",
                "workflow", "proof_type", "gate_state"):
        assert vocab[key], key
    assert set(vocab["bucket"]) == {"needs_attention", "team_updates", "low_priority"}


def test_vocab_check_passes_after_write():
    from prism_service.ontology import vocab as vocab_mod

    assert vocab_mod.main(["--write"]) == 0
    assert vocab_mod.main(["--check"]) == 0


def test_drift_no_reimplemented_channel_tuple_in_web_or_ttl():
    from prism_service.models.task import CHANNELS

    web_src = _SERVICE_ROOT / "prism_service" / "web" / "src"
    needle = ", ".join(f'"{c}"' for c in CHANNELS)

    hits = []
    if web_src.is_dir():
        for path in list(web_src.rglob("*.ts")) + list(web_src.rglob("*.tsx")):
            text = path.read_text(encoding="utf-8", errors="ignore")
            if needle in text:
                hits.append(path)
    assert not hits, hits

    for ttl in (_MODEL_TTL, _SHAPES_TTL):
        assert needle not in ttl.read_text(encoding="utf-8")


# ── resolver wiring ──────────────────────────────────────────────────────

def test_resolver_matches_task_by_ticket_key():
    from prism_service.project_context import get_project
    from prism_service.services.signal_resolver import resolve

    project = f"parse-proto-{uuid.uuid4().hex[:8]}"
    task = get_project(project).task_svc.create(
        title="unrelated title", channel="jira", channel_ref="PLAT-42")
    signal = _sample_signal(project=project, subject="ping",
                             body="Re PLAT-42 status")

    matches = resolve(project, signal)
    ids = {t["id"]: t for t in matches["related_tasks"]}
    assert task.id in ids


def test_resolver_persists_extraction_on_matches():
    from prism_service.services.signal_resolver import resolve

    project = f"parse-proto-{uuid.uuid4().hex[:8]}"
    signal = _sample_signal(project=project)
    matches = resolve(project, signal)
    assert matches["extraction"]["tickets"]
    assert any(t["key"] == "PLAT-42" for t in matches["extraction"]["tickets"])


def test_resolver_ask_kind_deliverable_when_deadline_found():
    from prism_service.models.signal import Signal
    from prism_service.services.signal_resolver import resolve

    project = f"parse-proto-{uuid.uuid4().hex[:8]}"
    signal = Signal(project=project, channel="ui",
                     subject="Checking in on the widget",
                     body="Need this before Friday, thanks",
                     arrived_at=SAMPLE_ARRIVED_AT)
    matches = resolve(project, signal)
    assert matches["ask"]["kind"] == "deliverable"


# ── ontology graph projection ───────────────────────────────────────────

def test_graph_carries_aboutticket_askedby_raises_for_a_seeded_signal():
    from prism_service.services.ontology_graph import OntologyGraph
    from prism_service.services.signal_resolver import resolve
    from prism_service.services.signal_store import SignalStore

    project = f"parse-proto-{uuid.uuid4().hex[:8]}"
    store = SignalStore(project)
    signal = store.create(_sample_signal(project=project))
    matches = resolve(project, signal)
    store.update(signal.id, matches=matches)

    graph = OntologyGraph(project)
    graph.rebuild()

    q_ticket = (
        "PREFIX o: <urn:prism:onto:> "
        "SELECT ?issue WHERE { GRAPH ?g { ?s a o:Signal ; o:aboutTicket ?issue . "
        "?issue a o:JiraIssue } }"
    )
    rows = list(graph._store.query(q_ticket, use_default_graph_as_union=True))
    assert rows, "expected an o:aboutTicket triple to a JiraIssue"

    q_person = (
        "PREFIX o: <urn:prism:onto:> "
        "SELECT ?p WHERE { GRAPH ?g { ?ask a o:Ask ; o:askedBy ?p . "
        "?p o:email ?e } }"
    )
    rows2 = list(graph._store.query(q_person, use_default_graph_as_union=True))
    assert rows2, "expected askedBy -> a Person with an o:email"

    q_raises = (
        "PREFIX o: <urn:prism:onto:> "
        "SELECT ?ask WHERE { GRAPH ?g { ?s a o:Signal ; o:raises ?ask } }"
    )
    rows3 = list(graph._store.query(q_raises, use_default_graph_as_union=True))
    assert rows3, "expected a o:raises triple from the Signal"


# ── the three new SHACL rules (compliant + violating fixture each) ──────

_PREFIXES = """
@prefix o: <urn:prism:onto:> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
"""

NEW_RULE_FIXTURES = {
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
}


def _graph(snippet: str) -> rdflib.Graph:
    g = rdflib.Graph()
    g.parse(str(_MODEL_TTL), format="turtle")
    g.parse(data=snippet, format="turtle", publicID="urn:prism:onto:")
    return g


@pytest.mark.parametrize("rule_name", sorted(NEW_RULE_FIXTURES))
def test_new_rule_is_quiet_on_compliant_and_fires_on_violation(rule_name):
    from prism_service.services import ontology_rules

    compliant, violating, focus_local = NEW_RULE_FIXTURES[rule_name]

    _inferred, quiet = ontology_rules.run_shapes(_graph(compliant))
    assert rule_name not in quiet, quiet.get(rule_name)

    _inferred2, bad = ontology_rules.run_shapes(_graph(violating))
    assert rule_name in bad, (
        f"{rule_name} did not fire on its OWN violating fixture")
    assert any(f.endswith(focus_local) for f in bad[rule_name])
