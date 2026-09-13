"""The root plan_gate certainty score must read the PLAN, not the ticket.

MEASURED DEFECT, 2026-09-13, on the live prism DB: every `gate_decide` row
carrying a `certainty=` value is an approval at exactly **1.00 -- 21 of 21**.
`plan_gate_certainty` had never once scored below its 0.90 threshold, so the
`conductor-adjudicator` seat had never refused a root plan_gate on certainty.

The proof case is task bb3d1f6a. A human seat rejected its plan_gate at
18:03:33 because plan_doc was fabricated tool-call markup. The daemon
re-planned in ~90s and the seat approved the replacement at 18:05:04 with
`plan_completeness 1.0, oracle_quality 1.0, diagram_quality 1.0,
scope_alignment 1.0`. That replacement is checked in verbatim at
tests/unit/fixtures/plan_doc_bb3d1f6a_placeholder.md: invented AC ids
(AC-123/456/789) and **zero** `oracle:` lines -- three acceptance criteria
that name no way at all to check them.

Measured over the 391 live plans: 150 carry an `oracle:` line under every AC,
151 carry none, 5 are partial. So the convention is real and half-followed,
and only 13 of the zero-oracle plans are still live -- that is the forward
blast radius of this guard, and every one of those 13 genuinely has ACs that
cannot be checked.

Why all four signals passed junk -- each grades SHAPE, never grounding:
  plan_completeness  word count against a floor; padding clears it.
  oracle_quality     reads task.oracle / task.likely_misfire -- the TICKET
                     AUTHOR's fields -- never the plan's own AC oracle lines.
  diagram_quality    the mermaid parses and has >1 edge; a generic A->B->C
                     box diagram passes.
  scope_alignment    compares the plan's CLAIMED files to allowed_files, so a
                     plan citing nothing contradicts nothing and scores 1.0.

The emptier the plan, the less there is to fail. This suite adds a fifth
signal, `plan_grounding`, which reads the plan's own body: does each AC carry
an `oracle:` line, and when the plan cites repo paths, do any of them exist.

NOTE ON FIXTURES: 7.13.187 already claimed to fix this exact "one constant
wearing four names" failure and shipped per-signal isolation tests. Those pass
while live data stays pinned at 1.00, because they assert against synthetic
packets built to be good or bad. This suite pins the REAL artifact that got
through.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

_FIXTURE = (Path(__file__).parent / "fixtures"
            / "plan_doc_bb3d1f6a_placeholder.md")

# The real ticket's own oracle/likely_misfire, both well over the 40-char
# floor and both naming checkable artifacts -- which is exactly why
# oracle_quality scored 1.0 while the PLAN engaged none of it.
_REAL_ORACLE = (
    "Open http://localhost:7780/workflows. The bot process shows one "
    "vocabulary node. Save a task whose body is the exact string `the PR is "
    "merged and the doc links the ticket`. Read the row on the task page. "
    "The body reads word for word as written.")
_REAL_MISFIRE = (
    "Removing the swap also removes the plain-English cleanup, so prose "
    "stops being normalised at all and the lexicon stops earning its keep.")

_PLACEHOLDER_DIAGRAM = "\n".join([
    "flowchart TD",
    " A[Bot Process] --> B[Lexicon Adjudication Node]",
    " B --> C[Ontology Model]",
    " C --> D[Term Lookup]",
    " D --> E[Term Addition]",
])

_GROUNDED_PLAN = "\n".join([
    "## Summary",
    "Restrict lexicon alignment to the title field so a task body keeps the "
    "words its author wrote, and give the vocabulary its own declared node "
    "rather than a swap buried in the write path.",
    "",
    "## Acceptance Criteria",
    "AC-1: a saved task body keeps the author's words byte for byte.",
    "oracle: services/prism-service/tests/unit/"
    "test_creating_a_ticket_swaps_no_words.py passes.",
    "AC-2: the title still aligns to canonical terms.",
    "oracle: saving the title `Fix the ticket page` stores `Fix the Task "
    "page`.",
])


@pytest.fixture()
def dp_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path / "data"))
    return "grounding-" + uuid.uuid4().hex[:8]


def _task(task_svc, plan_doc, plan_diagram, oracle, misfire):
    from prism_service.services.task_service import TaskService  # noqa: F401
    t = task_svc.create(title="grounding probe", oracle=oracle,
                        proof_type="test", parent_id="", workflow="implement")
    task_svc.update(t.id, workflow_step="plan_gate", gate_state="pending",
                    plan_doc=plan_doc, plan_diagram=plan_diagram,
                    likely_misfire=misfire)
    return task_svc.get(t.id)


def _svc(tmp_path):
    from prism_service.services.task_service import TaskService
    return TaskService(str(tmp_path / "tasks.db"))


def _placeholder(task_svc):
    """The REAL plan the seat approved at certainty=1.00 on task bb3d1f6a."""
    return _task(task_svc, _FIXTURE.read_text(encoding="utf-8"),
                 _PLACEHOLDER_DIAGRAM, _REAL_ORACLE, _REAL_MISFIRE)


def test_the_real_placeholder_plan_no_longer_clears_the_threshold():
    """The whole point. This exact packet scored 1.00 and cleared the gate."""
    from prism_service.services import design_packet as dp
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        task_svc = _svc(Path(td))
        t = _placeholder(task_svc)
        got = dp.plan_gate_certainty("probe", t.id, t)
    assert got["score"] < dp.certainty_threshold(), (
        f"the placeholder plan scored {got['score']} against threshold "
        f"{dp.certainty_threshold()} and would clear the gate again; "
        f"signals={got.get('signals')}"
    )


def test_plan_grounding_is_zero_when_no_ac_carries_an_oracle_line():
    from prism_service.services import design_packet as dp
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        task_svc = _svc(Path(td))
        t = _placeholder(task_svc)
        got = dp.plan_gate_certainty("probe", t.id, t)
    sig = got.get("signals", {})
    assert "plan_grounding" in sig, (
        f"expected a plan_grounding signal, got {sorted(sig)}")
    assert sig["plan_grounding"] == 0.0, (
        "a plan with AC-123/456/789 and zero `oracle:` lines is not grounded; "
        f"got {sig['plan_grounding']}")


def test_a_grounded_plan_still_scores_full_marks():
    """The guard must not punish a real plan -- every AC carries an oracle."""
    from prism_service.services import design_packet as dp
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        task_svc = _svc(Path(td))
        t = _task(task_svc, _GROUNDED_PLAN, _PLACEHOLDER_DIAGRAM,
                  _REAL_ORACLE, _REAL_MISFIRE)
        got = dp.plan_gate_certainty("probe", t.id, t)
    assert got["signals"]["plan_grounding"] == 1.0, (
        f"a plan whose every AC carries an oracle line is grounded; "
        f"got {got['signals']['plan_grounding']}")


def test_a_low_grounding_score_names_the_missing_oracle_lines():
    """A parked gate that states no real refusal is its own defect."""
    from prism_service.services import design_packet as dp
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        task_svc = _svc(Path(td))
        t = _placeholder(task_svc)
        got = dp.plan_gate_certainty("probe", t.id, t)
    blob = " ".join(got.get("reasons") or []).lower()
    assert "oracle" in blob and ("ac" in blob or "acceptance" in blob), (
        f"reasons must name what to fix, got: {got.get('reasons')}")
