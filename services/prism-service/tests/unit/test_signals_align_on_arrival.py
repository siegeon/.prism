"""Signals align on arrival and keep their raw text (task ed034701, epic
cc9a44c8 "Every ingestion path invokes Align language").

SignalStore.create() runs the SAME deterministic STE pipeline
(services/ste.py normalize -> services/lexicon.py align -> services/ste.py
check) TaskService._apply_ste already runs on every task write, over a
signal's subject and body. The result lands in NEW aligned_subject/
aligned_body/style columns; subject/body themselves are NEVER rewritten --
a signal must always be able to show what actually arrived.

Pins: SignalStore.create() aligning and round-tripping the new fields,
POST /api/signals and MCP signal_post returning them, promote() using the
aligned body for a task's description, signal_resolver matching on aligned
text, OntologyGraph projecting the aligned body as rdfs:comment on o:Signal,
text-uses-canonical-terms firing on an o:Signal holding a lexicon synonym,
and QueuePage rendering the aligned text plus a raw-text "As arrived"
fallback (source-scan -- no JS runner, per
test_conductor_page_animated_cleanup_ui.py's convention).
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_WEB = _SERVICE_ROOT / "prism_service" / "web" / "src"

_RAW_BODY = "Please open a ticket for the PR; it's urgent"
_EXPECTED_ALIGNED_BODY = "Please open a Task for the PullRequest. It is urgent"


@pytest.fixture
def project():
    """A throwaway project name under the suite-pinned PRISM_DATA_DIR
    (tests/conftest.py) -- unique per test so parallel runs never
    collide."""
    return f"align-signal-{uuid.uuid4().hex[:8]}"


# ── SignalStore.create() aligns ─────────────────────────────────────────

def test_create_aligns_body_and_keeps_the_raw_body_untouched(project):
    from prism_service.services import ste
    from prism_service.models.signal import Signal
    from prism_service.services.signal_store import SignalStore

    expected, _findings = ste.apply(_RAW_BODY, mode="flavored")
    assert expected == _EXPECTED_ALIGNED_BODY, (
        "the fixture's own expectation drifted from ste.apply's real output")

    store = SignalStore(project)
    signal = Signal(project=project, channel="slack", subject="ping",
                     body=_RAW_BODY, sender="alice")
    store.create(signal)

    assert signal.body == _RAW_BODY, "the raw body must never be rewritten"
    assert signal.aligned_body == _EXPECTED_ALIGNED_BODY
    assert "Task" in signal.aligned_body
    assert "PullRequest" in signal.aligned_body
    assert "It is urgent" in signal.aligned_body


def test_create_aligns_subject_too(project):
    from prism_service.models.signal import Signal
    from prism_service.services.signal_store import SignalStore

    store = SignalStore(project)
    signal = Signal(project=project, channel="slack",
                     subject="please open a ticket", body="", sender="alice")
    store.create(signal)

    assert signal.subject == "please open a ticket"
    # normalize() never capitalizes a sentence's first letter on its
    # own (only after a semicolon-break it just introduced); it only
    # fixes contractions/fillers/marketing words, so the raw casing
    # here is untouched and only "ticket" -> "Task" changes.
    assert signal.aligned_subject == "please open a Task"


def test_style_names_the_rules_that_fired(project):
    from prism_service.models.signal import Signal
    from prism_service.services.signal_store import SignalStore

    store = SignalStore(project)
    signal = Signal(project=project, channel="slack", subject="ping",
                     body=_RAW_BODY, sender="alice")
    store.create(signal)

    body_rules = signal.style["fixed"]["body"]
    assert "contraction" in body_rules
    assert "semicolon" in body_rules
    assert "lexicon" in body_rules
    aligned_from = {a["from"].lower() for a in signal.style["aligned"]
                    if a["field"] == "body"}
    assert "ticket" in aligned_from
    assert "pr" in aligned_from


def test_a_normaliser_failure_never_drops_a_signal(project, monkeypatch):
    """A bug in the normaliser must store an empty aligned pair, never
    fail the write (task ed034701's own stop_if)."""
    from prism_service.models.signal import Signal
    from prism_service.services import ste
    from prism_service.services.signal_store import SignalStore

    def _boom(*_a, **_kw):
        raise RuntimeError("normaliser exploded")

    monkeypatch.setattr(ste, "normalize", _boom)

    store = SignalStore(project)
    signal = Signal(project=project, channel="slack", subject="x", body="y")
    store.create(signal)

    assert signal.aligned_subject == ""
    assert signal.aligned_body == ""
    assert signal.style == {}
    got = store.get(signal.id)
    assert got is not None
    assert got.subject == "x" and got.body == "y"


def test_update_re_aligns_when_subject_or_body_change(project):
    """Task aa7fab99: SignalStore.update() must re-run the SAME
    alignment create() runs whenever subject or body actually change --
    a refreshed ontology signal (rule_decisions' dedup re-post) must
    keep showing CURRENT aligned text, never what the first post
    produced. Compares against the LITERAL expected text (never
    normalize(input) compared with itself)."""
    from prism_service.models.signal import Signal
    from prism_service.services.signal_store import SignalStore

    store = SignalStore(project)
    signal = Signal(project=project, channel="slack", subject="ping",
                     body="nothing to see here", sender="alice")
    store.create(signal)
    assert signal.aligned_body == "nothing to see here"

    updated = store.update(signal.id, body=_RAW_BODY)
    assert updated is not None
    assert updated.body == _RAW_BODY
    assert updated.aligned_body == _EXPECTED_ALIGNED_BODY
    assert "lexicon" in updated.style["fixed"]["body"]

    got = store.get(signal.id)
    assert got.aligned_body == _EXPECTED_ALIGNED_BODY

    # An update that touches neither subject nor body must not disturb
    # the aligned fields it already has.
    untouched = store.update(signal.id, state="dropped")
    assert untouched.aligned_body == _EXPECTED_ALIGNED_BODY


def test_get_and_list_carry_aligned_fields(project):
    from prism_service.models.signal import Signal
    from prism_service.services.signal_store import SignalStore

    store = SignalStore(project)
    signal = Signal(project=project, channel="slack", subject="ping",
                     body=_RAW_BODY, sender="alice")
    store.create(signal)

    got = store.get(signal.id)
    assert got.aligned_body == _EXPECTED_ALIGNED_BODY
    assert got.style["fixed"]

    listed = store.list()
    assert listed[0].aligned_body == _EXPECTED_ALIGNED_BODY


# ── API + MCP surfaces return the aligned fields ────────────────────────

def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import signals as signals_api
    app = FastAPI()
    app.include_router(signals_api.router, prefix="/api/signals")
    return TestClient(app)


def test_post_signals_api_returns_aligned_fields_and_style(project):
    client = _client()
    r = client.post(
        "/api/signals", params={"project": project},
        json={"channel": "slack", "subject": "ping", "body": _RAW_BODY,
              "sender": "alice"},
    )
    assert r.status_code == 200, r.text
    body = r.json()["signal"]
    assert body["body"] == _RAW_BODY
    assert body["aligned_body"] == _EXPECTED_ALIGNED_BODY
    assert body["style"]["fixed"]["body"]


def _call(tool, args, project_id):
    from prism_service.mcp.tools import handle_tool
    return asyncio.run(handle_tool(tool, args, project_id=project_id))[0].text


def test_mcp_signal_post_returns_aligned_fields(project):
    result = json.loads(_call("signal_post", {"subject": "ping", "body": _RAW_BODY},
                               project))
    assert result["body"] == _RAW_BODY
    assert result["aligned_body"] == _EXPECTED_ALIGNED_BODY
    assert result["style"]["fixed"]["body"]


# ── promote() uses the aligned body ──────────────────────────────────────

def test_promote_description_defaults_to_the_aligned_body(project):
    client = _client()
    posted = client.post(
        "/api/signals", params={"project": project},
        json={"channel": "slack", "subject": "ping", "body": _RAW_BODY},
    ).json()["signal"]

    r = client.post(f"/api/signals/{posted['id']}/promote",
                     params={"project": project}, json={"title": "go fix this"})
    assert r.status_code == 200, r.text
    task = r.json()["task"]
    context = f"From slack: {posted['channel_ref']}"
    assert task["description"] == f"{_EXPECTED_ALIGNED_BODY}\n\n{context}"


# ── resolver matches through aligned text ────────────────────────────────

def test_resolver_matches_a_task_through_aligned_text_not_raw(project):
    from prism_service.models.signal import Signal
    from prism_service.services.signal_resolver import resolve
    from prism_service.services.signal_store import SignalStore
    from prism_service.project_context import get_project

    task = get_project(project).task_svc.create(
        title="Investigate the Task and PullRequest")

    store = SignalStore(project)
    aligned_signal = store.create(Signal(
        project=project, channel="slack",
        subject="Please look at this ticket for the PR", body=""))
    matches = resolve(project, aligned_signal)
    related_ids = {m["id"] for m in matches["related_tasks"]}
    assert task.id in related_ids, (
        "the aligned subject ('...Task...PullRequest...') should overlap "
        f"the task title: {matches['related_tasks']}")

    # Proves it is the ALIGNMENT doing the work: the identical raw text,
    # unaligned (aligned_subject/aligned_body left empty, as a signal
    # built without going through SignalStore.create() always is), finds
    # no such overlap ("ticket"/"pr" never equal "task"/"pullrequest").
    raw_signal = Signal(project=project, channel="slack",
                         subject="Please look at this ticket for the PR")
    raw_matches = resolve(project, raw_signal)
    raw_related_ids = {m["id"] for m in raw_matches["related_tasks"]}
    assert task.id not in raw_related_ids


# ── ontology graph projects the aligned body ─────────────────────────────

def test_graph_projects_aligned_body_as_rdfs_comment(project):
    from prism_service.models.signal import Signal
    from prism_service.services.ontology_graph import OntologyGraph
    from prism_service.services.signal_store import SignalStore

    store = SignalStore(project)
    signal = store.create(Signal(project=project, channel="slack",
                                  subject="ping", body=_RAW_BODY))

    graph = OntologyGraph(project)
    graph.rebuild()

    q = (
        "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#> "
        "PREFIX o: <urn:prism:onto:> "
        "SELECT ?c WHERE { GRAPH ?g { ?s a o:Signal ; rdfs:comment ?c } }"
    )
    rows = list(graph._store.query(q, use_default_graph_as_union=True))
    comments = {sol["c"].value for sol in rows}
    assert _EXPECTED_ALIGNED_BODY in comments, comments


# ── SHACL: text-uses-canonical-terms fires on an o:Signal holding a
#    lexicon synonym (task ed034701 adds o:Signal to its target shapes,
#    alongside o:Task/o:Decision) ──────────────────────────────────────

_MODEL_TTL = _SERVICE_ROOT / "prism_service" / "ontology" / "model.ttl"
_MODEL_LEXICON_TTL = _SERVICE_ROOT / "prism_service" / "ontology" / "model-lexicon.ttl"

_PREFIXES = """
@prefix o: <urn:prism:onto:> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
"""


def _shape_graph(snippet: str):
    import rdflib
    g = rdflib.Graph()
    g.parse(str(_MODEL_TTL), format="turtle")
    g.parse(str(_MODEL_LEXICON_TTL), format="turtle")
    g.parse(data=snippet, format="turtle", publicID="urn:prism:onto:")
    return g


def test_text_uses_canonical_terms_fires_on_a_signal_holding_ticket():
    from prism_service.services import ontology_rules

    compliant = (
        _PREFIXES + 'o:sig1 a o:Signal ; rdfs:comment "Open a Task for review." .')
    violating = (
        _PREFIXES + 'o:sig1 a o:Signal ; rdfs:comment "Open a ticket for review." .')

    _inferred, quiet = ontology_rules.run_shapes(_shape_graph(compliant))
    assert "text-uses-canonical-terms" not in quiet, quiet.get(
        "text-uses-canonical-terms")

    _inferred, bad = ontology_rules.run_shapes(_shape_graph(violating))
    assert "text-uses-canonical-terms" in bad
    assert any(f.endswith("sig1") for f in bad["text-uses-canonical-terms"])


# ── QueuePage renders the aligned text and an "As arrived" fallback ─────

def _queue_page() -> str:
    return (_WEB / "pages" / "QueuePage.tsx").read_text(encoding="utf-8")


def test_queue_page_renders_aligned_subject_and_body():
    src = _queue_page()
    assert "aligned_subject" in src
    assert "aligned_body" in src
    assert "signal.aligned_subject || signal.subject" in src
    assert "signal.aligned_body || signal.body" in src


def test_queue_page_has_an_as_arrived_details_element_with_the_raw_text():
    src = _queue_page()
    assert "<details" in src
    assert "As arrived" in src
    # The raw signal.subject/signal.body must actually be inside the
    # <details> element -- pin the enclosing block, not just the label,
    # so a decoy comment naming "As arrived" elsewhere can't satisfy this.
    import re
    m = re.search(r"<details[^>]*>.*?</details>", src, re.DOTALL)
    assert m, "no <details>...</details> block found"
    block = m.group(0)
    assert "As arrived" in block
    assert "signal.subject" in block
    assert "signal.body" in block
