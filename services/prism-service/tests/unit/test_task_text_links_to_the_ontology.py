"""Task and signal text link to the ontology (task 6968cc39, epic 47bba8fe,
owner relayed: "make sure we have cross clicking on every noun verb etc in
the tasks... there are many words in the description of the tasks that do
not show any of the linked data").

services/entity_linker.py's link(project, text) resolves free text against
the SAME OntologyGraph SPARQL store every other ontology surface reads.
Seeds a throwaway project's ABox directly via OntologyGraph.rebuild(rows=...)
-- the same hand-built-rows pattern test_prototype_axioms.py already uses --
so the label index is fully under this test's control.
"""

from __future__ import annotations

import uuid

import pytest

TASK_ID = "11111111-2222-3333-4444-555555555555"
TASK_TITLE = "Slack triage support flow"
DOC_PATH = "services/foo/bar.py"
MEMORY_ID = "mx-abc123"


@pytest.fixture
def project(monkeypatch):
    """A throwaway project with a hand-built ABox: one task (whose title
    itself contains the words 'Slack' and 'triage'), one document, one
    memory. PRISM_KNOWN_JIRA_PROJECTS makes 'PLAT-42' a recognized ticket,
    the same known-project gate signal_parse.parse() itself enforces."""
    from prism_service.services.ontology_graph import OntologyGraph

    monkeypatch.setenv("PRISM_KNOWN_JIRA_PROJECTS", "PLAT")
    pid = f"entity-linker-{uuid.uuid4().hex[:8]}"
    rows = {
        "channels": ["slack"], "agents": [], "providers": [],
        "tasks": [{"id": TASK_ID, "title": TASK_TITLE, "channel": "slack"}],
        "signals": [],
        "documents": [DOC_PATH],
        "code_kinds": [],
        "memories": [{
            "id": MEMORY_ID, "name": "A great decision", "type": "decision",
            "domain": "", "cites": [], "evidence_task": "", "evidence_files": [],
        }],
    }
    OntologyGraph(pid).rebuild(rows=rows, agent_descriptions={}, signal_arrived_at={})
    return pid


def _sample_text() -> str:
    return (
        f"See task {TASK_ID} and read the {TASK_TITLE} plan. "
        f"Also check {DOC_PATH} and {MEMORY_ID}. "
        "We route new work over slack, and triage runs every morning. "
        "Ticket PLAT-42 tracks this. "
        "Note: the task stays fine regardless."
    )


# ---------------------------------------------------------------------------
# link() finds exactly the seven entities, each with the right cls/href,
# and never links plain words like "the"/"task"
# ---------------------------------------------------------------------------

def test_link_finds_every_entity_with_the_right_cls_and_href(project):
    from prism_service.services import entity_linker

    text = _sample_text()
    spans = entity_linker.link(project, text)
    by_text = {s["text"]: s for s in spans}

    task_span = by_text[TASK_ID]
    assert task_span["cls"] == "Task"
    assert task_span["href"] == f"/tasks/{TASK_ID}"

    title_span = by_text[TASK_TITLE]
    assert title_span["cls"] == "Task"
    assert title_span["href"] == f"/tasks/{TASK_ID}"

    doc_span = by_text[DOC_PATH]
    assert doc_span["cls"] == "Document"
    assert doc_span["href"] == f"/files?path={DOC_PATH.replace('/', '%2F')}"

    mem_span = by_text[MEMORY_ID]
    assert mem_span["cls"] == "Decision"
    assert mem_span["href"] == f"/understand?concept={MEMORY_ID}"

    slack_span = by_text["slack"]
    assert slack_span["cls"] == "Term"
    assert slack_span["href"] == "/ontology?tab=terms"

    triage_span = by_text["triage"]
    assert triage_span["cls"] == "Term"
    assert triage_span["href"] == "/ontology?tab=terms"

    ticket_span = by_text["PLAT-42"]
    assert ticket_span["cls"] == "JiraIssue"
    assert ticket_span["href"] == ""  # no Jira connection wired in this test

    # task 8a6f175b: "Ticket" is now a lexicon synonym for the Task term
    # (model-lexicon.ttl o:altLabel "ticket"), so it links too -- an 8th
    # span this fixture's text did not carry before that change.
    synonym_span = by_text["Ticket"]
    assert synonym_span["cls"] == "Term"
    assert synonym_span["label"] == "Task"
    assert "term=Task" in synonym_span["href"]

    assert len(spans) == 8, spans

    for start, end in ((s["start"], s["end"]) for s in spans):
        assert text[start:end] in by_text

    # "the"/"task" (both lowercase, plain prose) never link.
    for tok in ("the", "task"):
        idx = text.index(f" {tok} ")
        assert not any(s["start"] <= idx < s["end"] for s in spans), tok


# ---------------------------------------------------------------------------
# a memory's NAME links too, not just its raw mx-id (task 44c7e2d0 follow-up,
# owner: "pet names like that can be used, but only if linked to reality").
# Live symptom: task 44c7e2d0's title read "Promote ARC-PRISM-1 to law" --
# "ARC-PRISM-1" is memory mx-6320ab's name/pet-name, opaque on its own, and
# never became a link anywhere it appeared, because _build_index only ever
# registered a memory by its raw id. Tasks already got both (id + label,
# task 8a6f175b/2ec1e395) -- this mirrors that for memory.
# ---------------------------------------------------------------------------

def test_memory_name_links_the_same_as_its_raw_id(project):
    from prism_service.services import entity_linker

    text = f"See {MEMORY_ID} — also known as A great decision."
    spans = entity_linker.link(project, text)
    by_text = {s["text"]: s for s in spans}

    id_span = by_text[MEMORY_ID]
    name_span = by_text["A great decision"]
    assert id_span["cls"] == name_span["cls"] == "Decision"
    assert id_span["href"] == name_span["href"] == f"/understand?concept={MEMORY_ID}"


# ---------------------------------------------------------------------------
# longest match wins: inside the title occurrence, "Slack"/"triage" are NOT
# separately spanned as Term hits -- only the whole title is
# ---------------------------------------------------------------------------

def test_longest_match_wins_inside_a_title_containing_a_channel_word(project):
    from prism_service.services import entity_linker

    text = f"Open the {TASK_TITLE} page."
    spans = entity_linker.link(text=text, project=project)
    assert len(spans) == 1
    assert spans[0]["text"] == TASK_TITLE
    assert spans[0]["cls"] == "Task"


# ---------------------------------------------------------------------------
# a task id's 8-char prefix resolves to the same href as the full id
# ---------------------------------------------------------------------------

def test_task_id_prefix_resolves_same_as_full_id(project):
    from prism_service.services import entity_linker

    spans = entity_linker.link(project, f"see {TASK_ID[:8]} for context")
    assert len(spans) == 1
    assert spans[0]["href"] == f"/tasks/{TASK_ID}"


# ---------------------------------------------------------------------------
# no spans at all for empty/plain text
# ---------------------------------------------------------------------------

def test_link_is_quiet_on_plain_text_with_nothing_to_link(project):
    from prism_service.services import entity_linker

    assert entity_linker.link(project, "") == []
    assert entity_linker.link(project, "just an ordinary sentence here") == []


# ---------------------------------------------------------------------------
# GET /api/tasks/{id}/links returns spans for a seeded description
# ---------------------------------------------------------------------------

def test_get_task_links_route_returns_spans_for_the_description(project):
    from prism_service.api import tasks as tasks_api
    from prism_service.project_context import get_project

    ctx = get_project(project)
    t = ctx.task_svc.create(title="carrier", channel="ui")
    ctx.task_svc.update(t.id, description=f"Related to {TASK_TITLE} and {DOC_PATH}.")

    out = tasks_api.get_task_links(t.id, project=project)
    kinds = {s["cls"] for s in out["spans"]}
    assert "Task" in kinds
    assert "Document" in kinds
    assert len(out["spans"]) >= 2


# ---------------------------------------------------------------------------
# GET/POST /api/okf/ontology/link answer the same shape
# ---------------------------------------------------------------------------

def test_okf_ontology_link_get_and_post_routes(project):
    from prism_service.api import okf

    text = f"see {TASK_ID}"
    out_get = okf.ontology_link(project=project, text=text)
    out_post = okf.ontology_link_post({"text": text}, project=project)
    assert out_get == out_post
    assert out_get["spans"][0]["cls"] == "Task"


# ---------------------------------------------------------------------------
# the index is CACHED -- a second link() call does no further SPARQL work
# (asserted by tripwiring OntologyGraph.query, since a cache hit answers
# from the plain dict index alone)
# ---------------------------------------------------------------------------

def test_index_is_cached_between_calls(project, monkeypatch):
    from prism_service.services import entity_linker
    from prism_service.services.ontology_graph import OntologyGraph

    entity_linker.link(project, TASK_ID)  # warms the cache

    real_query = OntologyGraph.query
    calls = []

    def _tripwire(self, *a, **k):
        calls.append(1)
        return real_query(self, *a, **k)

    monkeypatch.setattr(OntologyGraph, "query", _tripwire)
    entity_linker.link(project, TASK_ID)
    assert calls == [], "a cached index must not re-query SPARQL"


# ---------------------------------------------------------------------------
# frontend: LinkedText renders spans as links carrying data-cls; TaskDetail/
# Queue/Understand import and USE it (JSX, not merely a comment)
# ---------------------------------------------------------------------------

_WEB_SRC = None


def _web_src():
    global _WEB_SRC
    if _WEB_SRC is None:
        from pathlib import Path

        _WEB_SRC = (Path(__file__).resolve().parent.parent.parent
                    / "prism_service" / "web" / "src")
    return _WEB_SRC


def test_linked_text_component_renders_link_and_a_with_data_cls():
    src = (_web_src() / "components" / "LinkedText.tsx").read_text(encoding="utf-8")
    assert "data-cls={span.cls}" in src
    assert "<Link to={span.href}" in src
    assert '<a href={span.href}' in src


@pytest.mark.parametrize("page", ["TaskDetailPage.tsx", "QueuePage.tsx", "UnderstandPage.tsx"])
def test_page_imports_and_uses_linked_text(page):
    src = (_web_src() / "pages" / page).read_text(encoding="utf-8")
    assert 'import LinkedText from "@/components/LinkedText"' in src
    assert "<LinkedText" in src


# ---------------------------------------------------------------------------
# TaskDetailPage still renders the description through <Markdown> -- the
# stop_if guard: markdown rendering must never be replaced, only spliced
# into via linkText/spliceLinkedMarkdown
# ---------------------------------------------------------------------------

def test_task_detail_page_keeps_markdown_for_the_description():
    src = (_web_src() / "pages" / "TaskDetailPage.tsx").read_text(encoding="utf-8")
    assert "<Markdown text={linkedDescription || task.description} />" in src
    # task 2ec1e395 additionally imports renderInline (named) alongside the
    # same default Markdown import -- the default import itself is untouched.
    assert 'import Markdown, { renderInline } from "@/components/Markdown"' in src
    assert "spliceLinkedMarkdown" in src


# ---------------------------------------------------------------------------
# Regression (found live 2026-08-25): the index must see EVERY ABox row.
# OntologyGraph.query() defaults to limit=500 -- with 935 documents ahead of
# the tasks/memories in the store, no task id or memory id ever linked on
# the prism project. Seed more than 500 documents and prove the task id,
# the memory id and the LAST document still link.
# ---------------------------------------------------------------------------

def test_index_sees_every_instance_past_the_query_default_limit(monkeypatch):
    from prism_service.services.ontology_graph import OntologyGraph
    from prism_service.services import entity_linker

    pid = f"entity-linker-big-{uuid.uuid4().hex[:8]}"
    docs = [f"services/pkg/module_{i:04d}.py" for i in range(600)]
    rows = {
        "channels": ["slack"], "agents": [], "providers": [],
        "tasks": [{"id": TASK_ID, "title": TASK_TITLE, "channel": "slack"}],
        "signals": [], "documents": docs, "code_kinds": [],
        "memories": [{"id": MEMORY_ID, "name": "A great decision", "type": "decision",
                      "domain": "", "cites": [], "evidence_task": "", "evidence_files": []}],
    }
    OntologyGraph(pid).rebuild(rows=rows, agent_descriptions={}, signal_arrived_at={})
    spans = entity_linker.link(pid, f"see {TASK_ID[:8]} and {MEMORY_ID} and {docs[-1]}")
    texts = {s["text"] for s in spans}
    assert TASK_ID[:8] in texts, spans
    assert MEMORY_ID in texts, spans
    assert docs[-1] in texts, spans
