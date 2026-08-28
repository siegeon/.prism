"""A task's oracle and plan text link to the ontology too (task 2ec1e395,
epic 47bba8fe). Found live: the Overview's oracle card and the Design/plan
cards render oracle/plan_doc/premise_notes/completion_proof WITHOUT going
through entity_linker -- only description and likely_misfire did (task
6968cc39). This extends GET /api/tasks/{id}/links to return spans PER FIELD
in one response (one linker index build serves all fields, per
entity_linker's own cached-index contract) and routes TaskDetailPage's
oracle/plan/proof cards through the same splice technique the description
already uses.
"""

from __future__ import annotations

import uuid

import pytest

TASK_TITLE = "Slack triage support flow"
DOC_PATH = "services/foo/bar.py"


@pytest.fixture
def project(monkeypatch):
    from prism_service.services.ontology_graph import OntologyGraph

    pid = f"oracle-plan-link-{uuid.uuid4().hex[:8]}"
    rows = {
        "channels": [], "agents": [], "providers": [],
        "tasks": [{"id": "22222222-3333-4444-5555-666666666666",
                   "title": TASK_TITLE, "channel": "ui"}],
        "signals": [], "documents": [DOC_PATH], "code_kinds": [], "memories": [],
    }
    OntologyGraph(pid).rebuild(rows=rows, agent_descriptions={}, signal_arrived_at={})
    return pid


# ---------------------------------------------------------------------------
# GET /api/tasks/{id}/links returns spans PER FIELD, one response
# ---------------------------------------------------------------------------

def test_get_task_links_returns_spans_per_field(project):
    from prism_service.api import tasks as tasks_api
    from prism_service.project_context import get_project

    ctx = get_project(project)
    t = ctx.task_svc.create(title="carrier", channel="ui")
    ctx.task_svc.update(
        t.id,
        description=f"Related to {TASK_TITLE}.",
        oracle=f"See {DOC_PATH} for context. Then read {TASK_TITLE}.",
        likely_misfire=f"Might miss {TASK_TITLE}.",
        plan_doc=f"Plan touches {DOC_PATH}.",
        premise_notes=f"Assumes {TASK_TITLE} is current.",
        completion_proof=f"Verified against {DOC_PATH}.",
    )

    out = tasks_api.get_task_links(t.id, project=project)

    # blocked_reason and gate_reason joined _LINK_FIELDS at task 938b0a2d:
    # machine-written task text links to the ontology the same way
    # human-written text does.
    assert set(out["fields"]) == {
        "description", "oracle", "likely_misfire", "plan_doc",
        "premise_notes", "completion_proof", "blocked_reason", "gate_reason",
    }
    # each field's own text resolves its own entities
    assert any(s["text"] == TASK_TITLE for s in out["fields"]["description"])
    oracle_texts = {s["text"] for s in out["fields"]["oracle"]}
    assert DOC_PATH in oracle_texts and TASK_TITLE in oracle_texts
    assert any(s["text"] == TASK_TITLE for s in out["fields"]["likely_misfire"])
    assert any(s["text"] == DOC_PATH for s in out["fields"]["plan_doc"])
    assert any(s["text"] == TASK_TITLE for s in out["fields"]["premise_notes"])
    assert any(s["text"] == DOC_PATH for s in out["fields"]["completion_proof"])

    # back-compat: top-level `spans` is still exactly the description spans
    assert out["spans"] == out["fields"]["description"]


def test_get_task_links_fields_are_empty_lists_for_blank_fields(project):
    from prism_service.api import tasks as tasks_api
    from prism_service.project_context import get_project

    ctx = get_project(project)
    t = ctx.task_svc.create(title="carrier2", channel="ui")

    out = tasks_api.get_task_links(t.id, project=project)
    for name in ("description", "oracle", "likely_misfire", "plan_doc",
                 "premise_notes", "completion_proof"):
        assert out["fields"][name] == []
    assert out["spans"] == []


# ---------------------------------------------------------------------------
# one linker index build serves all fields -- no extra SPARQL work per field
# ---------------------------------------------------------------------------

def test_one_index_build_serves_every_field(project, monkeypatch):
    from prism_service.api import tasks as tasks_api
    from prism_service.project_context import get_project
    from prism_service.services.ontology_graph import OntologyGraph

    ctx = get_project(project)
    t = ctx.task_svc.create(title="carrier3", channel="ui")
    ctx.task_svc.update(
        t.id,
        description=TASK_TITLE, oracle=TASK_TITLE, likely_misfire=TASK_TITLE,
        plan_doc=TASK_TITLE, premise_notes=TASK_TITLE, completion_proof=TASK_TITLE,
    )
    tasks_api.get_task_links(t.id, project=project)  # warm the cache

    real_query = OntologyGraph.query
    calls = []

    def _tripwire(self, *a, **k):
        calls.append(1)
        return real_query(self, *a, **k)

    monkeypatch.setattr(OntologyGraph, "query", _tripwire)
    tasks_api.get_task_links(t.id, project=project)
    assert calls == [], "a cached index must not re-query SPARQL for any field"


# ---------------------------------------------------------------------------
# frontend: api.ts exposes the combined fetch; TaskDetailPage routes the
# oracle card, plan_doc and completion_proof through the linked splice, and
# fetches links ONCE (never per field) via linkTaskFields
# ---------------------------------------------------------------------------

def _web_src():
    from pathlib import Path

    return (Path(__file__).resolve().parent.parent.parent
            / "prism_service" / "web" / "src")


def test_api_ts_exposes_link_task_fields():
    src = (_web_src() / "lib" / "api.ts").read_text(encoding="utf-8")
    assert "export async function linkTaskFields(" in src
    assert "/links?project=" in src
    assert "fields" in src


def test_task_detail_page_fetches_link_fields_once_per_task_load():
    src = (_web_src() / "pages" / "TaskDetailPage.tsx").read_text(encoding="utf-8")
    assert "linkTaskFields(" in src
    # exactly one call site -- never one per field
    assert src.count("linkTaskFields(project") == 1


@pytest.mark.parametrize("token", [
    "linkedOracle", "linkedPlanDoc", "linkedCompletionProof",
])
def test_task_detail_page_computes_linked_variants(token):
    src = (_web_src() / "pages" / "TaskDetailPage.tsx").read_text(encoding="utf-8")
    assert token in src


def test_task_detail_page_routes_oracle_card_through_render_inline():
    src = (_web_src() / "pages" / "TaskDetailPage.tsx").read_text(encoding="utf-8")
    assert "renderInline" in src
    assert 'import Markdown, { renderInline } from "@/components/Markdown"' in src


def test_task_detail_page_splices_plan_doc_into_planview():
    src = (_web_src() / "pages" / "TaskDetailPage.tsx").read_text(encoding="utf-8")
    assert "doc={linkedPlanDoc || task.plan_doc}" in src
