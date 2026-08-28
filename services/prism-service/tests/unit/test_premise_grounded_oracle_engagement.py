"""premise_grounded's oracle-engagement tooth (task 8956d6e4).

Owner, 2026-08-28, reviewing task 0b5dd37c's premises: "i do not see any
links in the premisi that go back to the orcal, its like you skilled that
part entierly is that a missing step in theworkflow?" Confirmed by reading
services/prism-service/prism_service/services/governance_rubrics.yaml:
premise_grounded (review_previous_notes' rubric) only checked that every
claim carried a citation — it never checked a claim against task.oracle at
all. score_green_outcome (the terminal green_gate) is the only place
task.oracle was ever checked against evidence, at the LAST step of a
drive.

WORKED EXAMPLE this suite pins (the ticket's own oracle, item 3): task
0b5dd37c's real oracle asked for "every distinct tier of the Workflows
page" (clause 1). Its daemon-authored premises cited real files with real
line numbers for a run-history rail, a pill-tone function, and a p95
duration helper — every claim was grounded — but never once engaged the
oracle's actual subject (the page's TIER hierarchy, which a sibling task,
0c396de2, already defines as "tier 0 Bot / tier 1 Behavior"). A
citation-only rubric passed that report; this suite proves the new
oracle-engagement tooth would have refused it.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


ORACLE = (
    "A design/plan is proposed and approved by the owner naming: "
    "(1) every distinct tier of the Workflows page, "
    "(2) what visual state each tier shows when idle vs actively running, "
    "and (3) how idle nothing running is made to read as calm not frozen."
)

# Every claim below is grounded (a real file:line each) — it would pass the
# OLD, citation-only premise_grounded — but never mentions "tier" or any
# other word from oracle clause 1 at all. This is the real 0b5dd37c shape.
CITED_BUT_ORACLE_BLIND_NOTES = """# Review notes

## Premises
- The run-history rail is capped at 72 runs — WorkflowsPage.tsx:99
- The rail's pulse animation only fires for a live run — WorkflowsPage.tsx:642
- Step pacing uses p95 of real durations — WorkflowsPage.tsx:130
"""

# Same citations, but now the claims actually engage all three oracle
# clauses: the page's distinct tiers, the per-tier idle/running states, and
# the calm-not-frozen QUIET requirement.
ENGAGED_NOTES = """# Review notes

## Premises
- The directory already nests a bot's tier-0 canvas and its tier-1 \
Behaviors — one of the page's several distinct tiers — by parent_id, \
WorkflowsPage.tsx:320
- The rail pill is only accent-pulsed while a run is actively running; \
otherwise it renders a static tone — WorkflowsPage.tsx:642
- QUIET (idle, nothing running) must read calm, never frozen, per the \
alarm-word doctrine — components/conductor/SdlcProgress.tsx:102
"""

# Cited, and explicitly names clause 1 as a real, acknowledged gap rather
# than silently ignoring it.
UNRESOLVED_NOTES = """# Review notes

## Premises
- The run-history rail is capped at 72 runs — WorkflowsPage.tsx:99
- clause 1: UNRESOLVED, the page's distinct tiers are not yet mapped
- QUIET must read calm, never frozen, per the alarm-word doctrine — \
components/conductor/SdlcProgress.tsx:102
- clause 2: UNRESOLVED, idle/running states per tier still need a table
"""


def _gov():
    from prism_service.services import arc_governance
    return arc_governance


def _rubric():
    return _gov().load_rubrics()["premise_grounded"]


# ── rubric is YAML data ──────────────────────────────────────────────────

def test_oracle_engagement_keys_are_yaml_data():
    rub = _rubric()
    assert rub.get("require_oracle_engagement") is True
    assert rub.get("oracle_min_shared_words")
    assert rub.get("oracle_word_min_len")


# ── oracle_clauses (pure) ─────────────────────────────────────────────────

def test_oracle_clauses_splits_on_numbered_markers():
    clauses = _gov().oracle_clauses(ORACLE)
    assert len(clauses) == 3, clauses
    assert "tier" in clauses[0].lower()
    assert "idle" in clauses[1].lower()
    assert "frozen" in clauses[2].lower()


def test_oracle_clauses_falls_back_to_whole_oracle_when_unstructured():
    unstructured = "The page must show live progress at every tier."
    clauses = _gov().oracle_clauses(unstructured)
    assert clauses == [unstructured]


def test_oracle_clauses_empty_oracle_is_no_clauses():
    assert _gov().oracle_clauses("") == []
    assert _gov().oracle_clauses("   ") == []


# ── score_oracle_engagement (pure) — the worked example ──────────────────

def test_cited_but_oracle_blind_premises_are_refused():
    """THE worked example (task 8956d6e4's own oracle, item 3): every claim
    carries a real citation and would have passed the old rubric, but none
    engages oracle clause 1 ("every distinct tier")."""
    res = _gov().score_oracle_engagement(
        ORACLE, CITED_BUT_ORACLE_BLIND_NOTES, _rubric())
    assert res["ok"] is False, res
    assert "clause 1" in res["reason"], res


def test_engaged_premises_pass():
    res = _gov().score_oracle_engagement(ORACLE, ENGAGED_NOTES, _rubric())
    assert res["ok"] is True, res


def test_unresolved_marker_is_accepted_as_engagement():
    """A driver may name a real gap instead of being blocked forever —
    clauses 1 and 2 are marked UNRESOLVED; clause 3 is actually grounded."""
    res = _gov().score_oracle_engagement(ORACLE, UNRESOLVED_NOTES, _rubric())
    assert res["ok"] is True, res


def test_blank_oracle_never_blocks_on_engagement():
    res = _gov().score_oracle_engagement("", CITED_BUT_ORACLE_BLIND_NOTES, _rubric())
    assert res["ok"] is True, res


def test_marker_only_premises_skip_engagement():
    """A section whose claims are ALL REFUTED/UNVERIFIED/UNRESOLVED (no
    real citation) carries nothing that could falsely reassure a
    reviewer, so oracle engagement is not scored — this is the exact
    shape ~20 pre-existing test fixtures across the suite use for setup
    unrelated to premise content ("not a real premise claim -
    UNVERIFIED"); they must keep passing unchanged."""
    notes = ("# Review notes\n\n## Premises\n"
              "- fixture walk exercising an unrelated mechanism, not a "
              "real premise claim - UNVERIFIED\n")
    res = _gov().score_premise_grounded({"notes_md": notes, "oracle": ORACLE}, _rubric())
    assert res["ok"] is True, res


def test_engagement_disabled_by_rubric_flag():
    rubric = dict(_rubric())
    rubric["require_oracle_engagement"] = False
    res = _gov().score_oracle_engagement(
        ORACLE, CITED_BUT_ORACLE_BLIND_NOTES, rubric)
    assert res["ok"] is True, res


# ── score_premise_grounded wires the oracle tooth in ──────────────────────

def test_premise_grounded_refuses_cited_but_oracle_blind_notes():
    res = _gov().score_premise_grounded(
        {"notes_md": CITED_BUT_ORACLE_BLIND_NOTES, "oracle": ORACLE}, _rubric())
    assert res["ok"] is False, res
    assert "clause 1" in res["reason"], res


def test_premise_grounded_passes_engaged_notes():
    res = _gov().score_premise_grounded(
        {"notes_md": ENGAGED_NOTES, "oracle": ORACLE}, _rubric())
    assert res["ok"] is True, res


def test_premise_grounded_still_checks_citations_first():
    """A claim with no citation at all is still refused on the citation
    tooth, with the citation reason — oracle engagement never masks it."""
    no_citation = "# Review notes\n\n## Premises\n- The rail looks stale to a viewer\n"
    res = _gov().score_premise_grounded(
        {"notes_md": no_citation, "oracle": ORACLE}, _rubric())
    assert res["ok"] is False, res
    assert "citation" in res["reason"], res


# ── end-to-end wiring through the REAL advance_task chokepoint ──────────

def _services(tmp_path):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService
    task_svc = TaskService(str(tmp_path / "tasks.db"))
    cond = ConductorService(
        str(tmp_path / "scores.db"), enable_engine=False, task_svc=task_svc)
    return task_svc, cond


def test_advance_task_refuses_oracle_blind_notes_through_the_real_wiring(
        tmp_path):
    task_svc, cond = _services(tmp_path)
    t = task_svc.create(
        title="oracle engagement wiring: blind notes", oracle=ORACLE)
    res0 = cond.advance_task(t.id)  # '' -> review_previous_notes
    assert res0["to_step"] == "review_previous_notes", res0

    task_svc.update(t.id, premise_notes=CITED_BUT_ORACLE_BLIND_NOTES)
    res1 = cond.advance_task(t.id)
    assert res1["ok"] is False, (
        "advance_task must REFUSE review_previous_notes -> draft_story on "
        f"oracle-blind notes; got {res1}")
    assert "clause 1" in res1.get("reason", ""), res1
    t2 = task_svc.get(t.id)
    assert t2.workflow_step == "review_previous_notes", (
        "a refused report must not advance the workflow step")


def test_advance_task_advances_on_oracle_engaged_notes(tmp_path):
    task_svc, cond = _services(tmp_path)
    t = task_svc.create(
        title="oracle engagement wiring: engaged notes", oracle=ORACLE)
    cond.advance_task(t.id)  # -> review_previous_notes
    task_svc.update(t.id, premise_notes=ENGAGED_NOTES)
    res = cond.advance_task(t.id)
    assert res["ok"] is True, res
    assert res["to_step"] == "draft_story", res


def test_advance_task_accepts_unresolved_marker_through_the_real_wiring(
        tmp_path):
    task_svc, cond = _services(tmp_path)
    t = task_svc.create(
        title="oracle engagement wiring: unresolved marker", oracle=ORACLE)
    cond.advance_task(t.id)
    task_svc.update(t.id, premise_notes=UNRESOLVED_NOTES)
    res = cond.advance_task(t.id)
    assert res["ok"] is True, res
    assert res["to_step"] == "draft_story", res


def test_advance_task_with_no_oracle_is_unaffected(tmp_path):
    """A task created with no oracle at all (blank) must not regress —
    oracle engagement never blocks when there is nothing to trace."""
    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="oracle engagement wiring: no oracle")
    cond.advance_task(t.id)
    task_svc.update(t.id, premise_notes=CITED_BUT_ORACLE_BLIND_NOTES)
    res = cond.advance_task(t.id)
    assert res["ok"] is True, res
    assert res["to_step"] == "draft_story", res
