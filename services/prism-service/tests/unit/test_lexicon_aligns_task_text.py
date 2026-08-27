"""A lexicon of canonical terms aligns every task write (task 2ee65e14,
epic df0eed4a, owner: "tasks converge on the ontology's standard
language"; owner decision 2026-08-26: the act itself is called ALIGN,
not converge — ontology alignment).

ontology/model-lexicon.ttl declares the canonical o:Term instances.
services/lexicon.py reads them and rewrites free text: every whole-word
synonym (singular or a plain plural), case-insensitively, becomes its
canonical label — everywhere except a protected span (code, a URL, a
file path, a quoted string, an id) that services/ste.py already knows
not to touch. services/ste.py's apply() runs normalize() then
lexicon.align(); services/task_service.py's own write path (_apply_ste)
runs the same align() per field, directly, so every task write goes
through it. services/ontology_terms.py serves the lexicon as its own
Terms-tab vocabulary; web/src/pages/OntologyPage.tsx renders a term's
synonyms when it carries them.

No model call anywhere in this file. Every assertion is a plain string,
list, or JSX-source comparison against a fixed input.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service.services import lexicon  # noqa: E402


def _mk_service(tmp_path):
    from prism_service.services.task_service import TaskService

    return TaskService(str(tmp_path / "tasks.db"))


# ----------------------------------------------------------------------
# The lexicon itself: unique labels/altLabels, one alignment per synonym.
# ----------------------------------------------------------------------


def test_labels_and_alt_labels_are_unique_across_the_lexicon():
    terms = lexicon.load_lexicon()
    labels = [t.label for t in terms]
    assert len(labels) == len(set(labels)), labels

    all_alt_labels = [alt for t in terms for alt in t.alt_labels]
    assert len(all_alt_labels) == len(set(a.lower() for a in all_alt_labels)), all_alt_labels

    everything = [l.lower() for l in labels] + [a.lower() for a in all_alt_labels]
    assert len(everything) == len(set(everything)), "a label collides with an alt label"


def _synonym_cases():
    cases = []
    for term in lexicon.load_lexicon():
        for alt in term.alt_labels:
            cases.append((alt, term.label))
    return cases


@pytest.mark.parametrize("synonym,label", _synonym_cases())
def test_every_synonym_aligns_to_its_canonical_label(synonym, label):
    aligned, applied = lexicon.align(f"Please see the {synonym} for details.")
    assert label in aligned, (synonym, label, aligned)
    assert {"from": synonym, "to": label} in applied


# ----------------------------------------------------------------------
# The worked example from the task brief.
# ----------------------------------------------------------------------


def test_worked_example_aligns_three_synonyms_at_once():
    text = "Open a ticket for the PR and link the memory entry"
    aligned, applied = lexicon.align(text)
    assert aligned == "Open a Task for the PullRequest and link the Concept"
    assert applied == [
        {"from": "ticket", "to": "Task"},
        {"from": "PR", "to": "PullRequest"},
        {"from": "memory entry", "to": "Concept"},
    ]


# ----------------------------------------------------------------------
# Plurals: the lexicon stores only the singular alt label, but align()
# recognizes a plain plural of it and emits a plural of the canonical
# label too.
# ----------------------------------------------------------------------


def test_plural_synonyms_align_to_plural_canonical_labels():
    aligned, applied = lexicon.align("Review the tickets and the PRs before merge.")
    assert aligned == "Review the Tasks and the PullRequests before merge."
    assert applied == [
        {"from": "tickets", "to": "Tasks"},
        {"from": "PRs", "to": "PullRequests"},
    ]


def test_singular_form_still_aligns_next_to_its_plural():
    aligned, _applied = lexicon.align("One ticket became three tickets.")
    assert aligned == "One Task became three Tasks."


# ----------------------------------------------------------------------
# Protected spans: align() reuses ste's own span finder, so a synonym
# inside code, a URL, a path, or a quoted string survives untouched.
# ----------------------------------------------------------------------


def test_synonym_inside_code_span_survives():
    text = "Run `git ticket show` to see the raw fields."
    aligned, applied = lexicon.align(text)
    assert aligned == text
    assert applied == []


def test_synonym_inside_url_survives():
    text = "See https://example.com/docs/ticket/guide for the write-up."
    aligned, applied = lexicon.align(text)
    assert "https://example.com/docs/ticket/guide" in aligned
    assert not any(a["from"] == "ticket" for a in applied)


def test_synonym_inside_path_survives():
    text = "The fixture lives at tests/fixtures/ticket_sample.json today."
    aligned, applied = lexicon.align(text)
    assert "tests/fixtures/ticket_sample.json" in aligned
    assert not any(a["from"] == "ticket" for a in applied)


def test_synonym_inside_quoted_string_survives():
    text = 'The old label was "ticket" before this rename.'
    aligned, applied = lexicon.align(text)
    assert '"ticket"' in aligned
    assert applied == []


# ----------------------------------------------------------------------
# A common English word that is NOT one of the lexicon's synonyms (task
# 2ee65e14's brief: never add "issue" or "note" — ordinary non-domain
# words) is left alone.
# ----------------------------------------------------------------------


def test_ordinary_word_issue_is_left_alone():
    text = "the issue is that nothing changed"
    aligned, applied = lexicon.align(text)
    assert aligned == text
    assert applied == []


# ----------------------------------------------------------------------
# TaskService wiring: create() stores the aligned description,
# last_style["aligned"] lists the replacements, and the ste_normalise
# history row carries "before" text for every field it changed.
# ----------------------------------------------------------------------


def test_create_stores_aligned_description_and_records_before(tmp_path):
    svc = _mk_service(tmp_path)
    text = "File a ticket for the PR."
    task = svc.create(title="Wiring check", description=text)

    assert task.description == "File a Task for the PullRequest."
    assert {"field": "description", "from": "ticket", "to": "Task"} in svc.last_style["aligned"]
    assert {"field": "description", "from": "PR", "to": "PullRequest"} in svc.last_style["aligned"]

    rows = svc.history(task.id)
    normalise_rows = [h for h in rows if h.action == "ste_normalise"]
    assert normalise_rows, [h.action for h in rows]
    details = normalise_rows[0].details
    assert "rules=" in details and "lexicon" in details
    assert "before=" in details
    before_json = details.split("before=", 1)[1]
    before = json.loads(before_json)
    assert before.get("description") == text


def test_update_stores_aligned_description_and_records_before(tmp_path):
    svc = _mk_service(tmp_path)
    task = svc.create(title="Wiring check", description="Starter text.")

    text = "Link the memory entry to the umbrella task."
    updated = svc.update(task.id, description=text)

    assert updated.description == "Link the Concept to the Epic."
    assert {"field": "description", "from": "memory entry", "to": "Concept"} in svc.last_style["aligned"]
    assert {"field": "description", "from": "umbrella task", "to": "Epic"} in svc.last_style["aligned"]

    rows = svc.history(task.id)
    normalise_rows = [h for h in rows if h.action == "ste_normalise"]
    assert normalise_rows, [h.action for h in rows]
    latest = normalise_rows[-1]
    assert "before=" in latest.details
    before = json.loads(latest.details.split("before=", 1)[1])
    assert before.get("description") == text


def test_oracle_field_aligns_too_in_strict_mode(tmp_path):
    svc = _mk_service(tmp_path)
    task = svc.create(
        title="Oracle wiring check",
        oracle="The PR merges and the ticket closes.",
    )
    assert task.oracle == "The PullRequest merges and the Task closes."
    assert {"field": "oracle", "from": "PR", "to": "PullRequest"} in svc.last_style["aligned"]


# ----------------------------------------------------------------------
# ontology_terms.terms(project) serves the lexicon as its own vocabulary.
# ----------------------------------------------------------------------


def test_terms_payload_carries_the_lexicon_vocabulary():
    import uuid

    from prism_service.project_context import get_project
    from prism_service.services import ontology_terms

    pid = f"lexicon-terms-{uuid.uuid4().hex[:8]}"
    ctx = get_project(pid)
    ctx.task_svc.create(title="a real task", channel="ui")

    out = ontology_terms.terms(pid)
    names = {v["name"] for v in out["vocabularies"]}
    assert "lexicon" in names

    lex = next(v for v in out["vocabularies"] if v["name"] == "lexicon")
    assert lex["comment"]

    task_term = next(t for t in lex["terms"] if t["value"] == "Task")
    assert task_term["comment"]
    assert set(task_term["synonyms"]) == {"ticket", "work item"}
    assert task_term["denotes"] == "Task"
    assert task_term["count"] >= 0

    workflow_term = next(t for t in lex["terms"] if t["value"] == "Workflow")
    assert workflow_term["denotes"] == ""


# ----------------------------------------------------------------------
# OntologyPage.tsx renders a lexicon term's synonyms — a source-reading
# assertion against the REAL rendered JSX branch (the condition that
# actually gates the "also: ..." line), never a comment near it.
# ----------------------------------------------------------------------


def test_ontology_page_renders_lexicon_synonyms_in_its_own_jsx_branch():
    tsx_path = (_SERVICE_ROOT / "prism_service" / "web" / "src"
                / "pages" / "OntologyPage.tsx")
    src = tsx_path.read_text(encoding="utf-8")

    # The Term type carries the lexicon's extra fields.
    assert "synonyms?: string[]" in src
    assert "comment?: string" in src

    # The actual conditional branch that gates the synonym block — not a
    # standalone comment, the real `t.synonyms ? (` ternary that decides
    # what renders for a term.
    assert "t.synonyms ? (" in src

    # Inside that branch, the definition and the "also: ..." line render
    # from the term's own fields.
    branch_start = src.index("t.synonyms ? (")
    branch_end = src.index("))", branch_start)
    branch = src[branch_start:branch_end]
    assert "{t.comment}" in branch
    assert "also: {t.synonyms.join(" in branch


# ----------------------------------------------------------------------
# Guard (epic cc9a44c8, 2026-08-26): a single common English word as a
# synonym changes meaning at scale. The live backfill rewrote "story",
# "card", "proof" and "memory" in 525 tasks before this guard existed.
# Every one-word synonym must be a deliberate entry in this allowlist.
# ----------------------------------------------------------------------

_SINGLE_WORD_SYNONYMS_ALLOWED = {
    "ticket", "PR", "MR", "ADR", "doc", "assistant",
    # "bot" left this list on 2026-08-27 (mx-0e5a88): a Bot is a tier-1
    # deterministic workflow, not an Agent; see test_bot_is_never_a_synonym.
}


def test_every_single_word_synonym_is_deliberately_allowed():
    offenders = []
    for term in lexicon.load_lexicon():
        for alt in term.alt_labels:
            if " " not in alt and "-" not in alt and alt not in _SINGLE_WORD_SYNONYMS_ALLOWED:
                offenders.append((alt, term.label))
    assert not offenders, f"single-word synonyms need a deliberate allowlist entry: {offenders}"


def test_common_words_never_align():
    for text in ("The story step drafts the story.", "The gate card shows the proof.",
                 "Keep it in memory.", "Run the CI pipeline.", "Add a checkpoint.", "Get sign-off."):
        aligned, applied = lexicon.align(text)
        assert aligned == text and applied == [], (text, aligned, applied)


def test_bot_is_never_a_synonym():
    """Owner 2026-08-27 (mx-0e5a88): "the top level are bots (tier 1
    workflows) that have deterministic flows"; a Bot is not an Agent, so the
    lexicon must leave the word alone. Before this the aligner rewrote a
    ticket titled "Bot is its own term, not a synonym of Agent" into "Agent
    is its own term, not a synonym of Agent" (task 8bcd4cb3)."""
    from prism_service.services.lexicon import load_lexicon, align

    for term in load_lexicon():
        assert "bot" not in [a.lower() for a in term.alt_labels], term.label
    text = "The Bot runs a deterministic flow. A bot is not an agent."
    aligned, _changes = align(text)
    assert aligned == text
