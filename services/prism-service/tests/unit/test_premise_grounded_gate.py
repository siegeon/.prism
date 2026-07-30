"""premise_grounded on review_previous_notes (task 3a63190b / github issue
#222), UNCONDITIONAL follow-up (task 3928b7ac / issue #222 continued).

3a63190b shipped the rubric OPT-IN: it only engaged once a report opened a
'## Premises' heading; a report with none had "nothing to ground" and
passed. That scoping was forced by a real collision — task.completion_proof
is a SHARED field also read by the green_gate oracle tooth
(score_green_outcome), and several existing suites stage green-proof-shaped
content there on a fresh task as unrelated fixture setup. Gating
unconditionally on that SAME field broke those suites.

This task gives review_previous_notes its OWN field (task.premise_notes)
and RETIRES the opt-in escape hatch: an absent or empty premise report is
now REFUSED, not passed, closing the exact gap issue #222 named — a driver
who never considers citing evidence, not one who declines to. The dedicated
field means the shared-field collision cannot recur: score_premise_grounded
now reads ONLY task.premise_notes (via ConductorService._verify_rubric_gate),
never task.completion_proof, so unrelated content staged in completion_proof
for a later gate never satisfies (and never falsely blocks) this one.

FAILS today (pre-implementation): task.premise_notes does not exist yet,
_verify_rubric_gate still reads completion_proof, and score_premise_grounded
still returns ok=True on a missing/empty section.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


COMPLIANT_NOTES = """# Review notes

## Premises
- The failing CI lane is unit-tests.yml, configured identically to \
the integration lane — services/prism-service/.github/workflows/unit-tests.yml:12
- The repo ships no Testcontainers configuration — UNVERIFIED, grep found \
no hits yet, re-check before shipping this claim as fact
"""

# Same claims, but the first citation is a bare path with no line number —
# the citation-theatre misfire this rubric exists to catch.
THEATRE_NOTES = COMPLIANT_NOTES.replace(
    "services/prism-service/.github/workflows/unit-tests.yml:12",
    "services/prism-service/.github/workflows/unit-tests.yml",
)

NO_SECTION_NOTES = "# Review notes\n\nJust some prose, no Premises heading.\n"

EMPTY_SECTION_NOTES = "# Review notes\n\n## Premises\n\nNothing recorded yet.\n"


def _gov():
    from prism_service.services import arc_governance
    return arc_governance


def _rubric():
    return _gov().load_rubrics()["premise_grounded"]


# ── rubric is YAML data ──────────────────────────────────────────────────

def test_premise_grounded_rubric_is_yaml_data():
    rub = _gov().load_rubrics()
    assert "premise_grounded" in rub


# ── pure scorer (AC-2) ───────────────────────────────────────────────────

def test_compliant_notes_score_ok():
    res = _gov().score_premise_grounded(
        {"notes_md": COMPLIANT_NOTES}, _rubric())
    assert res["ok"] is True, res


def test_bare_path_without_line_number_is_citation_theatre():
    """likely_misfire (1): a path with no line number is NOT a citation."""
    res = _gov().score_premise_grounded(
        {"notes_md": THEATRE_NOTES}, _rubric())
    assert res["ok"] is False, (
        "a bare path with no line number must not satisfy the rubric")


def test_missing_premises_section_now_refused_unconditionally():
    """SUPERSEDED (task 3928b7ac / issue #222): the old opt-in behavior
    ("nothing to ground -> pass") is retired. review_previous_notes now
    has its OWN dedicated field (task.premise_notes), so completion_proof
    staged for an unrelated LATER purpose (e.g. a green_gate proof set
    early in a test fixture) can never even reach this scorer's evidence —
    the shared-field collision this test used to guard against cannot
    recur. With that guard no longer needed, a missing '## Premises'
    section is a real gap (AC-2): REFUSED, not passed."""
    res = _gov().score_premise_grounded(
        {"notes_md": NO_SECTION_NOTES}, _rubric())
    assert res["ok"] is False, res


def test_empty_premises_section_fails():
    """A PRESENT but empty section means the driver opened the convention
    and wrote nothing under it - that still fails (unchanged by 3928b7ac)."""
    res = _gov().score_premise_grounded(
        {"notes_md": EMPTY_SECTION_NOTES}, _rubric())
    assert res["ok"] is False


def test_empty_notes_now_refused_unconditionally():
    """SUPERSEDED (task 3928b7ac / issue #222): empty notes_md used to pass
    ("nothing to ground"). AC-2: unconditional means empty is refused too —
    a driver must record at least one grounded premise claim."""
    res = _gov().score_premise_grounded({"notes_md": ""}, _rubric())
    assert res["ok"] is False, res


def test_refusal_names_the_offending_claim():
    res = _gov().score_premise_grounded(
        {"notes_md": THEATRE_NOTES}, _rubric())
    assert "unit-tests.yml" in res["reason"], (
        "refusal must name the offending claim so the driver can "
        f"self-diagnose: {res}")


def test_refuted_marker_is_accepted_as_grounding():
    notes = "# Review notes\n\n## Premises\n- The CI lanes differ — REFUTED, both jobs use the same runner image\n"
    res = _gov().score_premise_grounded({"notes_md": notes}, _rubric())
    assert res["ok"] is True, res


def test_backtick_wrapped_bare_token_is_not_a_citation():
    """likely_misfire, generalized: wrapping a single bare token (a
    filename, a variable name) in backticks must not count as 'command
    output' evidence — only a real multi-token output snippet does."""
    notes = ("# Review notes\n\n## Premises\n"
             "- The lane is misconfigured, see `unit-tests.yml`\n")
    res = _gov().score_premise_grounded({"notes_md": notes}, _rubric())
    assert res["ok"] is False, (
        "a bare filename in backticks is not command-output evidence")


def test_real_command_output_is_accepted_as_a_citation():
    notes = ("# Review notes\n\n## Premises\n"
             "- Both CI lanes use the same image, confirmed by "
             "`pytest -q -> 2 passed`\n")
    res = _gov().score_premise_grounded({"notes_md": notes}, _rubric())
    assert res["ok"] is True, res


# ── b3-style wiring: the kind is registered, not just the scorer ─────────

def test_verifier_rules_registers_premise_grounded():
    from prism_service.services.conductor_service import ConductorService
    rule = ConductorService._VERIFIER_RULES.get("premise_grounded")
    assert rule is not None, "premise_grounded missing from _VERIFIER_RULES"
    assert rule.get("rubric") == "premise_grounded", rule


def test_workflow_step_carries_the_validation_kind():
    from prism_service.models.workflow import WORKFLOW_STEPS
    step = next(s for s in WORKFLOW_STEPS
                if s["id"] == "review_previous_notes")
    assert step["validation"] == "premise_grounded", (
        "review_previous_notes still has validation=None (issue #222)")


def test_story_gate_inheritance_is_unaffected():
    """Guard against a regression this rubric could cause: draft_story's
    own validation must still win story_gate's inheritance (issue #222's
    design constraint — premise_grounded cannot ride gate inheritance)."""
    from prism_service.services.conductor_service import ConductorService
    assert ConductorService._validation_for_gate("story_gate") == \
        "story_complete", (
        "story_gate must still inherit story_complete, not "
        "premise_grounded, even though review_previous_notes now carries "
        "a validation kind")


# ── task.premise_notes: the dedicated field (AC-1) ───────────────────────

def test_premise_notes_round_trips_through_task_service(tmp_path):
    from prism_service.services.task_service import TaskService
    task_svc = TaskService(str(tmp_path / "tasks.db"))
    t = task_svc.create(title="premise notes round-trip")
    assert t.premise_notes == "", "premise_notes must default to ''"
    task_svc.update(t.id, premise_notes=COMPLIANT_NOTES)
    t2 = task_svc.get(t.id)
    assert t2.premise_notes == COMPLIANT_NOTES


# ── end-to-end wiring through the REAL advance_task chokepoint ──────────
# (the exact path conductor_flow.flow_report calls for every non-gate step
# report) — no arc_governance import here, so a scorer-only test cannot
# fake this evidence.

def _services(tmp_path):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService
    task_svc = TaskService(str(tmp_path / "tasks.db"))
    cond = ConductorService(
        str(tmp_path / "scores.db"), enable_engine=False, task_svc=task_svc)
    return task_svc, cond


def test_advance_task_refuses_ungrounded_notes_through_the_real_wiring(
        tmp_path):
    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="premise gate wiring: bad notes")
    res0 = cond.advance_task(t.id)  # '' -> review_previous_notes
    assert res0["to_step"] == "review_previous_notes", res0

    # AC-3: evidence comes from the DEDICATED field, not completion_proof.
    task_svc.update(t.id, premise_notes=THEATRE_NOTES)
    res1 = cond.advance_task(t.id)
    assert res1["ok"] is False, (
        "advance_task must REFUSE review_previous_notes -> draft_story on "
        f"ungrounded notes; got {res1}")
    assert "unit-tests.yml" in res1.get("reason", ""), res1
    t2 = task_svc.get(t.id)
    assert t2.workflow_step == "review_previous_notes", (
        "a refused report must not advance the workflow step")


def test_advance_task_advances_on_compliant_notes(tmp_path):
    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="premise gate wiring: compliant notes")
    cond.advance_task(t.id)  # -> review_previous_notes
    task_svc.update(t.id, premise_notes=COMPLIANT_NOTES)
    res = cond.advance_task(t.id)
    assert res["ok"] is True, res
    assert res["to_step"] == "draft_story", res


def test_unrelated_completion_proof_without_premise_notes_is_now_refused(
        tmp_path):
    """SUPERSEDED (task 3928b7ac / issue #222): this used to be named
    test_unrelated_preset_completion_proof_does_not_block_the_step and
    pinned the OPT-IN regression guard — an unrelated green-proof-shaped
    string staged in completion_proof (for a LATER, unrelated gate) had to
    not block review_previous_notes, because at the time notes_md WAS
    completion_proof. Now notes_md is task.premise_notes, a dedicated
    field completion_proof never reaches — so staging content ONLY in
    completion_proof (premise_notes left at its '' default) no longer
    resembles a premise report at all. Under the new unconditional rule
    (AC-2) that is refused, exactly like any other missing premise_notes
    case; the two fields are fully decoupled, not merely opt-in-decoupled."""
    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="premise gate: unrelated preset proof")
    task_svc.update(t.id, completion_proof=(
        "full suite green: 1447 passed in 150s - pytest tests -q; "
        "screenshot on file"))
    cond.advance_task(t.id)  # '' -> review_previous_notes
    res = cond.advance_task(t.id)  # premise_notes is still '' -> refused
    assert res["ok"] is False, res
    assert task_svc.get(t.id).workflow_step == "review_previous_notes"


def test_refusal_reason_is_recorded_on_the_task_for_self_diagnosis(
        tmp_path):
    """The refusal must be readable off the TASK (gate_reason), not just
    the advance_task return value, so a driver polling
    conductor_work/task_list can self-diagnose without re-deriving the
    rubric (the same convention story_gate/plan_gate already use)."""
    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="premise gate wiring: reason on task")
    cond.advance_task(t.id)
    task_svc.update(t.id, premise_notes=THEATRE_NOTES)
    cond.advance_task(t.id)
    t2 = task_svc.get(t.id)
    assert getattr(t2, "gate_reason", ""), (
        "a refused premise_grounded report must record an actionable "
        "gate_reason on the task")
    assert "unit-tests.yml" in t2.gate_reason, t2.gate_reason


def test_advance_task_clears_stale_reason_on_a_later_compliant_report(
        tmp_path):
    """A driver that fixes its notes after a refusal must not still see
    the stale reason once it has advanced past the step."""
    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="premise gate wiring: reason clears")
    cond.advance_task(t.id)
    task_svc.update(t.id, premise_notes=THEATRE_NOTES)
    cond.advance_task(t.id)  # refused, gate_reason set
    task_svc.update(t.id, premise_notes=COMPLIANT_NOTES)
    res = cond.advance_task(t.id)  # now compliant -> advances
    assert res["ok"] is True, res
    t2 = task_svc.get(t.id)
    assert t2.workflow_step == "draft_story"
    assert not (getattr(t2, "gate_reason", "") or ""), (
        "a stale premise_grounded refusal reason must not linger once "
        f"the task has advanced past the step: {t2.gate_reason!r}")


# ── MCP report path dual-writes premise_notes + completion_proof (AC-4) ──

def test_mcp_report_routes_review_previous_notes_proof_to_premise_notes():
    """Source-reading test (not a call-through — the MCP dispatcher needs a
    live server/project context this unit suite does not stand up): parses
    the conductor_work report branch in mcp/tools.py and asserts the
    review_previous_notes case is routed to BOTH task.premise_notes (the
    new dedicated field the gate reads) and task.completion_proof (kept for
    backward-compatible rendering — TaskDetailPage renders completion_proof
    generically today)."""
    tools_path = (_SERVICE_ROOT / "prism_service" / "mcp" / "tools.py")
    src = tools_path.read_text(encoding="utf-8")
    idx = src.find('if _cur["id"] in ("draft_story", "verify_plan"):')
    assert idx != -1, "conductor_work proof-routing branch not found"
    window = src[idx:idx + 800]
    assert '"review_previous_notes"' in window, (
        "review_previous_notes must have its own routing arm in the "
        f"proof router: {window}")
    assert "premise_notes=str(_proof)" in window, (
        f"review_previous_notes must route proof to premise_notes: {window}")
