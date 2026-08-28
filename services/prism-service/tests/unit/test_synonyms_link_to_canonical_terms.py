"""Synonyms in old text still link to their canonical term (task 8a6f175b,
epic df0eed4a: "Task language aligns with the ontology's standard
language"). entity_linker.link() now indexes every o:Term's rdfs:label
and o:altLabel (services/lexicon.py, ontology/model-lexicon.ttl)
case-insensitively and plural-aware, pointed at
/ontology?tab=terms&term=<label>. An instance label (a task title) always
wins over a same-text synonym, and a synonym links at most once per
paragraph -- see services/entity_linker.py's module docstring.
"""

from __future__ import annotations

import uuid

import pytest

PR_TASK_ID = "77777777-8888-9999-aaaa-bbbbbbbbbbbb"
PR_TASK_TITLE = "PR triage"


@pytest.fixture
def project():
    """A throwaway project whose ABox holds one task titled 'PR triage' --
    used to prove an instance title wins over the 'PR' synonym of the
    same text. The lexicon lives in the TBox, loaded by rebuild()'s own
    load_model() call, so no fixture setup is needed for it."""
    from prism_service.services.ontology_graph import OntologyGraph

    pid = f"synonym-link-{uuid.uuid4().hex[:8]}"
    rows = {
        "channels": [], "agents": [], "providers": [],
        "tasks": [{"id": PR_TASK_ID, "title": PR_TASK_TITLE, "channel": "ui"}],
        "signals": [], "documents": [], "code_kinds": [], "memories": [],
    }
    OntologyGraph(pid).rebuild(rows=rows, agent_descriptions={}, signal_arrived_at={})
    return pid


# ---------------------------------------------------------------------------
# "ticket" -> Task, "PR" -> PullRequest, both as Term links to the canonical
# label's href
# ---------------------------------------------------------------------------

def test_synonyms_link_to_their_canonical_term(project):
    from prism_service.services import entity_linker

    spans = entity_linker.link(project, "Open a ticket for the PR.")
    by_text = {s["text"]: s for s in spans}

    ticket_span = by_text["ticket"]
    assert ticket_span["cls"] == "Term"
    assert ticket_span["label"] == "Task"
    assert "term=Task" in ticket_span["href"]

    pr_span = by_text["PR"]
    assert pr_span["cls"] == "Term"
    assert pr_span["label"] == "PullRequest"
    assert "term=PullRequest" in pr_span["href"]


# ---------------------------------------------------------------------------
# a plain plural of a synonym links too, to the SINGULAR canonical label
# ---------------------------------------------------------------------------

def test_plural_synonym_links_to_the_singular_canonical_label(project):
    from prism_service.services import entity_linker

    spans = entity_linker.link(project, "Review the tickets")
    by_text = {s["text"]: s for s in spans}

    assert "tickets" in by_text
    assert by_text["tickets"]["label"] == "Task"
    assert "term=Task" in by_text["tickets"]["href"]


# ---------------------------------------------------------------------------
# precedence: an instance label always wins over a synonym of the same text
# ---------------------------------------------------------------------------

def test_instance_title_wins_over_a_synonym_of_the_same_text(project):
    from prism_service.services import entity_linker

    spans = entity_linker.link(project, f"See {PR_TASK_TITLE} for the plan.")
    assert len(spans) == 1
    assert spans[0]["text"] == PR_TASK_TITLE
    assert spans[0]["cls"] == "Task"
    assert spans[0]["href"] == f"/tasks/{PR_TASK_ID}"


# ---------------------------------------------------------------------------
# an unrelated ordinary word never links
# ---------------------------------------------------------------------------

def test_unrelated_word_produces_no_link(project):
    from prism_service.services import entity_linker

    assert entity_linker.link(project, "the issue is that") == []


# ---------------------------------------------------------------------------
# link soup guard: a synonym mentioned three times in one paragraph links
# once, at its first occurrence
# ---------------------------------------------------------------------------

def test_synonym_links_once_per_paragraph(project):
    from prism_service.services import entity_linker

    text = "A ticket came in. Another ticket followed. A third ticket too."
    spans = entity_linker.link(project, text)

    ticket_spans = [s for s in spans if s["text"].lower() == "ticket"]
    assert len(ticket_spans) == 1
    assert ticket_spans[0]["start"] == text.index("ticket")
    assert ticket_spans[0]["label"] == "Task"


# ---------------------------------------------------------------------------
# GET /api/tasks/{id}/links's vocabulary block lists the synonyms and
# canonical labels a field's text reached.
#
# TaskService._apply_ste runs lexicon.align() on every WRITE to
# description/oracle/etc, so a fresh write through the service would
# already have rewritten "ticket"/"PR" to "Task"/"PullRequest" -- it could
# never prove old, un-aligned synonym text still links. plan_doc USED TO
# be the one field _apply_ste only CHECKED and never rewrote (task
# 36283d72). Task dc676e24 (2026-08-26) made plan_doc align at write too,
# through TaskService._align_plan_doc, so a normal update() no longer
# leaves it un-aligned either -- this test now writes plan_doc with raw
# SQL (bypassing _apply_ste entirely, the same bypass
# test_align_language_workflow.py's _seed_tasks uses) to land real,
# un-aligned "old text" the way a legacy/pre-dc676e24 row would carry it.
# ---------------------------------------------------------------------------

def test_vocabulary_block_lists_synonyms_and_canonical_labels(project):
    from prism_service.api import tasks as tasks_api
    from prism_service.project_context import get_project

    ctx = get_project(project)
    t = ctx.task_svc.create(title="carrier", channel="ui")
    ctx.task_svc._db.execute(
        "UPDATE tasks SET plan_doc=? WHERE id=?",
        ("Open a ticket for the PR.", t.id))
    ctx.task_svc._db.commit()

    out = tasks_api.get_task_links(t.id, project=project)
    vocab = out["vocabulary"]["plan_doc"]

    assert vocab["canonical"] == ["Task", "PullRequest"]
    assert {"text": "ticket", "canonical": "Task"} in vocab["synonyms"]
    assert {"text": "PR", "canonical": "PullRequest"} in vocab["synonyms"]


def test_vocabulary_block_is_empty_for_a_field_with_no_synonyms(project):
    from prism_service.api import tasks as tasks_api
    from prism_service.project_context import get_project

    ctx = get_project(project)
    t = ctx.task_svc.create(title="carrier2", channel="ui")
    ctx.task_svc.update(t.id, plan_doc="just an ordinary sentence here")

    out = tasks_api.get_task_links(t.id, project=project)
    vocab = out["vocabulary"]["plan_doc"]
    assert vocab == {"canonical": [], "synonyms": []}
