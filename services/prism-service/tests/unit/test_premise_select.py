"""The premise node SELECTS its facts instead of refusing on count.

Task 6738006b. `_codified_step_proof` used to return "" whenever the
gather resolved more than four facts, which handed the step to a paid
agentic judge. Measured 2026-09-08 over the 10 tasks blocked at
review_previous_notes: 7 were refused by that count bail, and 6 of the 7
render a Premises section arc_governance.score_premise_grounded accepts,
at zero tokens.

These tests pin the replacement: a deterministic, coverage-first
selection that keeps the load-bearing few, names what it dropped, and
still renders only what the REAL rubric would accept.
"""
from __future__ import annotations

import types

from prism_service.services import arc_governance as gov
from prism_service.services import premise_gather as pg
from prism_service.services import task_runner as tr
from prism_service.services.premise_gather import GatheredFact


def _task(oracle: str = "", title: str = "A task", description: str = ""):
    return types.SimpleNamespace(id="t-1", title=title,
                                 description=description, oracle=oracle)


def _fact(i: int, text: str, path: str = "services/x.py"):
    return GatheredFact(kind="memory", text=text, citation=f"{path}:{i}")


# --- the bound still holds ------------------------------------------------

def test_a_tight_set_keeps_every_fact():
    """Selection is for the wide set. At or under the bound there is
    nothing to choose between, so nothing is dropped."""
    facts = [_fact(i, f"fact {i}") for i in range(pg.DEFAULT_KEEP_MAX)]

    chosen = pg.select(_task(), facts)

    assert chosen.kept == facts
    assert chosen.dropped == []


def test_a_wide_set_is_bounded_and_names_what_it_dropped():
    """The invariant the old count bail carried: a formatter must never
    assert every retrieved fact wholesale."""
    facts = [_fact(i, f"fact {i}") for i in range(pg.DEFAULT_KEEP_MAX + 4)]

    chosen = pg.select(_task(), facts)

    assert len(chosen.kept) == pg.DEFAULT_KEEP_MAX
    assert len(chosen.dropped) == 4
    assert set(chosen.kept).isdisjoint(chosen.dropped)
    assert str(len(facts)) in chosen.reason and "kept" in chosen.reason


def test_selection_is_deterministic():
    """No model, no randomness: the same input selects the same facts."""
    facts = [_fact(i, f"lease holder {i}") for i in range(9)]
    task = _task(oracle="A second driver refuses and names the holder.")

    first = pg.select(task, facts)
    second = pg.select(task, facts)

    assert [f.text for f in first.kept] == [f.text for f in second.kept]


# --- coverage first: the recorded misfire ---------------------------------

def test_selection_keeps_the_fact_that_engages_an_oracle_clause():
    """The recorded misfire: dropping a fact must not cost a clause the
    citation that was engaging it. The engaging fact sits LAST, past the
    bound, so only coverage-first ordering can save it."""
    oracle = ("A second driver refuses and names the holder. "
              "The expired lease lets a later drive claim the task.")
    filler = [_fact(i, f"unrelated bookkeeping note {i}") for i in range(8)]
    engaging = _fact(99, "The lease expires so a later drive claims "
                          "the task instead of wedging")

    chosen = pg.select(_task(oracle=oracle), filler + [engaging])

    assert engaging in chosen.kept
    assert len(chosen.kept) == pg.DEFAULT_KEEP_MAX


def test_a_wide_set_renders_a_section_the_real_rubric_accepts():
    """The whole point: what the old bail refused, the rubric accepts.

    Checked against arc_governance's own scorer -- the same function the
    gate runs -- never a local copy of its regexes.
    """
    oracle = "A second driver refuses and names the holder."
    facts = [_fact(i, f"the driver holds the lease and names holder {i}")
             for i in range(pg.DEFAULT_KEEP_MAX + 3)]
    task = _task(oracle=oracle)

    chosen = pg.select(task, facts)
    rendered = pg.render_premises(task, chosen.kept)
    rubric = gov.load_rubrics().get("premise_grounded") or {}

    verdict = gov.score_premise_grounded(
        {"notes_md": rendered, "oracle": oracle}, rubric)
    assert verdict.get("ok"), verdict.get("reason")


# --- the runner takes the codified path now -------------------------------

def test_the_runner_renders_a_wide_set_at_zero_tokens():
    """`_codified_step_proof` returned "" for this input before task
    6738006b, which is what sent task 1bcb2b24 to the judge that failed 6
    dispatches and parked it 3 times."""
    facts = [_fact(i, f"the runner drives one task per tick {i}")
             for i in range(pg.DEFAULT_KEEP_MAX + 2)]

    out = tr._codified_step_proof("review_previous_notes", _task(), facts)

    assert out.startswith("## Premises")


def test_an_empty_gather_still_refuses():
    """With nothing gathered there is nothing honest to assert, so the
    step must still fall through to the model rather than invent claims."""
    assert tr._codified_step_proof("review_previous_notes", _task(), []) == ""
