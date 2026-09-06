"""Recall is asked one topical term at a time, and only real matches count.

THE DEFECT (owner, 2026-09-05, on a query reading "bot OR hands OR its OR
task OR the OR next OR flow"): _gather_memories passed the whole task title
as one query. A short generic sentence embeds into generic-workflow space,
so four unrelated tasks came back with the same five feedback-* memories,
two of them identical across all four -- and none about the task. Those
then became the task's own Premises.

Two things fix it, both measured live:
  * ONE TERM AT A TIME. The corpus does hold task-specific memories; the
    single word "planner" returns a-split-is-unsafe-until-the-planner
    -proves-it immediately, where the full title never reaches it.
  * A RELEVANCE FLOOR. A vector recall always returns nearest neighbours,
    so a term with no real match still yields entries -- "vocabulary"
    produced signal-loss-pill-live-verified. Requiring the term to appear
    in the entry turns "nothing matched" into an honest empty result
    instead of confident noise.
"""
from __future__ import annotations

import types

from prism_service.services import premise_gather as pg


class _Entry:
    def __init__(self, name, description=""):
        self.name, self.description, self.summary = name, description, ""
        self.id = name


class _Mem:
    """Records what it was asked, and answers nearest-neighbour style."""

    def __init__(self, by_term):
        self.by_term, self.asked = by_term, []

    def recall(self, query, limit=5):
        self.asked.append(query)
        return self.by_term.get(query, [_Entry("unrelated-nearest-neighbour")])[:limit]


def _task(title, oracle=""):
    return types.SimpleNamespace(id="t-1", title=title, oracle=oracle,
                                 description="")


def test_it_asks_one_topical_term_at_a_time_not_the_sentence():
    mem = _Mem({"planner": [_Entry("a-split-is-unsafe-until-the-planner-proves-it")]})

    pg._gather_memories(_task("The planner cuts slices that run in parallel"),
                        mem, 5)

    assert "The planner cuts slices that run in parallel" not in mem.asked
    assert "planner" in mem.asked, mem.asked
    # stopwords carry no topic and must never be asked about
    for junk in ("the", "that", "runs", "task", "node"):
        assert junk not in mem.asked, mem.asked


def test_a_nearest_neighbour_that_does_not_match_is_dropped():
    """The floor: 'vocabulary' returning signal-loss-pill is not evidence."""
    mem = _Mem({"vocabulary": [_Entry("signal-loss-pill-live-verified")]})

    facts = pg._gather_memories(_task("One node adjudicates the vocabulary"),
                                mem, 5)

    assert facts == [], [f.text for f in facts]


def test_a_real_match_is_kept_and_cites_the_term_that_found_it():
    mem = _Mem({"planner": [_Entry("a-split-is-unsafe-until-the-planner-proves-it")]})

    facts = pg._gather_memories(_task("The planner cuts slices"), mem, 5)

    assert len(facts) == 1, [f.text for f in facts]
    assert "planner-proves-it" in facts[0].text
    # the citation names the QUERY ACTUALLY MADE, not the whole title
    assert 'memory_recall("planner")' in facts[0].citation, facts[0].citation


def test_the_result_set_is_hard_capped():
    """Per-term querying multiplies candidates, which is exactly how a
    retrieval fix becomes a noise problem. The cap is the guard."""
    many = [_Entry(f"planner-memory-{i}", "planner") for i in range(20)]
    mem = _Mem({"planner": many, "slices": many, "parallel": many})

    facts = pg._gather_memories(
        _task("The planner cuts slices that run in parallel"), mem, 50)

    assert len(facts) <= pg._MEMORY_KEEP_LIMIT, len(facts)


def test_no_topical_term_means_no_query_at_all():
    mem = _Mem({})
    assert pg._gather_memories(_task("the task is done"), mem, 5) == []
