"""Rules tab shows each exemption and lets the user undo it
(task c3762cb5). Red step: every test here fails until
rule_decisions.decorated_report emits an "exempt" list,
POST /api/okf/ontology/rules/{rule}/unexempt removes one IRI from
decisions.json, and RuleRow renders a chip with an Undo control.

Isolation: the session conftest pins PRISM_DATA_DIR to a throwaway dir,
so _decisions_path() never touches the live decisions.json. The persisted
SHACL report and the ontology graph are stubbed - no SHACL run.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

RULE = "no-artifacts-in-the-root"
IRI_A = "https://prism.local/doc/CLAUDE.md"
IRI_B = "https://prism.local/doc/README.md"
TSX = (Path(__file__).resolve().parents[2]
       / "prism_service" / "web" / "src" / "pages" / "OntologyPage.tsx")


class _StubGraph:
    def __init__(self, project: str) -> None:
        self.project = project

    def label_of(self, iri: str) -> str:
        return iri.rsplit("/", 1)[-1]


@pytest.fixture
def seeded(monkeypatch):
    from prism_service.services import ontology_graph, ontology_rules
    from prism_service.services import rule_decisions

    pid = f"exempt-{uuid.uuid4().hex[:8]}"
    rows = [{
        "name": RULE, "title": "No artifacts in the root",
        "description": "d", "message": "m", "looked_at": 941,
        "focus": [IRI_A, IRI_B], "validated_at": "2026-08-27T00:00:00Z",
    }]
    monkeypatch.setattr(ontology_rules, "_read_report", lambda p: rows)
    monkeypatch.setattr(ontology_rules, "rule_catalog", lambda p: [])
    monkeypatch.setattr(ontology_graph, "OntologyGraph", _StubGraph)
    return pid, rule_decisions


def _client() -> TestClient:
    from prism_service.api.okf import router

    app = FastAPI()
    app.include_router(router, prefix="/api/okf")
    return TestClient(app)


def _rule(rd, pid: str) -> dict:
    return rd.decorated_report(pid)["rules"][0]


def _disk(rd, pid: str) -> dict:
    return json.loads(rd._decisions_path(pid).read_text(encoding="utf-8"))


def test_api_exempt_field(seeded):
    pid, rd = seeded
    rd._record_exempt(pid, RULE, [IRI_A, IRI_B])
    rule = _rule(rd, pid)
    assert rule["exempt"] == [{"iri": IRI_A, "label": "CLAUDE.md"},
                              {"iri": IRI_B, "label": "README.md"}]
    assert rule["focus"] == []
    assert rule["violations"] == 0


def test_api_exempt_empty(seeded):
    pid, rd = seeded
    rule = _rule(rd, pid)
    assert rule["exempt"] == []
    assert rule["violations"] == 2 and rule["looked_at"] == 941
    assert set(rule) - {"decision"} == {
        "name", "title", "description", "message", "looked_at",
        "violations", "focus", "exempt", "validated_at", "derived_from"}


def test_unexempt_route_writes_decisions(seeded):
    pid, rd = seeded
    rd._record_exempt(pid, RULE, [IRI_A, IRI_B])
    resp = _client().post(f"/api/okf/ontology/rules/{RULE}/unexempt",
                          json={"project": pid, "iri": IRI_A})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"rule": RULE, "exempt": [IRI_B]}
    assert _disk(rd, pid)[RULE]["exempt"] == [IRI_B]


def test_unexempt_then_rule_reports_again(seeded):
    pid, rd = seeded
    rd._record_exempt(pid, RULE, [IRI_A, IRI_B])
    assert _rule(rd, pid)["violations"] == 0
    resp = _client().post(f"/api/okf/ontology/rules/{RULE}/unexempt",
                          json={"project": pid, "iri": IRI_A})
    assert resp.status_code == 200, resp.text
    rule = _rule(rd, pid)
    assert rule["violations"] == 1
    assert rule["focus"][0]["iri"] == IRI_A
    assert rule["exempt"] == [{"iri": IRI_B, "label": "README.md"}]


def test_unexempt_last_and_404(seeded):
    pid, rd = seeded
    rd._record_exempt(pid, RULE, [IRI_A])
    client = _client()
    resp = client.post(f"/api/okf/ontology/rules/{RULE}/unexempt",
                       json={"project": pid, "iri": IRI_A})
    assert resp.status_code == 200, resp.text
    assert "exempt" not in _disk(rd, pid)[RULE]
    before = hashlib.sha256(rd._decisions_path(pid).read_bytes()).hexdigest()
    resp = client.post(f"/api/okf/ontology/rules/{RULE}/unexempt",
                       json={"project": pid, "iri": IRI_A})
    assert resp.status_code == 404
    resp = client.post("/api/okf/ontology/rules/no-such-rule/unexempt",
                       json={"project": pid, "iri": IRI_A})
    assert resp.status_code == 404
    after = hashlib.sha256(rd._decisions_path(pid).read_bytes()).hexdigest()
    assert before == after


def _rule_row_body() -> str:
    src = TSX.read_text(encoding="utf-8")
    start = src.index("function RuleRow(")
    end = src.index("\nfunction ", start + 1)
    return src[start:end]


def _strip_comments(body: str) -> str:
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", body)


def test_rules_tab_renders_exempt_chip():
    src = _strip_comments(TSX.read_text(encoding="utf-8"))
    rule_type = src[src.index("type Rule = {"):]
    rule_type = rule_type[:rule_type.index("};")]
    assert "exempt:" in rule_type
    body = _strip_comments(_rule_row_body())
    assert "rule.exempt.map(" in body
    assert "EXEMPT" in body
    assert re.search(r"<[Bb]utton[^>]*>[^<]*Undo", body), "no Undo button"
    post = src.index("/unexempt")
    assert "/api/okf/ontology/rules" in src[post:post + 800], "no refetch"


def test_rules_tab_count_text_shows_exempt():
    body = _strip_comments(_rule_row_body())
    count = body[body.index("CHECKED"):]
    assert "rule.exempt.length > 0" in count
    assert "EXEMPT" in count
