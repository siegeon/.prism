"""The premise section is RENDERED from resolved facts, not retyped by a model.

THE DOMINANT LOSS CONDITION this closes. Measured live on 2026-09-05:
premise_grounded refused 273 advances across 141 distinct tasks -- task
cdb8e365 alone 82 times, 4c9b39e5 66 times. Every refusal burns a whole
`claude -p` drive and returns the task to the same step it started on.

The facts were already in hand each time. premise_gather.gather()'s own
docstring: "Every element's citation already satisfies one of
arc_governance's grounding regexes ... so an agentic judge that reuses a
fact's citation verbatim will always pass premise_grounded's citation
tooth." The runner hands those facts to the model in its prompt, asks it to
retype them into a `## Premises` section, and the model does not -- so the
step parks. Identical shape to the red-test-ids stall: PRISM holds the fact,
asks a model to restate it, and refuses when it doesn't.

render_premises() closes it by BUILDING that section deterministically. It
grades against the real rubric functions (score_premise_grounded's citation
tooth and score_oracle_engagement), never a local copy, so the render cannot
drift from what actually decides.

HONESTY IS THE POINT, not a green light. Every bullet's citation comes from
a real gathered fact -- none is invented -- and an oracle clause that no
fact engages is marked `clause N: UNRESOLVED, <why>`, which is the marker
score_oracle_engagement itself names ("or mark it 'clause N: UNRESOLVED,
<why>' if it is a real gap"). A clause with nothing behind it must read as a
named gap, never as engaged.
"""
from __future__ import annotations

import types

import pytest

from prism_service.services import premise_gather
from prism_service.services.arc_governance import score_oracle_engagement

RUBRIC = {"claims_section": "Premises", "require_oracle_engagement": True,
          "oracle_word_min_len": 5, "oracle_min_shared_words": 2}


def _facts():
    """Two real gathered facts, in the shape gather() returns."""
    return [
        premise_gather.GatheredFact(
            kind="memory",
            text="The runner drives one task per tick",
            citation="`memory_recall(\"drive worker\") -> project-drive-worker`"),
        premise_gather.GatheredFact(
            kind="symbol",
            text="'Task' is defined at services/prism-service/prism_service/models/task.py:10",
            citation="services/prism-service/prism_service/models/task.py:10"),
    ]


def _task(oracle=""):
    return types.SimpleNamespace(id="t-1", title="A task", oracle=oracle,
                                 description="")


# --- AC-1: every rendered bullet passes the REAL citation tooth ----------

def test_the_rendered_section_passes_the_citation_tooth(monkeypatch):
    rendered = premise_gather.render_premises(_task(), _facts())

    verdict = premise_gather.citation_check(rendered)
    assert verdict["ok"] is True, verdict
    assert verdict["claims_checked"] == 2, verdict
    assert verdict["failing"] == []


def test_every_citation_comes_from_a_real_fact_never_invented():
    """The render may only repeat citations it was GIVEN. A section that
    passes by fabricating a file:line would be worse than the stall."""
    facts = _facts()
    rendered = premise_gather.render_premises(_task(), facts)

    for f in facts:
        assert f.citation in rendered, f.citation
    # Nothing that looks like a citation but was never gathered.
    assert "models/task.py:10" in rendered
    assert "models/task.py:11" not in rendered


# --- AC-2: oracle clauses are engaged, or named as gaps -----------------

def test_a_clause_the_facts_engage_is_reported_as_engaged():
    oracle = ("(1) The runner drives one task per tick. "
              "(2) Nothing unrelated happens.")
    facts = [premise_gather.GatheredFact(
        kind="memory", text="The runner drives one task per tick",
        citation="`memory_recall(\"runner\") -> project-runner`")]

    rendered = premise_gather.render_premises(_task(oracle), facts)

    # Clause 1 shares real words with the fact, so it must NOT be marked a gap.
    assert "clause 1: UNRESOLVED" not in rendered, rendered
    # Clause 2 has nothing behind it and must be named as a gap, not padded.
    assert "clause 2: UNRESOLVED" in rendered, rendered
    assert score_oracle_engagement(oracle, rendered, RUBRIC)["ok"] is True


def test_a_clause_with_nothing_behind_it_is_marked_unresolved_with_a_reason():
    oracle = "(1) Something no gathered fact mentions at all whatsoever."

    rendered = premise_gather.render_premises(_task(oracle), _facts())

    assert "clause 1: UNRESOLVED" in rendered, rendered
    # A bare marker is not enough -- the tooth's own wording asks for a why.
    line = next(l for l in rendered.splitlines() if "clause 1: UNRESOLVED" in l)
    assert len(line.split("UNRESOLVED,")[-1].strip()) > 10, line
    assert score_oracle_engagement(oracle, rendered, RUBRIC)["ok"] is True


def test_the_render_satisfies_both_teeth_together():
    """The whole point: one deterministic section that the real rubric
    accepts on BOTH counts, with no model involved."""
    oracle = ("(1) The runner drives one task per tick. "
              "(2) A gate is decided by a distinct actor. "
              "(3) Something entirely unmentioned.")

    rendered = premise_gather.render_premises(_task(oracle), _facts())

    assert premise_gather.citation_check(rendered)["ok"] is True
    assert score_oracle_engagement(oracle, rendered, RUBRIC)["ok"] is True


# --- AC-3: honest degradation -------------------------------------------

def test_no_facts_yields_no_fabricated_section():
    """With nothing gathered there is nothing honest to assert. The render
    must return empty rather than emit a section of invented claims."""
    assert premise_gather.render_premises(_task("(1) anything"), []) == ""


def test_a_task_with_no_oracle_still_renders_its_facts():
    rendered = premise_gather.render_premises(_task(""), _facts())
    assert premise_gather.citation_check(rendered)["ok"] is True
    assert "UNRESOLVED" not in rendered


# --- AC-4: the runner repairs a failing report instead of parking -------

def test_the_runner_repairs_an_ungrounded_report(monkeypatch):
    """A model report that cites nothing is completed with the rendered
    grounded section, so the step advances on real evidence instead of
    burning another drive on the same refusal."""
    from prism_service.services import task_runner as tr

    ungrounded = "## Premises\n- the thing is probably fine\n"
    repaired = tr._repair_premises(ungrounded, _task("(1) anything at all"),
                                   _facts())

    assert premise_gather.citation_check(repaired)["ok"] is True, repaired
    # the model's own prose is preserved, not discarded
    assert "the thing is probably fine" in repaired


def test_a_report_that_already_passes_is_left_alone():
    """No rewriting a report the model got right -- the repair is a floor,
    never a replacement."""
    from prism_service.services import task_runner as tr

    good = premise_gather.render_premises(_task(""), _facts())
    assert tr._repair_premises(good, _task(""), _facts()) == good


# --- AC-5: the NODE itself declares the codified render -----------------

def test_the_review_notes_node_declares_a_codified_render_step():
    """The upgrade is to the WORKFLOW NODE, not just the driver: the
    behaviour JSON the engine executes must carry the render as a real
    http-callback step, positioned AFTER the agentic loop (the model gets
    its chance first) and BEFORE check (so check grades a section that can
    actually pass)."""
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[4]
    node = json.loads(
        (root / ".prism/behaviors/conductor/review-previous-notes-loop.json")
        .read_text(encoding="utf-8"))
    ids = [s["id"] for s in node["steps"]]

    assert "render" in ids, ids
    assert ids.index("loop") < ids.index("render") < ids.index("check"), ids

    render = node["steps"][ids.index("render")]
    assert render["kind"] == "http-callback"
    assert "/api/workflows/steps/premise-render" in render["url"], render["url"]

    # Only the judge stays agentic; everything else is deterministic.
    agentic = [s["id"] for s in node["steps"] if "premise-judge" in s.get("url", "")]
    assert agentic == ["loop"], agentic


def test_the_render_endpoint_exists_and_is_codified():
    """The node's url must resolve to a real route that never calls a
    model -- a node step pointing at nothing is the built-but-unwired
    fault this whole change exists to stop repeating."""
    from prism_service.api import workflows as wf

    assert hasattr(wf, "workflow_step_premise_render")
    doc = wf.workflow_step_premise_render.__doc__ or ""
    assert "CODIFIED" in doc
    assert "Never calls a model" in doc
